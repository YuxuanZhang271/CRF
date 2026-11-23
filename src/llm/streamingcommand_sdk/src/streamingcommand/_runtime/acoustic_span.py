"""Cheap speech-span feature used only to select an ASR model, not reject audio."""
import numpy as np


def acoustic_span(pcm):
    """Measure the span between active 10 ms PCM16 blocks using a floor and peak-relative RMS threshold."""
    samples=np.frombuffer(pcm,dtype='<i2').astype(np.float64)
    if not len(samples):return {'active_span_ms':0.,'threshold_dbfs':-55.}
    blocks=[samples[i:i+160] for i in range(0,len(samples),160)]
    levels=np.array([20*np.log10(max(1e-12,np.sqrt(np.mean(b*b))/32768)) for b in blocks])
    threshold=max(-55.,float(levels.max())-25.)
    active=np.flatnonzero(levels>=threshold)
    span=(min(len(samples),(int(active[-1])+1)*160)-int(active[0])*160)/16 if len(active) else 0.
    return {'active_span_ms':span,'threshold_dbfs':threshold,'peak_block_rms_dbfs':float(levels.max()),
            'frame_ms':10,'waveform_changed':False}
