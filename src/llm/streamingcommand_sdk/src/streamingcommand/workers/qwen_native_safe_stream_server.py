"""Optional Qwen3-ASR/vLLM worker with UTF-safe finish-prefix rollback.

Run using .runtime/qwen-native-vllm/bin/python, never the main project runtime.
All library logs (including inherited child-process stdout) go to stderr. Each
input line receives one JSON response on stdout. Audio is mono 16 kHz float32,
base64 encoded little-endian bytes; updates must contain only newly received PCM.

The official SDK re-encodes accumulated audio and rolls back decoder text tokens.
This wrapper does not claim incremental encoder states or alter transcripts.
Only finish rollback parameters may temporarily change to avoid a decoded U+FFFD
prefix. This guard does not repair translation or modify generated/raw text.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import time
import traceback
from asr_word_bias import whole_word_bias

# vLLM starts fresh Python workers with multiprocessing spawn. Set this before
# the __main__ guard so those workers also avoid the host's incompatible optional
# kernels package; setting it only inside main() leaves engine children exposed.
sys.modules["kernels"] = None


def plan_safe_finish_prefix(tokenizer, raw_decoded, chunk_id,
                            unfixed_chunk_num, unfixed_token_num):
    """Choose parameters for the SDK's exact max(1, len(ids)-K) finish rule."""
    original_k, original_n = int(unfixed_token_num), int(unfixed_chunk_num)
    effective_k, effective_n = original_k, original_n
    ids = tokenizer.encode(raw_decoded)
    prefix = original_prefix = ""
    count = original_count = 0
    if chunk_id >= original_n:
        end = max(1, len(ids) - effective_k)
        prefix = original_prefix = tokenizer.decode(ids[:end])
        count = original_count = len(ids[:end])
        while "\ufffd" in prefix:
            if end <= 1 or count <= 1:
                # SDK finish cannot retain zero tokens with max(1,...).
                effective_n = chunk_id + 1
                prefix, count = "", 0
                break
            effective_k += 1
            end = max(1, len(ids) - effective_k)
            prefix = tokenizer.decode(ids[:end])
            count = len(ids[:end])
    return {
        "guard_name": "utf8-safe-finish-prefix", "suffixsafe_prefix_guard": True,
        "on_final": True, "raw_token_count": len(ids),
        "original_unfixed_token_num": original_k, "effective_unfixed_token_num": effective_k,
        "original_unfixed_chunk_num": original_n, "effective_unfixed_chunk_num": effective_n,
        "original_prefix_token_count": original_count, "effective_prefix_token_count": count,
        "trimmed_tokens": original_count - count,
        "original_prefix_contains_replacement": "\ufffd" in original_prefix,
        "effective_prefix_contains_replacement": "\ufffd" in prefix,
        "prefix_disabled": effective_n != original_n,
        "adjusted": effective_k != original_k or effective_n != original_n,
    }


@contextmanager
def temporary_safe_finish_prefix(tokenizer, state):
    """Restore both policy parameters on success or exception; never edit text."""
    original_k, original_n = state.unfixed_token_num, state.unfixed_chunk_num
    adjustment = plan_safe_finish_prefix(tokenizer, state._raw_decoded, state.chunk_id,
                                         original_n, original_k)
    adjustment["finish_has_audio_tail"] = state.buffer is not None and len(state.buffer) > 0
    state.unfixed_token_num = adjustment["effective_unfixed_token_num"]
    state.unfixed_chunk_num = adjustment["effective_unfixed_chunk_num"]
    try:
        yield adjustment
    finally:
        state.unfixed_token_num, state.unfixed_chunk_num = original_k, original_n


def arguments():
    """Parse the safe-stream worker's model, generation, and optional bank options."""
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", default=str(root / ".model-cache/qwen3-asr-1.7b"))
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.35)
    parser.add_argument("--max-model-len", type=int, default=2048)
    parser.add_argument("--max-new-tokens", type=int, default=96)
    parser.add_argument("--attention-backend", default="TRITON_ATTN")
    parser.add_argument("--disable-prefix-caching", action="store_true")
    return parser.parse_args()


def main():
    """Load the legacy engine and serve ASR requests with guarded streaming finalization."""
    args = arguments()
    # Keep a dedicated protocol descriptor, then route fd 1 to stderr. This also
    # catches native code and spawned vLLM workers which bypass redirect_stdout.
    protocol_fd = os.dup(sys.stdout.fileno())
    os.set_inheritable(protocol_fd, False)
    protocol = os.fdopen(protocol_fd, "w", encoding="utf-8", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

    def emit(payload):
        """Write a flushed JSON response while native library output stays off the protocol stream."""
        protocol.write(json.dumps(payload, ensure_ascii=False) + "\n")
        protocol.flush()

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("VLLM_WORKER_MULTIPROC_METHOD", "spawn")
    os.environ.setdefault("OMP_NUM_THREADS", "4")
    runtime_cache = Path(os.environ.get("STREAMINGCOMMAND_RUNTIME_CACHE", str(Path.home() / ".cache/StreamingCommand/runtime")))
    runtime_cache.mkdir(parents=True, exist_ok=True)
    for variable, directory in {
        "VLLM_CACHE_ROOT": "vllm", "TRITON_CACHE_DIR": "triton",
        "TORCHINDUCTOR_CACHE_DIR": "inductor", "CUDA_CACHE_PATH": "cuda",
        "NUMBA_CACHE_DIR": "numba",
    }.items():
        os.environ.setdefault(variable, str(runtime_cache / directory))
    os.environ.setdefault("VLLM_NO_USAGE_STATS", "1")
    # This optional package is unrelated to SDPA and can cause HF Hub conflicts.
    sys.modules["kernels"] = None
    try:
        import numpy as np
        from qwen_asr import Qwen3ASRModel
        # The SDK silently suppresses registration import failures. Perform the
        # same registration explicitly so an incompatible backend is reported.
        from qwen_asr.core.vllm_backend import Qwen3ASRForConditionalGeneration
        from vllm import ModelRegistry
        ModelRegistry.register_model("Qwen3ASRForConditionalGeneration",
                                     Qwen3ASRForConditionalGeneration)

        options = {
            "dtype": "float16",
            "gpu_memory_utilization": args.gpu_memory_utilization,
            "max_model_len": args.max_model_len,
            "max_num_seqs": 1,
            "enforce_eager": True,
            "enable_prefix_caching": not args.disable_prefix_caching,
            "limit_mm_per_prompt": {"audio": 1},
        }
        if args.attention_backend:
            options["attention_config"] = {"backend": args.attention_backend}
        initialized = time.perf_counter()
        asr = Qwen3ASRModel.LLM(
            model=str(Path(args.model_dir).resolve()),
            max_new_tokens=args.max_new_tokens,
            **options,
        )
        versions = {}
        for package in ("qwen-asr", "vllm", "torch", "transformers", "numpy", "numba",
                        "llvmlite", "triton", "huggingface-hub", "nvidia-cudnn-cu12",
                        "nvidia-nccl-cu12", "nvidia-nvshmem-cu12"):
            versions[package] = importlib.metadata.version(package)
        emit({
            "event": "ready", "ok": True, "backend": "qwen-vllm-native",
            "versions": versions, "model_dir": str(Path(args.model_dir).resolve()),
            "load_seconds": time.perf_counter() - initialized,
            "options": options, "encoder_cache": False,
            "shared_libraries_provenance": os.environ.get("STREAMINGCOMMAND_ASR_ENVIRONMENT_PROVENANCE"),
            "dependency_environment": "explicit_external_asr_python_versions_reported",
            "safe_finish_prefix_guard": "utf8-safe-finish-prefix",
        })
    except Exception as error:
        traceback.print_exc(file=sys.stderr)
        emit({"event": "startup_error", "ok": False, "error": str(error),
              "error_type": type(error).__name__})
        return 1

    # Count the actual native vLLM generations, including token reuse telemetry
    # exposed by the installed vLLM version. No outputs are selected or repaired.
    generation_stats = []
    original_generate = asr.model.generate

    def measured_generate(*call_args, **call_kwargs):
        """Capture generation counts, timings, and token metadata for the current request."""
        inputs = call_args[0] if call_args else call_kwargs.get("prompts", [])
        started = time.perf_counter()
        results = original_generate(*call_args, **call_kwargs)
        elapsed_ms = (time.perf_counter() - started) * 1000
        for index, result in enumerate(results):
            input_item = inputs[index] if index < len(inputs) else {}
            prompt = input_item.get("prompt", "")
            base_prompt = state.prompt_raw if state is not None else ""
            prefix = prompt[len(base_prompt):] if base_prompt and prompt.startswith(base_prompt) else ""
            audio_items = input_item.get("multi_modal_data", {}).get("audio", [])
            outputs = getattr(result, "outputs", [])
            generation_stats.append({
                "decode_ms": elapsed_ms,
                "prompt_tokens": len(getattr(result, "prompt_token_ids", None) or []),
                "prefix_text_used": prefix,
                "prefix_tokens_used": len(asr.processor.tokenizer.encode(prefix)) if prefix else 0,
                "audio_samples_reencoded": sum(len(item) for item in audio_items),
                "generated_tokens": sum(len(getattr(item, "token_ids", None) or []) for item in outputs),
                "generated_texts": [item.text for item in outputs],
                "generated_token_ids": [list(getattr(item, "token_ids", None) or []) for item in outputs],
                "logit_bias": getattr(asr.sampling_params, "logit_bias", None),
                "num_cached_tokens": getattr(result, "num_cached_tokens", None),
                "finish_reasons": [getattr(item, "finish_reason", None) for item in outputs],
                "on_final": operation == "finish",
                "safe_prefix_adjustment": dict(safe_prefix_adjustment)
                if safe_prefix_adjustment is not None else None,
            })
        return results

    asr.model.generate = measured_generate
    state = None
    segment_id = None
    received = 0
    operation = None
    safe_prefix_adjustment = None

    def audio_from_request(request):
        """Decode a base64 request into finite little-endian float32 waveform samples."""
        raw = base64.b64decode(request["audio_float32_b64"], validate=True)
        if len(raw) % 4:
            raise ValueError("audio_float32_b64 must contain little-endian float32 bytes")
        audio = np.frombuffer(raw, dtype="<f4").copy()
        if not np.isfinite(audio).all():
            raise ValueError("Audio contains NaN or Inf")
        return audio

    for line in sys.stdin:
        if not line.strip():
            continue
        request = {}
        generation_stats.clear()
        safe_prefix_adjustment = None
        started = time.perf_counter()
        try:
            request = json.loads(line)
            operation = request["op"]
            if operation == "close":
                emit({"ok": True, "op": operation})
                break
            if operation == "start":
                state = asr.init_streaming_state(
                    context=request.get("context", ""),
                    language=request.get("language"),
                    chunk_size_sec=float(request.get("chunk_size_sec", 0.64)),
                    unfixed_chunk_num=int(request.get("unfixed_chunk_num", 2)),
                    unfixed_token_num=int(request.get("unfixed_token_num", 5)),
                )
                segment_id = request["segment_id"]
                received = 0
            elif operation == "transcribe":
                audio = audio_from_request(request)
                original_sampling = asr.sampling_params
                bias = request.get("word_bias")
                try:
                    if bias:
                        if request.get("language") != "English":
                            raise ValueError("Word bias is restricted to explicit English-conditioned decoding")
                        sampling = original_sampling.clone()
                        sampling.logit_bias = whole_word_bias(asr.processor.tokenizer, bias['words'], bias['weight'])
                        asr.sampling_params = sampling
                    result = asr.transcribe(
                        audio=(audio, 16000), context=request.get("context", ""),
                        language=request.get("language"),
                    )[0]
                finally:
                    asr.sampling_params = original_sampling
                emit({
                    "ok": True, "op": operation,
                    "segment_id": request.get("segment_id"),
                    "text": result.text, "detected_language": result.language,
                    "raw_decoded": "".join(text for item in generation_stats
                                              for text in item["generated_texts"]),
                    "raw_decoded_chunks": [text for item in generation_stats
                                           for text in item["generated_texts"]],
                    "audio_processed_samples": len(audio), "audio_received_samples": len(audio),
                    "audio_buffered_samples": 0,
                    "decode_ms": (time.perf_counter() - started) * 1000,
                    "generated_tokens": sum(item["generated_tokens"] for item in generation_stats),
                    "generations": list(generation_stats),
                })
                continue
            elif operation in ("update", "finish"):
                if state is None or request.get("segment_id") != segment_id:
                    raise ValueError("No active streaming state for segment_id")
                if operation == "update":
                    delta = audio_from_request(request)
                    received += len(delta)
                    asr.streaming_transcribe(delta, state)
                else:
                    with temporary_safe_finish_prefix(asr.processor.tokenizer, state) as safe_prefix_adjustment:
                        asr.finish_streaming_transcribe(state)
            else:
                raise ValueError(f"Unknown op {operation!r}")

            raw_ids = asr.processor.tokenizer.encode(state._raw_decoded)
            emit({
                "ok": True, "op": operation, "segment_id": segment_id,
                "text": state.text, "detected_language": state.language,
                "raw_decoded": state._raw_decoded,
                "audio_processed_samples": len(state.audio_accum),
                "audio_buffered_samples": len(state.buffer),
                "audio_received_samples": received, "chunk_id": state.chunk_id,
                "decode_ms": (time.perf_counter() - started) * 1000,
                "prefix_tokens": generation_stats[-1]["prefix_tokens_used"] if generation_stats else 0,
                "total_reused_prefix_tokens": sum(item["prefix_tokens_used"] for item in generation_stats),
                "next_prefix_tokens": max(0, len(raw_ids) - state.unfixed_token_num)
                if state.chunk_id >= state.unfixed_chunk_num else 0,
                "generated_tokens": sum(item["generated_tokens"] for item in generation_stats),
                "generations": list(generation_stats),
                "safe_prefix_adjustment": safe_prefix_adjustment,
            })
        except Exception as error:
            traceback.print_exc(file=sys.stderr)
            emit({"ok": False, "op": request.get("op"),
                  "segment_id": request.get("segment_id"), "error": str(error),
                  "error_type": type(error).__name__,
                  "safe_prefix_adjustment": safe_prefix_adjustment})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
