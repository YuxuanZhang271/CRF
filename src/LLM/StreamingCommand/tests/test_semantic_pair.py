"""Boundary/history invariants; scripted predictions are not semantic accuracy."""
import copy
import json

import httpx
import pytest

from streamingcommand._runtime.semantic_pair import PairedCommandSession, validate_logic
from streamingcommand._runtime.command_models import NEW_MODEL, NEW_CLARIFICATION_CHECKPOINT
from streamingcommand._runtime.single_command_runtime import SingleCommandRuntime
POOL={"I":["bend","self rotate","move","stop","reset","track"],"T":["none","scope"],"P":["none","left","right","forward","backward"]}
HISTORY={"H1":"I=bend;T=none;P=left"}
def event(text, segment=1, revision=1, final=False):
    """Construct a transcript update with a controlled segment ID, revision, and finality."""
    return {"type": "transcript.final" if final else "transcript.partial", "segment_id": segment,
            "revision": revision, "text": text, "is_final": final}


class Predictions:
    def __init__(self, mapping):
        """Store scripted predictions and initialize a trace of model calls."""
        self.mapping, self.calls = mapping, []

    def __call__(self, text, pool, history, pending):
        """Record a frozen history snapshot and return the scripted action or clarification for a prompt."""
        self.calls.append((text, copy.deepcopy(history), pending))
        value = self.mapping.get(text)
        q = value == 'Q'
        return dict(command='Y' if value else 'N',
            slots=dict(I='bend', T='none', P=value) if value and not q else dict.fromkeys('ITP'),
            alternative_slots=dict.fromkeys('ITP'), clarification_required=q,
            execute=bool(value and not q), next_clarification_input=q,
            question='Which direction?' if q else None)


def runtime(mapping):
    """Create the real clarification runtime with an injected deterministic predictor."""
    predict = Predictions(mapping)
    return SingleCommandRuntime(NEW_CLARIFICATION_CHECKPOINT, model_id=NEW_MODEL, predictor=predict), predict


def test_pair_runs_two_frozen_history_views_and_commits_chosen_result_once():
    """Verify identical history inputs for both interpretations and a single commitment of the paired
    result.
    """
    model, predict = runtime({'B': 'left', 'A B': 'left'})
    output = model.parse_pair('B', 'A', POOL, HISTORY, session_id='s')
    assert predict.calls == [('B', HISTORY, False), ('A B', HISTORY, False)]
    assert output['semantic_pair']['selected'] == 'joined'
    assert output['resolved'] and output['backbone_calls'] == 2
    assert model.session_id == 's' and model.pending is None


@pytest.mark.parametrize('mapping,reason', [({'A B': 'left'}, 'joined_only_unconfirmed'),
    ({'B': 'left', 'A B': 'right'}, 'conflict'), ({'B': 'left'}, 'conflict'),
    ({'B': 'Q', 'A B': 'left'}, 'current_requires_clarification'), ({}, 'neither_executes')])
def test_pair_disagreement_does_not_vote_or_ignore_negating_prefix(mapping, reason):
    """Check that agreement and clarification rules govern conflicts, joined-only results, and no-action
    cases.
    """
    model, predict = runtime(mapping)
    result = model.parse_pair('B', 'A', POOL, HISTORY, session_id='s')
    assert result['semantic_pair']['reason'] == reason
    assert len(predict.calls) == 2
    if reason == 'conflict':
        assert not result['execute'] and not result['resolved'] and model.pending is None
        assert result['semantic_pair']['current']['execute']
    if reason == 'joined_only_unconfirmed':
        assert not result['execute'] and not result['resolved']
        assert result['semantic_pair']['joined']['execute']
    if reason == 'current_requires_clarification':
        assert result['clarification_id'] and model.pending


class Backend:
    status = {'model_id': NEW_MODEL, 'max_history_depth': 1}
    default_candidates = POOL

    def __init__(self, mapping):
        """Wrap the scripted runtime in the async backend interface used by the command session."""
        self.model, self.predict = runtime(mapping)

    # Delegate a single-turn request to the real runtime with scripted predictions.
    async def parse_turn(self, *args, **kwargs): return self.model.parse_turn(*args, **kwargs)
    # Delegate a paired request to the real runtime with scripted predictions.
    async def parse_pair(self, *args, **kwargs): return self.model.parse_pair(*args, **kwargs)


def final(text, segment, start=0, end=600):
    """Construct a final transcript with explicit acoustic anchors for pairing eligibility."""
    return {**event(text, segment=segment, final=True), 'audio_start_ms': start,
            'audio_end_ms': end, 'vad_estimated_speech_end_ms': end - 240}


@pytest.mark.asyncio
@pytest.mark.parametrize('invalidation', ['none', 'executed', 'timeout', 'configure', 'empty', 'missing_anchors', 'clarification'])
async def test_session_only_pairs_adjacent_unexecuted_final_and_writes_one_H1(invalidation):
    """Check predecessor invalidation across execution, timeout, candidate changes, empty input, and
    clarification.
    """
    mapping = {'B': 'left', 'A B': 'left'}
    if invalidation == 'executed': mapping['A'] = 'right'
    if invalidation == 'clarification': mapping['A'] = 'Q'
    backend, emitted = Backend(mapping), []
    # Collect emitted semantic events for predecessor and H1 assertions.
    async def emit(value): emitted.append(value)
    session = PairedCommandSession(backend, emit, POOL, HISTORY)
    try:
        a, b = final('A', 1), final('B', 3, 700, 1200)
        if invalidation == 'missing_anchors': a.pop('audio_start_ms')
        session.submit(a)
        await session.finish()
        if invalidation == 'configure': session.configure(POOL)
        if invalidation == 'timeout': b.update(audio_start_ms=5000, audio_end_ms=5600)
        if invalidation == 'empty':
            session.submit(final('', 2, 610, 680))
            await session.finish()
        # Pending questions must route to feedback, not paired ordinary parsing.
        if invalidation == 'clarification':
            assert session._prefix(b, {'candidate_version': 0}, HISTORY) is None
            return
        session.submit(b)
        await session.finish()
        result = emitted[-1]
        assert ('semantic_pair' in result) == (invalidation == 'none')
        assert session.history == {'H1': 'I=bend;T=none;P=left'}
        if invalidation == 'none':
            assert backend.predict.calls[-2:]==[('B', HISTORY, False), ('A B', HISTORY, False)]
            assert result['semantic_pair']['previous_segment_id'] == 1
            # Already committed B cannot be included in the next command.
            session.submit(final('B', 4, 1300, 1900))
            await session.finish()
            assert 'semantic_pair' not in emitted[-1]
    finally:
        await session.close()


def test_logic_validation_keeps_old_model_and_asr_only_paths_explicit():
    """Require paired recognition to have a supported backend and keep the original mode available without
    one.
    """
    assert validate_logic('original') == 'original'
    with pytest.raises(ValueError): validate_logic('semantic-pair')
    with pytest.raises(ValueError): validate_logic('bad')
    assert validate_logic('semantic-pair', Backend({})) == 'semantic-pair'


