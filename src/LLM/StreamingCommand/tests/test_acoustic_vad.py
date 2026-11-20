"""Acoustic and transport invariants; these tests do not claim ASR accuracy."""
import math
import struct
import sys
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from streamingcommand._runtime.acoustic_vad import AcousticGateConfig,AcousticGateVAD,rms_dbfs
from streamingcommand._runtime.streaming import StreamingSession,make_vad

class AlwaysSpeech:
    # Force a positive base VAD vote to isolate the energy gate in tests.
    def is_speech(self,frame,sample_rate):return True

def tone(dbfs, samples=320):
    """Synthesize a 500 Hz PCM16 frame at the requested RMS level."""
    peak=32768*10**(dbfs/20)*math.sqrt(2)
    return b''.join(struct.pack('<h',round(peak*math.sin(2*math.pi*500*i/16000))) for i in range(samples))

class Capture(StreamingSession):
    def __init__(self,vad):
        """Run the real framing logic with a supplied VAD and capture outputs without starting a model."""
        super().__init__(None,None,vad=vad)
        self.final_pcm=[]
        self.events=[]
    # Collect the VAD and segmentation events synchronously for assertions.
    def _emit(self,e):self.events.append(e)
    def _submit(self,kind):
        """Capture the exact active waveform presented for a final decode."""
        if kind=='final':self.final_pcm.append(bytes(self._active['pcm']))

def feed_frames(session,audio):
    """Feed frame-aligned audio while retaining the valid length of the padded final frame."""
    for offset in range(0,len(audio),640):
        frame=audio[offset:offset+640]
        session._frame(frame+bytes(640-len(frame)),len(frame)//2)


def test_dc_removed_energy_and_pcm16_floor():
    """Check silence, DC rejection, and calibrated RMS measurement at two levels."""
    assert rms_dbfs(bytes(640))==-100
    assert rms_dbfs(struct.pack('<h',12345)*320)==-100
    assert abs(rms_dbfs(tone(-60))+60)<0.4
    assert abs(rms_dbfs(tone(-36))+36)<0.05


def test_causal_gate_rejects_steady_vad_false_positive_then_accepts_quiet_speech():
    """Verify that calibrated quiet noise is rejected even with a positive base VAD and active speech
    preserves the noise floor.
    """
    gate=AcousticGateVAD(AlwaysSpeech())
    noise=tone(-68)
    for _ in range(100):
        gate.set_active(False)
        assert not gate.is_speech(noise,16000)
    assert abs(gate.noise_dbfs+68)<0.4
    # A genuine quiet signal above a quiet background needs no calibration wait.
    gate.set_active(False)
    assert gate.is_speech(tone(-54),16000)
    gate.set_active(True)
    before=gate.noise_dbfs
    for _ in range(150):assert gate.is_speech(tone(-63),16000)
    assert gate.noise_dbfs==before  # sustained speech cannot raise the noise floor


def test_hysteresis_continuation_and_preroll_preserve_complete_soft_tail():
    """Verify that pre-roll and endpoint framing retain the quiet tail byte for byte."""
    gate=AcousticGateVAD(AlwaysSpeech())
    s=Capture(gate)
    noise=tone(-68);quiet_tail=tone(-65)
    leading=noise*100
    phrase=tone(-50)*10+quiet_tail*8
    trailing=noise*20
    audio=leading+phrase+trailing
    feed_frames(s,audio)
    assert len(s.final_pcm)==1
    event=s.events[0]
    assert event['type']=='speech.started'
    start=round(event['audio_start_ms']*16)*2
    # Even below the continuation threshold, the existing 240 ms endpoint tail
    # captures a 160 ms weak trailing sound byte for byte. No waveform rewriting.
    assert s.final_pcm[0]==audio[start:start+len(s.final_pcm[0])]
    tail_end=len(leading)+len(phrase)
    assert start+len(s.final_pcm[0])>=tail_end


def test_one_frame_impulse_cannot_use_vad_overhang_to_create_a_segment():
    """Require a lone high-energy frame to remain below the onset confirmation requirement."""
    s=Capture(AcousticGateVAD(AlwaysSpeech()))
    noise=tone(-68)
    feed_frames(s,noise*100+tone(-25)+noise*100)
    assert not s.events
    assert not s.final_pcm


def test_live_default_is_safe2s_and_baseline_can_be_injected():
    """Check the default gate settings and prove that an injected WebRTC baseline remains available."""
    default=StreamingSession(None,None)
    assert isinstance(default.vad,AcousticGateVAD)
    assert default.vad.config==AcousticGateConfig(continuation_uses_raw_vad=True,
        passthrough_before_calibration=True,minimum_noise_frames=100)
    assert not isinstance(StreamingSession(None,None,vad=make_vad(2)).vad,AcousticGateVAD)
    audio=bytes(640)*20+tone(-35)*20+bytes(640)*20
    a,b=Capture(default.vad),Capture(make_vad(2))
    feed_frames(a,audio);feed_frames(b,audio)
    assert a.events==b.events  # cold onset is still immediate original WebRTC
    assert a.final_pcm==b.final_pcm
    with pytest.raises(ValueError):make_vad(4)
    with pytest.raises(ValueError):AcousticGateConfig(onset_snr_db=1,continuation_snr_db=3)


def test_uncalibrated_safe_onset_does_not_wait_or_reject_immediate_weak_voice():
    """Verify that a weak initial utterance starts at the ordinary confirmation window before noise
    calibration.
    """
    config=AcousticGateConfig(continuation_uses_raw_vad=True,
                             passthrough_before_calibration=True,
                             minimum_noise_frames=100)
    safe=Capture(AcousticGateVAD(AlwaysSpeech(),config))
    phrase=tone(-68)*15
    feed_frames(safe,phrase)
    assert safe.events[0]['type']=='speech.started'
    assert safe.events[0]['audio_start_ms']==0
    assert safe.events[0]['audio_end_ms']==60  # same existing 2-of-3 window
    safe._submit_final('test_end')
    assert safe.final_pcm==[phrase]


