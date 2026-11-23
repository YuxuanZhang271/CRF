"""One-forward parser for the published flat LoRA + I/T/P head checkpoint."""
import json
from pathlib import Path
import time

import torch
from safetensors.torch import load_file

from ..config import PUBLISHED_MODEL
from .llm_ranker import SLOTS, SingleCommandGraphCache, load_qwen


class PublishedCommandRuntime:
    def __init__(self, checkpoint, base_model, device, use_cuda_graphs):
        """Restore the published flat LoRA and candidate head and record checkpoint identity for status."""
        checkpoint = Path(checkpoint)
        meta = json.loads((checkpoint / "experiment.json").read_text(encoding="utf-8"))
        if meta.get("adapter_layout") != "single_lora" or meta.get("initial_ar_adapter"):
            raise ValueError("The published parser requires a single flat LoRA on the raw Qwen base")
        self.device = "cuda:0" if device.startswith("cuda:") else device
        model, self.encoder = load_qwen(base_model, init_adapter=checkpoint, device=self.device)
        model.head.load_state_dict(load_file(str(checkpoint / "head.safetensors")))
        model.requires_grad_(False).eval()
        self.model = SingleCommandGraphCache(model) if use_cuda_graphs and self.device == "cuda:0" else model
        self.identity = dict(parser_checkpoint=str(checkpoint), base_model_id=meta.get("base_model"),
                             checkpoint_step=meta["global_step"], source_best_step=meta.get("source_best_step"),
                             lora_rank=meta.get("lora_rank"), adapter_layout="single_lora")

    def warmup(self, candidates):
        """Prime the published ranker with a representative short command and candidate pool."""
        self.parse_turn("move left", candidates, {})

    @torch.inference_mode()
    def parse_turn(self, utterance, candidates, history, *, session_id=None, commit=False):
        """Rank C/I/T/P candidates from one backbone pass and distinguish unresolved slots from business
        none.
        """
        started = time.perf_counter()
        record = dict(utterance=utterance, history=history, candidates=candidates,
                      command="N", slots=dict.fromkeys(SLOTS))
        batch = self.encoder.collate([self.encoder.encode(record)])
        inputs = {k: v.to(self.device) for k, v in batch.items() if k != "labels"}
        with torch.autocast("cuda", dtype=torch.float16, enabled=self.device == "cuda:0", cache_enabled=False):
            out = self.model(**inputs)

        def rank(logits, labels):
            """Return the probability of each valid label without including masked padding choices."""
            probs = logits[:len(labels)].float().softmax(-1).cpu().tolist()
            return [{"value": label, "probability": prob} for label, prob in zip(labels, probs)]

        scores = {"C": rank(out["command"][0], ["N", "Y"])}
        command = ("N", "Y")[out["command"][0].argmax().item()]
        slots = {}
        for i, slot in enumerate(SLOTS):
            values = candidates[slot]
            logits = out["slots"][0, i, :len(values) + 1]
            scores[slot] = rank(logits, ["__unresolved__", *values])
            index = logits.argmax().item()
            slots[slot] = values[index - 1] if command == "Y" and index else None
        resolved = command == "Y" and all(value is not None for value in slots.values())
        raw = "C=N" if command == "N" else "C=Y;" + ";".join(f"{k}={v or '?'}" for k, v in slots.items())
        return dict(command=command, slots=slots, resolved=resolved, execute=resolved, scores=scores,
                    status="EXECUTE" if resolved else "NON_COMMAND" if command == "N" else "UNRESOLVED",
                    raw_output=raw, clarification_required=False, backbone_calls=1, is_fake=False,
                    model_id=PUBLISHED_MODEL, backend="qwen-prefill", parse_ms=round((time.perf_counter()-started)*1000, 3))
