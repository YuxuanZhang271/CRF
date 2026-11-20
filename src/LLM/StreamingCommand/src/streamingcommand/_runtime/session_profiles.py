"""Effective endpoint policy, shared by recordings and the WebSocket session."""


def endpoint_policy(asr, endpoint_ms):
    """Describe whether the requested silence threshold fits the ASR profile's continuation bound."""
    requested = asr.status.get('endpoint_commit_policy')
    enabled = requested == 'yue_observed_continuation_380ms' and endpoint_ms <= 380
    return dict(name=requested if enabled else 'native_segment_final',
                continuation_enabled=enabled, continuation_gap_ms=380 if enabled else None,
                extra_wait_ms=0,
                native_reason='endpoint_exceeds_continuation_bound' if requested and not enabled else None)
