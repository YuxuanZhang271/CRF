"""Acoustic-language-gated version of unpublished endpoint continuation."""
from .endpoint_continuation import EndpointContinuationSession


class YueEndpointContinuationSession(EndpointContinuationSession):
    def _resume(self, event):
        """Permit continuation only with completed Yue evidence already available for the tentative final."""
        job = self._tentative
        task = getattr(job, 'language_prefetch', None)
        # Reuse only a completed query already requested by the ordinary ASR
        # endpoint. Never wait for it or start a new language/ASR pass here.
        if task is None or not task.done() or task.cancelled():
            return
        try:
            hint = task.result()
        except Exception:
            return
        pair = hint.get('temporal_language_queries') or {}
        current = pair.get('current') or hint
        rolling = pair.get('rolling') or {}
        current_yue = (current.get('native_language_tag') == 'yue'
                       and current.get('language_probability', 0) >= .7)
        rolling_yue = (rolling.get('native_language_tag') == 'yue'
                       and rolling.get('language_probability', 0) >= .9)
        if not (current_yue or rolling_yue):
            return
        prior = self.stats['endpoint_continuations']
        super()._resume(event)
        if self.stats['endpoint_continuations'] != prior:
            event['continuation_language_evidence'] = dict(
                source='existing_completed_acoustic_query', current_yue=current_yue,
                rolling_yue=rolling_yue, extra_queries=0, extra_wait_ms=0)
