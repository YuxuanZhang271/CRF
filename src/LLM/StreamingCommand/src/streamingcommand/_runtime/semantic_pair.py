"""Optional B versus A+B parsing, using unchanged ASR final boundaries.

Only one adjacent, unexecuted physical final is eligible as A. This is a
bounded semantic recovery experiment, not a learned utterance boundary model.
"""
from __future__ import annotations

import copy
import math

from .command_stream import CommandSession
from .command_models import H1_MODELS

LOGIC_OPTIONS = [
    {"id": "original", "label": "原有识别逻辑"},
    {"id": "semantic-pair", "label": "前后段联合解析（B 与 A＋B，实验）"},
]
MAX_GAP_MS = 1000
MAX_SPAN_MS = 8000
MAX_TEXT_CHARS = 500


def validate_logic(value, backend=None):
    """Require a recognized policy and a supported H1 clarification model for paired parsing."""
    if value not in {o['id'] for o in LOGIC_OPTIONS}:
        raise ValueError('Unknown recognition_logic')
    if value == 'semantic-pair' and (backend is None or
            backend.status.get('model_id') not in H1_MODELS):
        raise ValueError('前后段联合解析需要 H1 澄清模型')
    return value


def executable(result):
    """Check that a resolved result permits action and has no pending clarification."""
    return bool(result.get('execute') and result.get('resolved') and
                not result.get('clarification_required'))


def select_pair(current, joined):
    """Return a selection or abstain; never reinterpret probabilities as boundaries."""
    if current.get('clarification_required'):
        return 'current', 'current_requires_clarification'
    b, ab = executable(current), executable(joined)
    if ab and b and current['slots'] == joined['slots']:
        return 'joined', 'both_agree'
    if ab and not b:
        return None, 'joined_only_unconfirmed'
    if b or ab:
        return None, 'conflict'
    # Both nonexecuting: keep B's existing clarification/noncommand behavior.
    return 'current', 'neither_executes'


class PairedCommandSession(CommandSession):
    def __init__(self, *args, **kwargs):
        """Initialize the command scheduler with one optional predecessor for paired parsing."""
        super().__init__(*args, **kwargs)
        self._previous_unexecuted = None

    def submit(self, event):
        """Invalidate a predecessor when an empty final or a pending clarification makes pairing unsafe."""
        if event['type'] == 'transcript.final' and (not event['text'].strip() or self._pending_clarification):
            self._previous_unexecuted = None
        super().submit(event)

    def configure(self, candidates):
        """Apply the candidate update and discard a predecessor interpreted under the previous pool."""
        result = super().configure(candidates)
        self._previous_unexecuted = None
        return result

    async def clear_memory(self):
        """Clear the clarification state and forget the saved predecessor."""
        await super().clear_memory()
        self._previous_unexecuted = None

    async def feedback(self, *args):
        """Forget the predecessor before sending a clarification reply."""
        self._previous_unexecuted = None
        return await super().feedback(*args)

    def _prediction_ready(self, event, result, final):
        """Invalidate pairing state after skipped, reply, or error finals are processed."""
        # Empty/feedback finals bypass _parse_command. Invalidate when processed
        # as well as submitted: an earlier in-flight A may have filled the cache.
        if final and (result.get('skipped_reason') or result.get('clarification_reply_source')
                      or result['type'] == 'command.error'):
            self._previous_unexecuted = None

    def _prefix(self, event, job, history):
        """Return the eligible predecessor only when audio timing, text, and session state fit the pairing
        limits.
        """
        previous = self._previous_unexecuted
        if not previous or self._pending_clarification or self._reply_bindings.get(event['segment_id']):
            return None
        if (previous['candidate_version'] != job['candidate_version'] or
                previous['history'] != history or job['candidate_version'] != self.candidate_version):
            return None
        a = previous['event']
        # Missing audio anchors must not accidentally join arbitrary HTTP/text inputs.
        anchors = [a.get('audio_start_ms'), a.get('vad_estimated_speech_end_ms'),
                   event.get('audio_start_ms'), event.get('audio_end_ms')]
        if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in anchors):
            return None
        start_a, end_a, start_b, end_b = anchors
        gap = start_b - end_a  # preroll may include up to 200ms of the pause
        if not 0 <= gap <= MAX_GAP_MS or not 0 < end_b - start_a <= MAX_SPAN_MS:
            return None
        if len(a['text']) + len(event['text']) > MAX_TEXT_CHARS:
            return None
        return previous

    async def _parse_command(self, event, job, history, final):
        """Compare B and eligible A+B under the same history and commit at most one model result."""
        if not final:
            return await super()._parse_command(event, job, history, final)
        previous = self._prefix(event, job, history)
        self._previous_unexecuted = None
        if previous:
            result = await self.backend.parse_pair(event['text'], previous['event']['text'],
                job['candidates'], history, session_id=self.session_id)
            result['semantic_pair'].update(previous_segment_id=previous['event']['segment_id'],
                current_segment_id=event['segment_id'], max_gap_ms=MAX_GAP_MS,
                previous_text=previous['event']['text'],
                previous_result=previous['result'],
                gap_basis='current_audio_start_with_preroll_minus_previous_last_vad_voice')
        else:
            result = await super()._parse_command(event, job, history, final)
        # No rolling chains and no cache of already executable/clarified/old-config inputs.
        if (not previous and not executable(result) and not result.get('clarification_required')
                and not result.get('clarification_id') and not self._pending_clarification
                and job['candidate_version'] == self.candidate_version):
            self._previous_unexecuted = dict(event=copy.deepcopy(event),
                result=copy.deepcopy(result), history=copy.deepcopy(history),
                candidate_version=job['candidate_version'])
        return result
