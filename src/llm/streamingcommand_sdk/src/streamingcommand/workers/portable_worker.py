"""Portable one-engine protocol, supporting native PyTorch and built-in vLLM.

The legacy vLLM0.14 backend retains its exact separate workers. This adapter
shares scheduling policies but makes no claim of identical backend numerics.
"""
import argparse
import base64
from contextlib import redirect_stdout
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import time
import traceback
from types import SimpleNamespace


def fixed_bias(tokenizer, context):
    """Decode the request's fixed prior payload and build bounded token weights for portable ASR."""
    marker = "__PHONO_DOMAIN_TOKEN_PRIOR__"
    if not context.startswith(marker):
        return context, None
    from asr_fusion_priors import make_bias
    from asr_word_bias import whole_word_bias
    spec = json.loads(context[len(marker):])
    cjk = spec.pop("cjk_words", [])
    cjk_weight = float(spec.pop("cjk_weight", 0))
    def bounded(tok, words, weight):
        """Adapt the tokenizer's uniform-bias limit to the explicitly permitted per-word weight."""
        result = whole_word_bias(tok, words, min(float(weight), 3))
        return {key: value * float(weight) / 3 for key, value in result.items()} if float(weight) > 3 else result
    if "word_weights" not in spec:
        spec["word_weights"] = {}
    result = make_bias(tokenizer, spec, bounded)
    if cjk and (cjk != ["支", "個", "把"] or not 0 <= cjk_weight <= 2):
        raise ValueError("Invalid Cantonese classifier prior")
    for word in cjk:
        ids = tokenizer.encode(word, add_special_tokens=False)
        if len(ids) == 1 and tokenizer.decode(ids) == word:
            result[int(ids[0])] = cjk_weight
    return spec["context"], result


class NativeBanks:
    """One loaded model; bank only differing parameters, preserve parameter addresses."""
    def __init__(self, generator, primary, alternate):
        """Verify both checkpoints and build the exact base and mix75 banks for differing live parameters."""
        import torch
        from safetensors import safe_open
        from asr_shared_weight_bank import verify_model_compatibility
        self.compatibility = verify_model_compatibility(primary, alternate)
        model = generator.hf_model
        self.params = {}
        self.banks = {"base": {}, "mix75": {}}
        indexes = [json.loads((Path(p) / "model.safetensors.index.json").read_text())["weight_map"] for p in (primary, alternate)]
        for name, param in model.named_parameters():
            if name not in indexes[0] or name not in indexes[1]:
                continue
            values = []
            for directory, index in zip((primary, alternate), indexes):
                with safe_open(str(Path(directory) / index[name]), framework="pt", device="cpu") as file:
                    values.append(file.get_tensor(name).to(torch.float16))
            if torch.equal(values[0].view(torch.int16), values[1].view(torch.int16)):
                continue
            self.params[name] = param
            self.banks["base"][name] = values[1].to(device=param.device, dtype=param.dtype)
            mixed = torch.lerp(values[1].float(), values[0].float(), .75).to(torch.float16)
            self.banks["mix75"][name] = mixed.to(device=param.device, dtype=param.dtype)
        self.variant = "v15"
        self.inventory = {"engine_count": 1, "bank_names": ["v15", "base", "mix75"], "different_tensors": len(self.params),
                          "cache_policy": "new KV cache per generation", "compatibility": self.compatibility}

    def select(self, variant):
        """Swap one bank in place, preserving parameter addresses for the resident engine."""
        import torch
        if variant != self.variant and variant not in self.banks:
            raise ValueError("Unknown model bank")
        changed = variant != self.variant
        if changed:
            with torch.no_grad():
                for name, parameter in self.params.items():
                    temporary = parameter.detach().clone()
                    parameter.copy_(self.banks[variant][name])
                    self.banks[variant][name].copy_(temporary)
            self.banks[self.variant] = self.banks.pop(variant)
            self.variant = variant
        return {"variant": variant, "changed": changed, "cache_invalidated": changed}


def build_engine(args):
    """Initialize the selected native or built-in vLLM engine after checking the visible GPU identity."""
    import torch
    from asr_windows_native import NativeWindowsASR
    backend = os.environ.get("STREAMINGCOMMAND_ASR_BACKEND", "native")
    requested = os.environ.get("STREAMINGCOMMAND_DEVICE", "cpu")
    device = "cuda:0" if requested.startswith("cuda:") else "cpu"
    expected = os.environ.get("PHONO_ASR_GPU_UUID")
    actual_uuid = None
    if device.startswith("cuda:"):
        actual_uuid = str(torch.cuda.get_device_properties(0).uuid)
        if expected and actual_uuid.lower().removeprefix("gpu-") != expected.lower().removeprefix("gpu-"):
            raise RuntimeError("ASR GPU UUID mismatch")
    torch.set_num_threads(4)
    if backend == "native":
        asr = NativeWindowsASR(args.model_dir, args.max_new_tokens, args.max_model_len, device=device)
        bank = NativeBanks(asr.model, args.model_dir, args.alternate_model_dir) if args.alternate_model_dir else None
    elif backend == "vllm-builtin":
        os.environ["VLLM_ENABLE_V1_MULTIPROCESSING"] = "0"
        os.environ["VLLM_USE_FLASHINFER_SAMPLER"] = "0"
        from vllm import LLM, SamplingParams, ModelRegistry
        from transformers import AutoTokenizer
        import asr_modern_weight_bank
        sys.modules["asr_shared_weight_bank"] = asr_modern_weight_bank
        from asr_blended_weight_bank import BlendedEngineController
        if "Qwen3ASRForConditionalGeneration" not in ModelRegistry.get_supported_archs():
            raise RuntimeError("This vLLM wheel has no built-in Qwen3-ASR model")
        tokenizer = AutoTokenizer.from_pretrained(args.model_dir, local_files_only=True)
        template = Path(args.model_dir) / "chat_template.json"
        if template.exists():
            tokenizer.chat_template = json.loads(template.read_text())["chat_template"]
        asr = NativeWindowsASR.__new__(NativeWindowsASR)
        asr.processor = SimpleNamespace(tokenizer=tokenizer, apply_chat_template=tokenizer.apply_chat_template)
        options = dict(dtype="float16", model_impl="vllm", max_model_len=args.max_model_len,
            max_num_seqs=1, tensor_parallel_size=1, distributed_executor_backend="uni",
            gpu_memory_utilization=args.gpu_memory_utilization, enable_prefix_caching=True,
            worker_extension_cls="asr_blended_weight_bank.BlendedWeightBankExtension",
            limit_mm_per_prompt={"audio": 1})
        if args.kv_cache_memory_bytes:
            options["kv_cache_memory_bytes"] = args.kv_cache_memory_bytes
        # Audit and install the same EOS/punctuation protection inside the real engine.
        from asr_boundary_prior_guard import install
        install()
        asr.model = LLM(model=args.model_dir, **options)
        asr.sampling_params = SamplingParams(temperature=0, max_tokens=args.max_new_tokens,
                                            stop_token_ids=[151643, 151645], seed=0)
        bank = BlendedEngineController(asr.model, args.model_dir, args.alternate_model_dir, bank_mode="swap")
    else:
        raise ValueError("Unknown portable backend")
    return asr, bank, backend, actual_uuid


def main():
    """Serve portable ASR load, streaming, final decode, and shutdown operations over JSON lines."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--alternate-model-dir")
    parser.add_argument("--max-new-tokens", type=int, default=96)
    parser.add_argument("--max-model-len", type=int, default=4096)
    parser.add_argument("--gpu-memory-utilization", type=float, default=.35)
    parser.add_argument("--kv-cache-memory-bytes", type=int)
    parser.add_argument("--weight-bank-mode", default="swap")
    args = parser.parse_args()
    # Native libraries can print to fd1; keep the protocol on its own descriptor.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    def emit(value):
        """Write and flush a worker event to the original protocol descriptor."""
        print(json.dumps(value, ensure_ascii=False), file=protocol, flush=True)
    try:
        import numpy as np
        asr, bank, backend, device_uuid = build_engine(args)
        emit({"ok": True, "event": "ready", "backend": backend,
              "versions": {name: importlib.metadata.version(name) for name in ("torch", "transformers", "numpy")},
              "engine_count": 1, "banks": bank.inventory if bank else None, "device_uuid": device_uuid})
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        emit({"ok": False, "event": "startup_error", "error": str(exc)})
        return 1
    from qwen_native_prefix_final_stream_server import temporary_safe_finish_prefix
    from asr_supported_margins_v3 import supported_margin_pcm
    state = None
    segment = None
    state_variant = None
    generations = []
    original = asr.model.generate
    operation = None
    def measured(prompts, sampling_params, use_tqdm=False):
        """Wrap generation and retain timing and text metadata for the current ASR request."""
        started = time.perf_counter()
        results = original(prompts, sampling_params=sampling_params, use_tqdm=use_tqdm)
        for result in results:
            generations.append({"decode_ms": (time.perf_counter() - started) * 1000,
                "generated_texts": [output.text for output in result.outputs],
                "generated_token_ids": [list(output.token_ids) for output in result.outputs],
                "generated_tokens": sum(len(output.token_ids) for output in result.outputs),
                "num_cached_tokens": getattr(result, "num_cached_tokens", 0), "on_final": operation == "transcribe"})
        return results
    asr.model.generate = measured
    for line in sys.stdin:
        try:
            request = json.loads(line)
            operation = request["op"]
            if operation == "close":
                emit({"ok": True, "op": "close"})
                break
            variant = request.get("model_variant", "v15")
            selected = bank.select(variant) if bank else {"variant": "v15"}
            generations.clear()
            reuse = False
            if operation == "start":
                state = asr.init_streaming_state(context=request.get("context", ""), language=request.get("language"),
                    chunk_size_sec=request.get("chunk_size_sec", .8), unfixed_chunk_num=request.get("unfixed_chunk_num", 2),
                    unfixed_token_num=request.get("unfixed_token_num", 5))
                segment, state_variant = request["segment_id"], variant
                text, language, raw, processed = "", None, "", 0
            elif operation in ("transcribe", "update", "finish"):
                raw_audio = base64.b64decode(request.get("audio_float32_b64", ""), validate=True)
                if len(raw_audio) % 4:
                    raise ValueError("Audio must contain float32 samples")
                audio = np.frombuffer(raw_audio, dtype="<f4").copy()
                if not np.isfinite(audio).all():
                    raise ValueError("Nonfinite audio")
                if operation in ("update", "finish"):
                    if state is None or segment != request.get("segment_id") or state_variant != variant:
                        raise ValueError("No compatible streaming state")
                    if operation == "update":
                        asr.streaming_transcribe(audio, state)
                    else:
                        with temporary_safe_finish_prefix(asr.processor.tokenizer, state):
                            asr.finish_streaming_transcribe(state)
                    text, language, raw, processed = state.text, state.language, state._raw_decoded, len(state.audio_accum)
                else:
                    context, bias = fixed_bias(asr.processor.tokenizer, request.get("context", ""))
                    original_sampling = asr.sampling_params
                    asr.sampling_params = original_sampling.clone()
                    asr.sampling_params.logit_bias = bias
                    try:
                        reuse = bool(request.get("reuse_stream_prefix") and state is not None and segment == request.get("segment_id")
                            and state_variant == variant and request.get("context", "") == state.context and request.get("language") == state.force_language)
                        if reuse:
                            pcm = np.rint(audio * 32768).clip(-32768, 32767).astype(np.int16)
                            normalized, _ = supported_margin_pcm(pcm)
                            state.audio_accum = np.zeros(0, np.float32)
                            state.buffer = normalized.astype(np.float32) / 32768
                            rollback = state.unfixed_token_num
                            state.unfixed_token_num = int(request.get("final_prefix_tokens", 5))
                            try:
                                with temporary_safe_finish_prefix(asr.processor.tokenizer, state):
                                    asr.finish_streaming_transcribe(state)
                            finally:
                                state.unfixed_token_num = rollback
                            result = state
                        else:
                            result = asr.transcribe((audio, 16000), context=context, language=request.get("language"))[0]
                    finally:
                        asr.sampling_params = original_sampling
                    if len(generations) != 1:
                        raise AssertionError("Final ASR must use exactly one generation")
                    text, language = result.text, result.language
                    raw = state._raw_decoded if reuse else "".join(t for g in generations for t in g["generated_texts"])
                    processed = len(audio)
            else:
                raise ValueError("Unknown operation")
            ids = asr.processor.tokenizer.encode(raw)
            emit({"ok": True, "op": operation, "segment_id": request.get("segment_id"), "text": text,
                  "detected_language": language, "raw_decoded": raw, "audio_processed_samples": processed,
                  "audio_received_samples": processed, "audio_buffered_samples": 0, "generations": list(generations),
                  "shared_engine": selected, "final_stream_state_reused": reuse,
                  "next_prefix_tokens": max(0, len(ids) - state.unfixed_token_num) if state and state.chunk_id >= state.unfixed_chunk_num else 0})
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            emit({"ok": False, "error": str(exc)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
