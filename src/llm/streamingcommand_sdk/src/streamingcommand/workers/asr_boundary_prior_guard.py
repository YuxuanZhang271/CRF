"""Do not let a domain prior override an already preferred text boundary."""
import inspect

# Verified against both R17 banks' byte-identical tokenizer, not model answers.
BOUNDARIES = (151645, 13, 11, 0, 30, 26, 25, 1773, 3837, 6313, 11319, 24968, 5122, 198)


def guarded_apply(original, processor, logits):
    """Keep original logits for rows where a boundary token was already ranked first before applying a
    prior.
    """
    if not processor.biases:
        return original(processor, logits)
    import torch
    unmodified = logits.clone()
    best = unmodified.argmax(dim=-1)
    boundary = best == BOUNDARIES[0]
    for token in BOUNDARIES[1:]:
        boundary = boundary | (best == token)
    biased = original(processor, logits)
    return torch.where(boundary.unsqueeze(-1), unmodified, biased)


def install():
    """Install the vLLM boundary guard once and replace an existing narrower EOS-only guard."""
    from vllm.v1.sample.logits_processor.builtin import LogitBiasLogitsProcessor
    original = LogitBiasLogitsProcessor.apply
    if getattr(original, '_phono_boundary_prior_guard', False):
        return
    if getattr(original, '_phono_eos_prior_guard', False):
        original = inspect.getclosurevars(original).nonlocals['original']

    def apply(self, logits):
        """Apply the boundary guard around the original vLLM bias operation."""
        return guarded_apply(original, self, logits)

    apply._phono_eos_prior_guard = True
    apply._phono_boundary_prior_guard = True
    LogitBiasLogitsProcessor.apply = apply
