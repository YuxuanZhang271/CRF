"""Consume the entire available causal prefix in one SDK generation per update."""
def complete_update(original, model, audio, state):
    """Temporarily resize the SDK chunk to consume all available causal PCM and verify sample coverage."""
    nominal = state.chunk_size_samples
    available = len(state.buffer) + len(audio)
    if available <= 0:
        return original(model, audio, state), None
    before = len(state.audio_accum)
    state.chunk_size_samples = available
    try:
        result = original(model, audio, state)
    finally:
        state.chunk_size_samples = nominal
    if len(state.audio_accum) != before + available or len(state.buffer):
        raise AssertionError('Complete-prefix update lost or delayed audio samples')
    return result, {'available_samples': available, 'nominal_chunk_samples': nominal,
                    'generations_expected': 1, 'audio_accumulated_samples': len(state.audio_accum)}
