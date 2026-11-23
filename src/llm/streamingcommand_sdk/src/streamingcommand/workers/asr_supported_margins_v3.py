"""Preserve microphone PCM when fixed ASR margins lack real quiet support."""
import numpy as np
from asr_consistent_features import canonical_pcm

POLICY_VERSION = 'supported-margins-v3'
MARGIN_SAMPLES = 2560  # 160ms at16kHz, unchanged canonical margins.
HOP_SAMPLES = 160     # Allow one10ms frontend hop of boundary tolerance.
MIN_QUIET_SAMPLES = MARGIN_SAMPLES - HOP_SAMPLES
MAX_EXTENSION_SAMPLES = MARGIN_SAMPLES + HOP_SAMPLES


def supported_margin_pcm(pcm):
    """Normalize margins only when the source already has enough quiet samples to support the adjustment."""
    pcm = np.asarray(pcm)
    normalized, candidate = canonical_pcm(pcm, 160, 160)
    existing_quiet = (candidate.get('source_start', 0) + len(pcm) -
                      candidate.get('source_end', len(pcm)))
    accepted = (candidate['changed'] and existing_quiet >= MIN_QUIET_SAMPLES
                and len(normalized) <= len(pcm) + MAX_EXTENSION_SAMPLES)
    result = normalized if accepted else pcm.copy()
    return result, {
        'policy': POLICY_VERSION, 'changed': bool(accepted),
        'reason': ('supported_outer_quiet' if accepted else
                   'no_above_threshold_samples' if not candidate['changed'] else
                   'insufficient_outer_quiet'),
        'existing_outer_quiet_samples': existing_quiet,
        'minimum_existing_outer_quiet_ms': 150,
        'maximum_extension_ms': 170, 'hop_tolerance_ms': 10,
        'source_samples': len(pcm), 'candidate_samples': len(normalized),
        'model_input_samples': len(result), 'candidate_transform': candidate,
        'retained_active_pcm_bit_identical': True,
    }
