"""Preserve the ASR's own end token when applying a domain logit prior.

This is a sampler operation inside the existing generation, not a second pass
or an output filter. Requests without a token prior use the original operation.
"""
EOS_TOKEN_ID = 151645


def guarded_apply(original, processor, logits):
    """Restore original logits in rows where EOS led before the logit prior."""
    if not processor.biases:
        return original(processor, logits)
    import torch
    unmodified = logits.clone()
    ending = (unmodified.argmax(dim=-1) == EOS_TOKEN_ID).unsqueeze(-1)
    biased = original(processor, logits)
    return torch.where(ending, unmodified, biased)


def install():
    """Wrap the vLLM logit-bias processor once with EOS preservation."""
    from vllm.v1.sample.logits_processor.builtin import LogitBiasLogitsProcessor
    original = LogitBiasLogitsProcessor.apply
    if getattr(original, '_phono_eos_prior_guard', False):
        return

    def apply(self, logits):
        """Apply the EOS guard using the captured unwrapped logit-bias method."""
        return guarded_apply(original, self, logits)

    apply._phono_eos_prior_guard = True
    LogitBiasLogitsProcessor.apply = apply
