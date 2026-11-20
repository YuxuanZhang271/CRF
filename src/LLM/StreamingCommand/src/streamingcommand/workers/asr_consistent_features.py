"""Per-clip features aligned to native vLLM's hop padding, without weight edits.

HF pads waveforms to the batch maximum before STFT; a non-hop-aligned short
clip can therefore have floor(N/hop) valid frames alone but ceil(N/hop) in a
batch. Native vLLM pads each waveform to a hop multiple first. Extracting each
clip independently after the same hop padding also removes STFT edge dependence
on companion clips. Only feature tensors are padded for the model batch.
"""
import numpy as np


class NativeConsistentFeatureExtractor:
    def __init__(self, original):
        """Wrap the existing feature extractor so its original configuration remains available."""
        self.original = original

    def __getattr__(self, name):
        """Forward feature-extractor attributes that the wrapper does not implement."""
        return getattr(self.original, name)

    def __call__(self, audio, **kwargs):
        """Hop-pad each mono clip independently, then batch the extracted features with matching masks."""
        import torch
        from transformers.feature_extraction_utils import BatchFeature
        clips = audio if isinstance(audio, (list, tuple)) else [audio]
        if clips and np.isscalar(clips[0]):
            clips = [np.asarray(clips)]
        options = dict(kwargs)
        requested_type = options.pop('return_tensors', None)
        options.update(return_tensors='pt', return_attention_mask=True, padding=True)
        features = []
        for clip in clips:
            wave = np.asarray(clip, dtype=np.float32)
            if wave.ndim != 1 or not len(wave):
                raise ValueError('Require nonempty mono waveform')
            hop = self.original.hop_length
            wave = np.pad(wave, (0, (-len(wave)) % hop))
            result = self.original([wave], **options)
            feature = result['input_features'][0]
            assert feature.shape[-1] == len(wave)//hop
            assert int(result['attention_mask'][0].sum()) == feature.shape[-1]
            features.append(feature)
        width = max(x.shape[-1] for x in features)
        data = {
            'input_features': torch.stack([torch.nn.functional.pad(x, (0,width-x.shape[-1])) for x in features]),
            'attention_mask': torch.stack([torch.nn.functional.pad(torch.ones(x.shape[-1],dtype=torch.long), (0,width-x.shape[-1])) for x in features]),
        }
        if requested_type != 'pt':
            data = {k:v.numpy() for k,v in data.items()}
        return BatchFeature(data=data, tensor_type=requested_type)


def install_native_consistent_features(processor):
    """Install per-clip hop padding once on the supplied processor."""
    if not isinstance(processor.feature_extractor, NativeConsistentFeatureExtractor):
        processor.feature_extractor = NativeConsistentFeatureExtractor(processor.feature_extractor)
    return processor


def native_block_attention_mask(hidden_states, cu_seqlens):
    """Match native ragged attention blocks when HF uses SDPA/eager."""
    import torch
    if cu_seqlens.numel()<=2:
        return None
    length=hidden_states.shape[0]
    mask=torch.full((1,1,length,length),torch.finfo(hidden_states.dtype).min,
                    device=hidden_states.device,dtype=hidden_states.dtype)
    boundaries=cu_seqlens.tolist()
    assert boundaries[0]==0 and boundaries[-1]==length
    for start,end in zip(boundaries,boundaries[1:]):mask[...,start:end,start:end]=0
    return mask


def install_native_consistent_attention(model):
    """HF legacy code passes cu_seqlens but SDPA ignores it without a mask.

    Native vLLM actually limits attention to these blocks. Preserve the exact
    original short-audio path, and supply a block mask only for multiple blocks.
    This is a runtime wrapper; no model parameters or vendor files are edited.
    """
    tower=model.thinker.audio_tower
    if getattr(tower,'_native_block_masks_installed',False):return model
    cache={}
    def wrap(original,implementation):
        """Bind an audio attention layer to its original method and configured attention implementation."""
        def forward(hidden_states,cu_seqlens,attention_mask=None,**kwargs):
            """Supply a cached block mask to SDPA or eager attention for multiple acoustic blocks."""
            if attention_mask is None and implementation in ('sdpa','eager') and cu_seqlens.numel()>2:
                key=(tuple(cu_seqlens.tolist()),str(hidden_states.device),hidden_states.dtype)
                if cache.get('key')!=key:
                    cache.clear();cache.update(key=key,mask=native_block_attention_mask(hidden_states,cu_seqlens))
                attention_mask=cache['mask']
            return original(hidden_states,cu_seqlens,attention_mask=attention_mask,**kwargs)
        return forward
    for layer in tower.layers:
        layer.forward=wrap(layer.forward,layer.self_attn.config._attn_implementation)
    tower._native_block_masks_installed=True
    return model


def canonical_pcm(pcm, leading_ms=160, trailing_ms=160, threshold_dbfs=-75):
    """Experimental margin normalization; never references the transcript.

    Retains every original sample between the first and last sample above -75
    dBFS, including all internal pauses. Only leading/trailing subthreshold audio
    is replaced; this is not command filtering. For all-subthreshold input leave
    the waveform unchanged so silence behavior is still evaluated by ASR.
    """
    pcm = np.asarray(pcm)
    assert pcm.dtype == np.int16 and pcm.ndim == 1
    active = np.flatnonzero(np.abs(pcm.astype(np.int32)) > 32768*10**(threshold_dbfs/20))
    if not len(active):
        return pcm.copy(), {'changed':False,'all_subthreshold':True}
    lo, hi = int(active[0]), int(active[-1])+1
    front, tail = round(leading_ms*16), round(trailing_ms*16)
    result = np.pad(pcm[lo:hi], (front,tail))
    result = np.pad(result, (0,(-len(result))%160))
    def peak_db(a):
        """Report the peak dBFS of a discarded PCM margin, or None for a silent margin."""
        peak = int(np.abs(a.astype(np.int32)).max()) if len(a) else 0
        return 20*np.log10(peak/32768) if peak else None
    return result, {'changed':True,'source_start':lo,'source_end':hi,
        'removed_leading_peak_dbfs':peak_db(pcm[:lo]),'removed_trailing_peak_dbfs':peak_db(pcm[hi:]),
        'retained_active_pcm_bit_identical':bool(np.array_equal(result[front:front+hi-lo],pcm[lo:hi])),
        'leading_ms':leading_ms,'trailing_ms':trailing_ms,'threshold_dbfs':threshold_dbfs}
