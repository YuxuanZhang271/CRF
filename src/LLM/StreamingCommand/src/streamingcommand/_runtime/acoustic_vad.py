"""Causal acoustic gate; never examines ASR text or command labels.

An idle rolling lower quantile estimates background RMS. Configurations support
an onset margin and continuation hysteresis. The live segmenter selects only
onset gating, uses raw WebRTC VAD during speech, and passes raw VAD through until
100 idle frames have accumulated. Existing preroll and endpoint tail remain.
"""
from __future__ import annotations

from array import array
from collections import deque
from dataclasses import asdict, dataclass
import math
import sys


def rms_dbfs(pcm16: bytes) -> float:
    """DC-removed RMS of little-endian signed PCM16, bounded at -100 dBFS."""
    values = array('h')
    values.frombytes(pcm16)
    if sys.byteorder != 'little':
        values.byteswap()
    if not values:
        return -100.0
    mean = sum(values) / len(values)
    energy = sum((value - mean) ** 2 for value in values) / len(values)
    return max(-100.0, 10 * math.log10(max(energy, 1e-12) / (32768.0 ** 2)))


@dataclass(frozen=True)
class AcousticGateConfig:
    onset_snr_db: float = 8.0
    continuation_snr_db: float = 3.0
    absolute_floor_dbfs: float = -80.0
    initial_noise_dbfs: float = -72.0
    noise_window_frames: int = 100  # 2 s of idle frames at 20 ms/frame
    minimum_noise_frames: int = 10
    noise_quantile: float = 0.20
    continuation_uses_raw_vad: bool = False
    passthrough_before_calibration: bool = False

    def __post_init__(self):
        """Reject inconsistent acoustic thresholds, calibration limits, or hysteresis settings."""
        if self.onset_snr_db < self.continuation_snr_db or self.continuation_snr_db < 0:
            raise ValueError('Require onset SNR >= continuation SNR >= 0')
        if not 0 <= self.noise_quantile <= 1:
            raise ValueError('Noise quantile must be between 0 and 1')
        if not 1 <= self.minimum_noise_frames <= self.noise_window_frames:
            raise ValueError('Noise window must cover minimum noise frames')
        if self.initial_noise_dbfs < -100 or self.absolute_floor_dbfs < -100:
            raise ValueError('PCM16 floors must be >= -100 dBFS')


DEFAULT_GATE_CONFIG = AcousticGateConfig(
    continuation_uses_raw_vad=True,
    passthrough_before_calibration=True,
    minimum_noise_frames=100,
)


def default_vad_settings(config=None):
    """JSON-compatible configuration snapshot shared with session recordings."""
    config = config or DEFAULT_GATE_CONFIG
    return {'base_vad': 'webrtcvad', 'mode': 2,
            'gate': 'adaptive_rms_onset', 'gate_config': asdict(config),
            'rms_reference': 'dc_removed_pcm16_dbfs',
            'noise_reference': 'past_idle_frames_lower_quantile',
            'margin_is_snr_proxy': True,
            'uncalibrated_action': ('raw_webrtc_vad_no_start_delay'
                if config.passthrough_before_calibration else 'past_noise_or_absolute_floor_no_start_delay'),
            'active_action': 'raw_webrtc_vad_no_energy_gating'}


class AcousticGateVAD:
    """Wrap an existing VAD with inexpensive, stateful, causal energy gating.

    ``set_active`` must be called before each frame by the segmenter.  This does
    not add buffering or a new start confirmation window.  The lower threshold
    during active speech is hysteresis, not a text-dependent acceptance rule.
    """
    def __init__(self, base_vad, config: AcousticGateConfig | None = None):
        """Initialize a causal energy gate around the supplied frame-level VAD."""
        self.base_vad = base_vad
        self.config = config or AcousticGateConfig()
        self._idle_levels = deque(maxlen=self.config.noise_window_frames)
        self._active = False
        self.noise_dbfs = self.config.initial_noise_dbfs
        self.last_frame = {}
        self.stats = {'frames': 0, 'raw_speech_frames': 0,
                      'accepted_speech_frames': 0, 'gated_speech_frames': 0}

    def set_active(self, active: bool):
        """Notify the gate when the streaming session enters or leaves an active speech segment."""
        self._active = bool(active)

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        """Combine the base VAD vote with calibrated energy and onset or continuation rules."""
        raw = bool(self.base_vad.is_speech(frame, sample_rate))
        level = rms_dbfs(frame)
        # Use only past idle frames for this decision.  Starting immediately on
        # speech remains possible; no fixed calibration pause is imposed.
        margin = (self.config.continuation_snr_db if self._active
                  else self.config.onset_snr_db)
        threshold = max(self.config.absolute_floor_dbfs, self.noise_dbfs + margin)
        calibrated = len(self._idle_levels) >= self.config.minimum_noise_frames
        accepted = raw and (level >= threshold or
            (not calibrated and self.config.passthrough_before_calibration) or
            (self._active and self.config.continuation_uses_raw_vad))
        self.last_frame = {'rms_dbfs': level, 'noise_dbfs': self.noise_dbfs,
                           'threshold_dbfs': threshold, 'raw_speech': raw,
                           'speech': accepted, 'active': self._active,
                           'noise_calibrated': calibrated}
        self.stats['frames'] += 1
        self.stats['raw_speech_frames'] += int(raw)
        self.stats['accepted_speech_frames'] += int(accepted)
        self.stats['gated_speech_frames'] += int(raw and not accepted)
        if not self._active:
            self._idle_levels.append(level)
            ordered = sorted(self._idle_levels)
            index = int((len(ordered) - 1) * self.config.noise_quantile)
            observed = ordered[index]
            if len(ordered) >= self.config.minimum_noise_frames:
                self.noise_dbfs = observed
            else:
                # A quiet start can lower the prior immediately.  A speech or
                # noise onset cannot raise it before sufficient idle history.
                self.noise_dbfs = min(self.noise_dbfs, observed)
        return accepted

    @property
    def description(self):
        """Return the effective gate settings for session metadata."""
        return {'kind': 'adaptive_rms_gate', 'config': asdict(self.config),
                'noise_dbfs': self.noise_dbfs, 'stats': dict(self.stats)}
