"""Lossless GPU weight banks for one serialized Qwen ASR vLLM engine.

Only exact unequal FP16 parameters are banked. Copies retain the original
parameter addresses used by CUDA graphs. KV and multimodal encoder caches must
be invalidated before every model change. There is no quantization or merging.
"""
import json
import hashlib
from pathlib import Path
import time


def verify_model_compatibility(primary_dir, alternate_dir):
    """Require matching computational settings and byte-identical frontends before reusing one modern vLLM
    engine.
    """
    directories=[Path(primary_dir),Path(alternate_dir)]
    def normalized(value):
        """Recursively strip model metadata keys excluded from compatibility comparison."""
        if isinstance(value,dict):
            return {k:normalized(v) for k,v in value.items()
                    if k not in {'model_type','dtype','transformers_version'}}
        if isinstance(value,list):return [normalized(v) for v in value]
        return value
    configs=[json.loads((p/'config.json').read_text()) for p in directories]
    for c in configs:c['thinker_config'].setdefault('user_token_id',872)
    if normalized(configs[0])!=normalized(configs[1]):
        raise ValueError('Cannot share ASR models with different computational configurations')
    digests={}
    for name in ['preprocessor_config.json','tokenizer_config.json','vocab.json','merges.txt']:
        values=[(p/name).read_bytes() for p in directories]
        if values[0]!=values[1]:raise ValueError('Cannot share different ASR frontends: '+name)
        digests[name]=hashlib.sha256(values[0]).hexdigest()
    return {'computational_config_verified':True,'identical_frontend_sha256':digests,
            'metadata_differences_allowed':['model_type','dtype_forced_fp16','transformers_version'],
            'default_user_token_id':872}


def runtime_parameter_name(name, parameters=None):
    """Map a checkpoint tensor name onto the live modern vLLM parameter layout."""
    if name.startswith('thinker.model.'):
        name='language_model.model.'+name.removeprefix('thinker.model.')
        for part in ('q','k','v'):
            name=name.replace(f'.self_attn.{part}_proj.', '.self_attn.qkv_proj.')
    elif name.startswith('thinker.audio_tower.'):
        name=name.removeprefix('thinker.')
        for part in ('q','k','v'):
            name=name.replace(f'.self_attn.{part}_proj.', '.self_attn.qkv.')
    else:
        raise ValueError('Unexpected differing parameter: '+name)
    candidates = [name]
    if name.startswith('audio_tower.'):
        candidates.append(name.replace('.self_attn.qkv.', '.self_attn.qkv_proj.'))
    if parameters is None:
        return name
    for candidate in candidates:
        if candidate in parameters:
            return candidate
    raise ValueError('Missing exact runtime parameter for ' + name + ': ' + str(candidates))


class WeightBankExtension:
    def prepare_asr_banks(self, primary_dir, alternate_dir, bank_mode='copy'):
        """Build the alternate parameter bank for only tensors that differ between the compatible
        checkpoints.
        """
        import torch
        from safetensors import safe_open
        model=(self.model_runner.get_model() if hasattr(self.model_runner, 'get_model')
               else self.model_runner.model)
        runtime_class = type(model).__module__ + '.' + type(model).__qualname__
        if ('transformers' in type(model).__module__ or
                not runtime_class.startswith(('vllm.', 'qwen_asr.core.vllm_backend.'))):
            raise RuntimeError('Expected actual vLLM Qwen3-ASR model, got ' + runtime_class)
        indexes=[json.loads((Path(p)/'model.safetensors.index.json').read_text())['weight_map']
                 for p in [primary_dir,alternate_dir]]
        if set(indexes[0])-set(indexes[1]):
            raise ValueError('Alternate model is missing primary parameters')
        # The official checkpoint also stores a tied lm_head; the runtime uses
        # the identical shared embedding. All actual primary tensors are checked.
        extra=set(indexes[1])-set(indexes[0])
        if extra-{'thinker.lm_head.weight'}:raise ValueError(str(extra))
        if extra:
            tied=[]
            for key in ['thinker.lm_head.weight','thinker.model.embed_tokens.weight']:
                with safe_open(str(Path(alternate_dir)/indexes[1][key]),framework='pt',device='cpu') as f:
                    tied.append(f.get_tensor(key).to(torch.float16))
            if not torch.equal(tied[0].view(torch.int16),tied[1].view(torch.int16)):
                raise ValueError('Alternate lm_head is not tied to embedding')
            del tied
        different=[];shared_bytes=0;different_bytes=0
        for key in sorted(indexes[0]):
            values=[]
            for directory,index in zip([primary_dir,alternate_dir],indexes):
                with safe_open(str(Path(directory)/index[key]),framework='pt',device='cpu') as f:
                    values.append(f.get_tensor(key).to(torch.float16))
            if torch.equal(values[0].view(torch.int16),values[1].view(torch.int16)):
                shared_bytes+=values[0].numel()*2
            else:
                different.append(key);different_bytes+=values[0].numel()*2
        parameters=dict(model.named_parameters(remove_duplicate=False))
        names=sorted({runtime_parameter_name(k, parameters) for k in different})
        if not set(names)<=parameters.keys():raise ValueError(str(set(names)-parameters.keys()))
        primary={k:parameters[k].detach().clone() for k in names}
        def alternate_weights():
            """Yield alternate model tensors from their indexed safetensors shards."""
            for key in different:
                with safe_open(str(Path(alternate_dir)/indexes[1][key]),framework='pt',device='cpu') as f:
                    yield key,f.get_tensor(key).to(torch.float16)
        model.load_weights(alternate_weights())
        self._asr_bank_parameters={k:parameters[k] for k in names}
        self._asr_bank_mode=bank_mode
        if bank_mode=='copy':
            alternate={k:parameters[k].detach().clone() for k in names}
            for k in names:parameters[k].data.copy_(primary[k])
            self._asr_banks={'v15':primary,'base':alternate}
        elif bank_mode=='swap':
            self._asr_inactive_bank=primary
            self._asr_swap_scratch=torch.empty(max(p.numel() for p in primary.values()),
                                               dtype=torch.float16,device='cuda')
            self._exchange_asr_parameters()
        else:raise ValueError('Unknown ASR bank mode '+bank_mode)
        torch.cuda.synchronize()
        self._asr_bank_variant='v15'
        return {'runtime_model_class': runtime_class,
                'different_hf_tensors':len(different),'runtime_banked_tensors':len(names),
                'shared_fp16_bytes':shared_bytes,'different_fp16_bytes':different_bytes,
                'bank_bytes_per_variant':sum(t.numel()*t.element_size() for t in primary.values()),
                'bank_mode':bank_mode,'stored_inactive_banks':1 if bank_mode=='swap' else 2,
                'variant':'v15','lossless':True,'fixed_graph_parameter_addresses':True}

    def _exchange_asr_parameters(self):
        """Swap active and inactive parameter values without reallocating the live parameters."""
        for name,param in self._asr_bank_parameters.items():
            temporary=self._asr_swap_scratch[:param.numel()].view_as(param)
            temporary.copy_(param.data)
            param.data.copy_(self._asr_inactive_bank[name])
            self._asr_inactive_bank[name].copy_(temporary)

    def select_asr_bank(self, variant):
        """Activate the requested weight bank and return its switch timing."""
        import torch
        if variant not in ('v15','base'):raise ValueError('Unknown ASR bank '+variant)
        changed=variant!=self._asr_bank_variant
        started=time.perf_counter()
        if changed:
            with torch.inference_mode():
                if self._asr_bank_mode=='swap':self._exchange_asr_parameters()
                else:
                    torch._foreach_copy_([p.data for p in self._asr_bank_parameters.values()],
                                         list(self._asr_banks[variant].values()))
            torch.cuda.synchronize()
            self._asr_bank_variant=variant
        return {'variant':variant,'changed':changed,'weight_copy_ms':(time.perf_counter()-started)*1000}


class SharedEngineController:
    def __init__(self, llm, primary_dir, alternate_dir, bank_mode='copy'):
        """Initialize the controller and prepare the two ASR banks through the modern engine RPC interface.
        """
        self.llm=llm;self.variant='v15'
        compatibility=verify_model_compatibility(primary_dir,alternate_dir)
        self.inventory=llm.collective_rpc('prepare_asr_banks',args=(str(primary_dir),str(alternate_dir),bank_mode))[0]
        self.inventory['compatibility']=compatibility

    def select(self, variant):
        """Invalidate cached model state and coordinate the switch to the requested bank."""
        if variant not in ('v15','base'):raise ValueError('Unknown ASR variant '+variant)
        started=time.perf_counter();changed=variant!=self.variant
        if changed:
            # The protocol and application each serialize decoding. A false
            # reset is an error, never permission to use stale other-model KV.
            if not self.llm.reset_prefix_cache():raise RuntimeError('ASR KV invalidation failed')
            self.llm.reset_mm_cache()
            result=self.llm.collective_rpc('select_asr_bank',args=(variant,))[0]
            self.variant=variant
        else:result={'variant':variant,'changed':False,'weight_copy_ms':0.}
        return {**result,'cache_invalidated':changed,
                'switch_ms':(time.perf_counter()-started)*1000}
