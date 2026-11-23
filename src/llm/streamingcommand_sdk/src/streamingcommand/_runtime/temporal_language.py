"""Experimental causal language evidence; no command vocabulary or text edits."""
from .acoustic_span import acoustic_span
import re


def repetition_signature(text):
    """Compare hypotheses, ignoring exact whole-phrase repetitions only.

    The returned signature never replaces ASR output or enters its prompt.
    No vocabulary, stemming or command equivalence is used.
    """
    words=tuple(re.findall(r'\w+',text.casefold()))
    for width in range(1,len(words)//2+1):
        if len(words)%width==0 and words[:width]*(len(words)//width)==words:
            return words[:width]
    return words


def history_agrees(history,minimum=2,reject_short_repetitions=False):
    """Check whether recent hypotheses have the same repetition-normalized signature for prefix reuse."""
    if len(history)<minimum:return False
    selected=history[-minimum:]
    signatures=[repetition_signature(r['text']) for r in selected]
    if reject_short_repetitions:
        # A repeated short hypothesis can contain more utterances than its
        # waveform. Supplying it as an aligned prefix may consume the next
        # utterance and produce an empty suffix. Longer merged speech keeps
        # its original hypothesis; this gate never edits emitted text.
        if any(r.get('samples',0)<=32000 and
               len(re.findall(r'\w+',r['text']))>len(signature)
               for r,signature in zip(selected,signatures)):
            return False
    return bool(signatures[0]) and all(value==signatures[0] for value in signatures)


class TemporalLanguage:
    def __init__(self, minimum_span_ms=200, maximum_gap_ms=1500):
        """Set the acoustic span and recent-English lifetime used by the causal language policy."""
        self.minimum_span_ms=minimum_span_ms
        self.maximum_gap_ms=maximum_gap_ms
        self.last_english_end_ms=None

    def choose(self, pcm, current, rolling, start_ms, end_ms, *, speech_evidence=None):
        """Choose an optional language condition from current, rolling, and still-recent acoustic evidence.
        """
        span=acoustic_span(pcm)['active_span_ms']
        ct,cp=current.get('native_language_tag'),current.get('language_probability',0)
        rt,rp=rolling.get('native_language_tag'),rolling.get('language_probability',0)
        recent=(self.last_english_end_ms is not None and
                start_ms-self.last_english_end_ms<=self.maximum_gap_ms)
        agreed_other=(ct in ('zh','yue') and rt in ('zh','yue') and cp>=.9 and rp>=.9)
        requested=None;reason='automatic'
        if speech_evidence is not None and not speech_evidence['speech_confirmed']:
            # This is a language-conditioning decision, not a speech filter.
            # The complete waveform still receives one ordinary auto ASR pass.
            self.last_english_end_ms=None
            reason='unconfirmed_acoustic_speech_use_auto_language'
        elif span<self.minimum_span_ms or ct=='nospeech':
            reason='insufficient_acoustic_language_evidence'
        elif agreed_other:
            requested='Chinese' if ct=='zh' and rt=='zh' else None
            self.last_english_end_ms=None
            reason='current_and_recent_non_english_agreement'
        elif (ct=='en' and cp>=.95) or (rt=='en' and rp>=.8) or recent:
            requested='English';self.last_english_end_ms=end_ms
            reason='causal_acoustic_english_evidence'
        return {'requested_language':requested,'reason':reason,'active_span_ms':span,
                'recent_english_before_current':recent,'current':current,'rolling':rolling,
                'acoustic_speech_evidence':speech_evidence,
                'audio_wait_added_ms':0,'reference_or_command_used':False}
