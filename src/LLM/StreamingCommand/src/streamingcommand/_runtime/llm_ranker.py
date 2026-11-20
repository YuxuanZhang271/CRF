from __future__ import annotations
import json
from pathlib import Path
from functools import lru_cache
from collections import OrderedDict
import torch
from torch import nn
SLOTS=("I","T","P")
class QwenEncoder:
    """Compact causal layout: history/U before candidates, queries after all candidates."""
    def __init__(self, tokenizer, max_tokens=512):
        """Record the tokenizer, pad token, and strict token budget for compact command inputs."""
        self.tokenizer, self.max_tokens = tokenizer, max_tokens
        self.pad_id = tokenizer.pad_token_id or tokenizer.eos_token_id

    @lru_cache(maxsize=100_000)
    def tokens(self, text):
        """Cache token IDs for recurring prompt and candidate text without adding special tokens."""
        return tuple(self.tokenizer.encode(text, add_special_tokens=False))

    def encode(self, record):
        """Build the causal command prompt and record candidate endpoints, query positions, and labels."""
        ids = list(self.tokens("Parse command using given candidates.\n"))
        for key, value in record["history"].items():
            ids.extend(self.tokens(key + "=" + value + "\n"))
        ids.extend(self.tokens("U=" + record["utterance"] + "\n"))
        positions, labels = [], [int(record["command"] == "Y")]
        for slot in SLOTS:
            ids.extend(self.tokens(slot + "="))
            positions.append([])
            for index, value in enumerate(record["candidates"][slot]):
                if index:
                    ids.extend(self.tokens(","))
                piece = self.tokens(value)
                if not piece:
                    raise ValueError("Empty encoded candidate")
                ids.extend(piece)
                positions[-1].append(len(ids) - 1)
            ids.extend(self.tokens("\n"))
            value = record["slots"][slot]
            labels.append(0 if value is None else record["candidates"][slot].index(value) + 1)
        queries = []
        for slot in ("C", *SLOTS):
            ids.extend(self.tokens(slot + "?\n"))
            queries.append(len(ids) - 1)
        if len(ids) > self.max_tokens:
            raise ValueError(f"{len(ids)} tokens exceeds {self.max_tokens}; no silent truncation")
        return {"record": record, "ids": ids, "positions": positions, "queries": queries, "labels": labels}

    def collate(self, examples):
        """Pad token and candidate dimensions separately and produce masks for the ranker."""
        batch_size = len(examples)
        length = max(len(x["ids"]) for x in examples)
        k = max(len(p) for x in examples for p in x["positions"])
        ids = torch.full((batch_size, length), self.pad_id, dtype=torch.long)
        mask = torch.zeros_like(ids)
        positions = torch.zeros((batch_size, 3, k), dtype=torch.long)
        candidate_mask = torch.zeros_like(positions, dtype=torch.bool)
        for i, example in enumerate(examples):
            n = len(example["ids"])
            ids[i, :n] = torch.tensor(example["ids"])
            mask[i, :n] = 1
            for slot, pos in enumerate(example["positions"]):
                positions[i, slot, :len(pos)] = torch.tensor(pos, dtype=torch.long)
                candidate_mask[i, slot, :len(pos)] = True
        return {
            "input_ids": ids, "attention_mask": mask,
            "candidate_positions": positions, "candidate_mask": candidate_mask,
            "query_positions": torch.tensor([x["queries"] for x in examples]),
            "labels": torch.tensor([x["labels"] for x in examples]),
        }

class SharedCandidateHead(nn.Module):
    def __init__(self, hidden_size, bottleneck=128):
        """Allocate the shared slot scorer, unresolved vectors, and binary command head."""
        super().__init__()
        self.slot_embedding = nn.Parameter(torch.zeros(3, hidden_size))
        self.unresolved = nn.Parameter(torch.empty(3, hidden_size))
        nn.init.normal_(self.unresolved, std=0.02)
        self.scorer = nn.Sequential(nn.LayerNorm(hidden_size * 3), nn.Linear(hidden_size * 3, bottleneck),
                                    nn.GELU(), nn.Linear(bottleneck, 1))
        self.command_head = nn.Sequential(nn.LayerNorm(hidden_size), nn.Linear(hidden_size, 2))

    def forward(self, hidden, candidate_positions, candidate_mask, query_positions):
        """Score valid candidate hidden states and the unresolved option against each slot query."""
        b, _, d = hidden.shape
        queries = hidden.gather(1, query_positions[..., None].expand(-1, -1, d)).float()
        flat_pos = candidate_positions.flatten(1)
        candidates = hidden.gather(1, flat_pos[..., None].expand(-1, -1, d))
        candidates = candidates.reshape(b, 3, -1, d).float()
        candidates = torch.cat((self.unresolved[None, :, None, :].expand(b, -1, -1, -1), candidates), dim=2)
        q = (queries[:, 1:] + self.slot_embedding)[..., None, :].expand_as(candidates)
        scores = self.scorer(torch.cat((q, candidates, q * candidates), dim=-1)).squeeze(-1).float()
        valid = torch.cat((torch.ones((b, 3, 1), device=candidate_mask.device, dtype=torch.bool), candidate_mask), dim=-1)
        scores = scores.masked_fill(~valid, -1e4)
        return {"command": self.command_head(queries[:, 0]).float(), "slots": scores}

class QwenCandidateRanker(nn.Module):
    def __init__(self, language_model):
        """Attach the shared candidate head to the provided Qwen language model."""
        super().__init__()
        self.language_model = language_model
        self.head = SharedCandidateHead(language_model.config.hidden_size)

    @property
    def backbone(self):
        """Expose the underlying decoder for both plain and PEFT-wrapped language models."""
        lm = self.language_model
        return (lm.get_base_model() if hasattr(lm, "get_base_model") else lm).model

    def forward(self, input_ids, attention_mask, candidate_positions, candidate_mask, query_positions):
        """Encode the prompt once and score commands and slots, using a capture-compatible mask for one-row
        inference.
        """
        if not self.training and input_ids.shape[0] == 1:
            # Transformers skips an all-valid SDPA mask in eager mode, but not
            # during CUDA capture. Supply the same explicit causal mask to both
            # paths so replay comparisons use the same attention kernel.
            length = input_ids.shape[1]
            causal = torch.ones((length, length), device=input_ids.device, dtype=torch.bool).tril()
            causal = causal[None, None] & attention_mask[:, None, None, :].bool()
            backbone_mask = {"full_attention": causal, "linear_attention": attention_mask}
        else:
            backbone_mask = attention_mask
        hidden = self.backbone(input_ids=input_ids, attention_mask=backbone_mask, use_cache=False).last_hidden_state
        return self.head(hidden, candidate_positions, candidate_mask, query_positions)

def load_qwen(model_path, init_adapter=None, device="cuda:0", local_files_only=True):
    """Load the local tokenizer and base model with the configured dtype and optionally merge an initial
    adapter.
    """
    from transformers import AutoTokenizer, Qwen3_5ForCausalLM
    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=local_files_only)
    lm = Qwen3_5ForCausalLM.from_pretrained(model_path, dtype=torch.float16 if str(device).startswith("cuda:") else torch.float32,
            attn_implementation="sdpa", local_files_only=local_files_only)
    if init_adapter:
        from peft import PeftModel
        lm = PeftModel.from_pretrained(lm, str(init_adapter)).merge_and_unload()
    # Freeze the merged AR semantic initialization; add fresh LoRA only after the latency gate.
    lm.requires_grad_(False)
    model = QwenCandidateRanker(lm).to(device).eval()
    model.head.float()
    return model, QwenEncoder(tokenizer)

class CudaGraphRunner:
    """One fixed input shape. All input tensors, including positions/masks, are copied on replay."""
    def __init__(self, model, batch):
        """Warm up and capture a fixed-shape inference graph on the model's CUDA device."""
        self.static = {key: value.clone() for key, value in batch.items()}
        stream = torch.cuda.Stream(device=next(model.parameters()).device)
        stream.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(stream), torch.inference_mode(), torch.autocast("cuda", dtype=torch.float16, cache_enabled=False):
            for _ in range(5):
                model(**self.static)
        torch.cuda.current_stream().wait_stream(stream)
        self.graph = torch.cuda.CUDAGraph()
        with torch.inference_mode(), torch.autocast("cuda", dtype=torch.float16, cache_enabled=False), torch.cuda.graph(self.graph):
            self.output = model(**self.static)

    def __call__(self, **batch):
        """Copy each request tensor into static buffers and replay the captured graph after checking
        shapes.
        """
        for key, value in batch.items():
            if value.shape != self.static[key].shape:
                raise ValueError("CUDA graph shape mismatch")
            self.static[key].copy_(value)
        self.graph.replay()
        return self.output

class SingleCommandGraphCache:
    """Warm CUDA graphs for exact request shapes; changing values are copied on every replay.

    Cache misses capture/warm a new graph and are outside the warm latency claim.
    For this notebook's single-threaded, batch-one inference only.
    """
    def __init__(self, model, max_shapes=16):
        """Store a model and an eviction limit for exact-shape command graphs."""
        self.model, self.max_shapes = model, max_shapes
        self.runners = OrderedDict()

    @torch.inference_mode()
    def __call__(self, **batch):
        """Reuse or capture a batch-one graph keyed by tensor shapes and dtypes, evicting the least recent
        graph.
        """
        if self.model.training or batch["input_ids"].shape[0] != 1:
            raise ValueError("Graph cache requires eval mode and one command")
        torch.cuda.set_device(next(self.model.parameters()).device)
        key = tuple((name, tuple(value.shape), value.dtype) for name, value in sorted(batch.items()))
        if key not in self.runners:
            self.runners[key] = CudaGraphRunner(self.model, batch)
            if len(self.runners) > self.max_shapes:
                self.runners.popitem(last=False)
        self.runners.move_to_end(key)
        return self.runners[key](**batch)

def restore_checkpoint(model, checkpoint, trainable=False):
    """Restore the adapter and head, preserving trainable weights or merging the adapter for inference."""
    from peft import PeftModel
    from safetensors.torch import load_file
    checkpoint = Path(checkpoint)
    device = next(model.parameters()).device
    if isinstance(model, QwenCandidateRanker):
        model.language_model = PeftModel.from_pretrained(model.language_model, str(checkpoint / "adapter"),
                                                       is_trainable=trainable)
        model.head.load_state_dict(load_file(str(checkpoint / "head.safetensors"), device=str(device)))
        if trainable:
            model.head.requires_grad_(True)
            for parameter in model.parameters():
                if parameter.requires_grad:
                    parameter.data = parameter.data.float()
        else:
            model.language_model = model.language_model.merge_and_unload()
    else:
        if trainable:
            raise ValueError("Trainable checkpoint continuation is currently implemented for Qwen")
        model.decision_model.encoder = PeftModel.from_pretrained(model.decision_model.encoder, str(checkpoint / "adapter"))
        model.decision_model.load_state_dict(load_file(str(checkpoint / "head.safetensors"), device=str(device)), strict=False)
        model.decision_model.encoder = model.decision_model.encoder.merge_and_unload()
    model.eval()
    return model
