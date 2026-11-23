"""Session transport for the evaluated single-Qwen clarification checkpoint.

The model predicts Q and both proposals. No back matcher, inverse-action rule,
reply model, or stored user convention is added by this adapter.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path, PureWindowsPath
import sys
import time
import uuid

from .command_models import (ROOT, SINGLE_MODEL, H1_MODELS, SINGLE_MODELS,
                             SINGLE_CLARIFICATION_CHECKPOINT, MODEL_SPECS, model_candidates, model_history)


class SingleCommandRuntime:
    def __init__(self, checkpoint=SINGLE_CLARIFICATION_CHECKPOINT, *, model_id=SINGLE_MODEL, predictor=None, base_model_dir=None, use_cuda_graphs=True):
        """Verify checkpoint files and load the selected unmerged adapter, command head, and clarification
        head.
        """
        self.checkpoint = Path(checkpoint).resolve()
        self.model_id = model_id
        if model_id not in SINGLE_MODELS:
            raise ValueError("Unknown model schema")
        self.session_id = None
        self.clear()
        self.identity = dict(base_model_id="Qwen/Qwen3.5-0.8B", adapter_layout="single_lora",
                             lora_rank=16, backbones=1, adapters=1, memory_policy="none",
                             parser_checkpoint=str(self.checkpoint))
        integrity_path = self.checkpoint / "integrity.json"
        if integrity_path.exists():
            review = json.loads(integrity_path.read_text(encoding="utf-8"))
            for filename, expected in review["weights"].items():
                if PureWindowsPath(filename).anchor or '\\' in filename or '..' in filename.split('/'):
                    raise ValueError(f"Invalid checkpoint manifest path: {filename}")
                if hashlib.sha256((self.checkpoint / filename).read_bytes()).hexdigest() != expected:
                    raise ValueError(f"Checkpoint hash mismatch: {filename}")
            self.identity.update(checkpoint_hashes_verified=True, max_history_depth=1, adapter_merged=False)
        if predictor is None:
            from . import llm_core as core
            from .llm_ranker import SingleCommandGraphCache, load_qwen, restore_checkpoint
            from .paths import model_path
            torch = core.torch
            device = __import__("os").environ.get("STREAMINGCOMMAND_DEVICE", "cpu")
            device = "cuda:0" if device.startswith("cuda:") else device
            parser, encoder = load_qwen(base_model_dir or model_path("llm-base"), None, device)
            restore_checkpoint(parser, self.checkpoint, trainable=True)
            self.model = core.UnifiedModel(parser)
            self.model.clarify_head.load_state_dict(torch.load(self.checkpoint / "clarify_head.pt", map_location=device, weights_only=True))
            self.model.requires_grad_(False).eval()
            self.core, self.encoder, self.device = core, core.ClarificationEncoder(encoder.tokenizer), device
            self.graphs = SingleCommandGraphCache(self.model, max_shapes=16) if device.startswith("cuda:") and use_cuda_graphs else self.model
            meta = json.loads((self.checkpoint / "experiment.json").read_text(encoding="utf-8"))
            self.identity.update(checkpoint_step=meta["step"], checkpoint_epoch=meta["epoch"], adapter_merged=False)
            predictor = self._predict
        self.predictor = predictor

    def _predict(self, text, candidates, history, pending):
        """Encode a turn with its current clarification stage and decode both proposals from one model
        output.
        """
        torch = self.core.torch
        row = dict(id="demo", utterance=text, candidates=candidates, history=history,
                   clarification_input=pending, command="N", slots=dict.fromkeys("ITP"),
                   clarification_required=False, alternative_slots=dict.fromkeys("ITP"))
        with torch.inference_mode():
            batch = self.encoder.collate([self.encoder.encode(row)])
            # Match assessed padding; pools retain their members and order.
            pad = torch.nn.functional.pad
            extra = (-batch["input_ids"].shape[1]) % 32
            batch["input_ids"] = pad(batch["input_ids"], (0, extra), value=self.encoder.pad_id)
            batch["attention_mask"] = pad(batch["attention_mask"], (0, extra))
            extra = max(0, 16 - batch["candidate_positions"].shape[-1])
            for key in ("candidate_positions", "candidate_mask"):
                batch[key] = pad(batch[key], (0, extra))
            inputs = {k: v.to(self.device) for k, v in batch.items() if k != "labels"}
            with torch.autocast("cuda", dtype=torch.float16, enabled=self.device.startswith("cuda:")):
                out = self.graphs(**inputs)
            return self.core.decode(out, [row], include_scores=True)[0]

    def warmup(self, candidates):
        """Exercise empty and populated history and clarification shapes before serving live turns."""
        histories = ({}, {"H1": "I=bend;T=none;P=left"},
                     {"H2": "I=bend;T=none;P=left", "H1": "I=rotate;T=none;P=right"})
        for history in histories:
            history = model_history(history, self.model_id)
            for pending in (False, True):
                for text in ("Bend left.", "Could we go a little back now?", "I mean pull back."):
                    self.predictor(text, model_candidates(candidates, self.model_id), history, pending)
        self.clear()

    def clear(self):
        """Forget the current pending clarification and its reply token."""
        self.pending = None
        self.clarification_id = None

    def _run(self, text, candidates, history, pending):
        """Normalize one prediction for the streaming protocol, including its empty-P execution rules."""
        started = time.perf_counter()
        result = self.predictor(text, candidates, history, pending)
        if not candidates["P"]:
            # Match the reviewed give joint trainer's absent-P contract.
            # All C/I/T predictions are retained; no utterance rules are added.
            result = copy.deepcopy(result)
            slots = result["slots"]
            result["execute"] = (result["command"] == "Y" and not result["clarification_required"]
                and slots["I"] is not None and (slots["I"] in ("stop", "reset") or slots["T"] is not None))
            result["next_clarification_input"] = bool(result["clarification_required"] or (pending and not result["execute"]))
            result.setdefault("scores", {}).setdefault("primary", {})["P"] = {"disabled": True}
        clarify, execute = result["clarification_required"], result["execute"]
        status = "CLARIFY" if clarify else "EXECUTE" if execute else "NON_COMMAND" if result["command"] == "N" else "UNRESOLVED"
        scores = result.get("scores", {})
        slots = result["slots"]
        def rendered(slot):
            """Format an unresolved slot or disabled field for the diagnostic raw output."""
            if slots[slot] is not None:
                return slots[slot]
            inactive = not candidates["P"] and (slot == "P" or (slot == "T" and slots["I"] in ("stop", "reset")))
            return "null" if inactive else "?"
        raw = "C=N" if result["command"] == "N" else "C=Y;" + ";".join(f"{s}={rendered(s)}" for s in "ITP")
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        return {**result, "status": status, "resolved": bool(execute and not clarify),
                "raw_output": raw, "scores": {"C": [{"value": k, "probability": v} for k, v in scores.get("C", {}).items()],
                    **scores.get("primary", {}), "clarify": scores.get("clarify", {}),
                    "alternative": scores.get("alternative", {})},
                "memory_active": False, "saved": False, "session_memory": False,
                "feedback_allowed": False, "clarification_id": None,
                "clarification_input": pending, "backbone_calls": 1,
                "backend": "qwen-prefill-single-clarification", "model_id": self.model_id,
                "is_fake": False, "parse_ms": elapsed, "model_ms": elapsed}

    def _commit(self, result, candidates, history):
        """Keep or clear the pending clarification and issue a fresh token for the next reply."""
        if result["next_clarification_input"]:
            # A rejected/chat reply keeps the earlier question, without changing
            # C=N into an execution or a new semantic model decision.
            question = result.get("question") or (self.pending or {}).get("question") or "What do you mean?"
            self.pending = dict(candidates=copy.deepcopy(candidates), history=copy.deepcopy(history), question=question)
            self.clarification_id = uuid.uuid4().hex
            result.update(feedback_allowed=True, clarification_id=self.clarification_id, pending_question=question)
        else:
            self.clear()

    def parse_turn(self, utterance, candidates, history, *, session_id=None, commit=False):
        """Evaluate one text turn and mutate the active session only when the request commits."""
        candidates = model_candidates(candidates, self.model_id)
        history = model_history(history, self.model_id)
        # Stateless requests and ASR previews never mutate a recording's state.
        same_session = session_id is not None and session_id == self.session_id
        pending = bool(same_session and self.pending and self.pending["candidates"] == candidates)
        result = self._run(utterance, candidates, history, pending)
        if commit and session_id is not None:
            if not same_session:
                self.clear()
                self.session_id = session_id
            elif self.pending and self.pending["candidates"] != candidates:
                self.clear()
            self._commit(result, candidates, history)
        return result

    def parse_pair(self, utterance, previous, candidates, history, *, session_id):
        """Compare the current and adjacent-context interpretations using one frozen history snapshot."""
        from .semantic_pair import select_pair
        if self.model_id not in H1_MODELS:
            raise ValueError('Paired parsing requires an H1 model')
        if not previous.strip() or not utterance.strip() or len(previous) + len(utterance) > 500:
            raise ValueError('Invalid paired text')
        candidates = model_candidates(candidates, self.model_id)
        history = model_history(history, self.model_id)
        if session_id == self.session_id and self.pending:
            raise ValueError('Paired parsing cannot consume a pending clarification')
        started = time.perf_counter()
        joined_text = previous + ' ' + utterance
        current = self._run(utterance, candidates, history, False)
        joined = self._run(joined_text, candidates, history, False)
        selected, reason = select_pair(current, joined)
        result = copy.deepcopy(joined if selected == 'joined' else current)
        if selected is None:
            # Preserve raw model slots/scores in both evidence branches; the gate
            # withholds execution and does not fabricate a model clarification.
            result.update(execute=False, resolved=False, status='BOUNDARY_CONFLICT',
                          next_clarification_input=False, clarification_required=False)
        else:
            if self.session_id != session_id:
                self.clear()
                self.session_id = session_id
            self._commit(result, candidates, history)
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        result.update(backbone_calls=2, parse_ms=elapsed, model_ms=elapsed,
            semantic_input=joined_text if selected == 'joined' else utterance,
            semantic_pair=dict(selected=selected, reason=reason, current=current, joined=joined,
                               joined_text=joined_text, history=copy.deepcopy(history),
                               inference_calls=2, policy='unexecuted_adjacent_B_vs_AB_v1'))
        return result

    def feedback(self, session_id, reply, clarification_id):
        """Resolve a matching clarification token against the session's saved candidates and history."""
        if (session_id != self.session_id or not self.pending or
                not clarification_id or clarification_id != self.clarification_id):
            raise ValueError("澄清已过期；请回答当前问题")
        candidates, history = self.pending["candidates"], self.pending["history"]
        result = self._run(reply, candidates, history, True)
        self._commit(result, candidates, history)
        return result
