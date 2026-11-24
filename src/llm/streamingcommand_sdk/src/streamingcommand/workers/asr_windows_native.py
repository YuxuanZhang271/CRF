"""Native Windows PyTorch ASR; no vLLM, WSL, Triton or FlashAttention required.

The chunk/prefix rules follow Qwen3-ASR 0.0.6 (Apache-2.0). The protocol is
compatible with the R17 scheduler, but execution and latency are separately
validated; vLLM measurements must never be attributed to this backend.
"""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import time

import numpy as np
import torch


class NativeSampling:
    # Store the per-generation token limit and initialize the optional logit prior.
    def __init__(self,max_tokens):self.max_tokens=max_tokens;self.logit_bias=None
    # Shallow-copy sampling options so a request can replace its own logit bias.
    def clone(self):return copy.copy(self)


class TorchGenerator:
    def __init__(self,model,processor,max_model_len,decoder_backend='hf'):
        """Configure the native tokenizer and model bridge, including an optional CUDA graph decoder."""
        self.hf_model=model;self.processor=processor;self.max_model_len=max_model_len
        self.accelerator=None
        if decoder_backend!='hf':
            from asr_cuda_graph import GreedyAccelerator
            self.accelerator=GreedyAccelerator(model,mode=decoder_backend)
            self.accelerator.warmup()

    @torch.inference_mode()
    def generate(self,prompts,sampling_params,use_tqdm=False):
        """Translate SDK-style prompts to native model inputs and return SDK-compatible generated text."""
        
        results=[]
        for item in prompts:
            audio=item['multi_modal_data']['audio']
            if len(audio)!=1:raise ValueError('Exactly one mono audio is required')
            inputs=self.processor(text=[item['prompt']],audio=audio,return_tensors='pt',padding=True)
            inputs=inputs.to(self.hf_model.device).to(self.hf_model.dtype)
            prompt_ids=inputs['input_ids'][0].tolist()
            if len(prompt_ids)+sampling_params.max_tokens>self.max_model_len:
                raise ValueError('ASR context capacity exceeded; no silent truncation')
            if self.accelerator is None:
                output=self.hf_model.generate(**inputs,max_new_tokens=sampling_params.max_tokens,
                    do_sample=False,use_cache=True,
                    logits_processor=[NativePrior(sampling_params.logit_bias)] if sampling_params.logit_bias else None)
                token_ids=output.sequences[0,len(prompt_ids):].tolist()
            else:
                token_ids=self.accelerator.generate(inputs,sampling_params.max_tokens)[0].tolist()
            text=self.processor.tokenizer.decode(token_ids,skip_special_tokens=True,
                                                 clean_up_tokenization_spaces=False)
            results.append(SimpleNamespace(prompt_token_ids=prompt_ids,num_cached_tokens=0,
                outputs=[SimpleNamespace(text=text,token_ids=token_ids,
                    finish_reason='length' if len(token_ids)>=sampling_params.max_tokens else 'stop')]))
        return results


class NativeWindowsASR:
    def __init__(self,model_dir,max_new_tokens=96,max_model_len=4096,decoder_backend='hf',device='cuda:0'):
        """Load the local Qwen ASR model and processor onto the selected CPU or CUDA device."""
        from transformers import AutoConfig
        from streamingcommand._runtime.model_loading import configure_loading
        configure_loading()
        from qwen_asr_backend import Qwen3ASRConfig, Qwen3ASRForConditionalGeneration, Qwen3ASRProcessor
        from asr_consistent_features import install_native_consistent_features,install_native_consistent_attention
        AutoConfig.register('qwen3_asr',Qwen3ASRConfig,exist_ok=True)
        model=Qwen3ASRForConditionalGeneration.from_pretrained(str(model_dir),dtype=torch.float16 if device.startswith('cuda:') else torch.float32,
            device_map=device,attn_implementation='sdpa',local_files_only=True).eval()
        processor=Qwen3ASRProcessor.from_pretrained(str(model_dir),local_files_only=True,
            fix_mistral_regex=True)
        self.processor=install_native_consistent_features(processor)
        install_native_consistent_attention(model)
        self.model=TorchGenerator(model,self.processor,max_model_len,decoder_backend)
        self.sampling_params=NativeSampling(max_new_tokens)

    def _build_text_prompt(self,context,force_language):
        """Render the multimodal chat prompt with context and any forced language marker."""
        messages=[{'role':'system','content':context or ''},
                  {'role':'user','content':[{'type':'audio','audio':''}]}]
        prompt=self.processor.apply_chat_template(messages,add_generation_prompt=True,tokenize=False)
        return prompt+(f'language {force_language}<asr_text>' if force_language else '')

    def transcribe(self,audio,context='',language=None,**kwargs):
        """Generate a complete transcript for one waveform and parse the returned language and text."""
        from asr_output import parse_asr_output
        from asr_supported_margins_v3 import supported_margin_pcm
        if audio[1]!=16000:raise ValueError('Native worker requires mono16k audio')
        marked=context.startswith('__PHONO_ACOUSTIC_PREFIX__')
        if marked:
            payload=json.loads(context.removeprefix('__PHONO_ACOUSTIC_PREFIX__'))
            if language!='English' or not payload['previous_asr_text']:raise ValueError('Invalid acoustic prefix')
            prompt=self._build_text_prompt(payload['context'],language)+payload['previous_asr_text']+' '
            waveform=audio[0]
        else:
            pcm=np.rint(audio[0]*32768).clip(-32768,32767).astype(np.int16)
            normalized,_=supported_margin_pcm(pcm)
            waveform=normalized.astype(np.float32)/32768
            prompt=self._build_text_prompt(context,language)
        result=self.model.generate([{'prompt':prompt,'multi_modal_data':{'audio':[waveform]}}],
                                   sampling_params=self.sampling_params,use_tqdm=False)[0].outputs[0].text
        if marked:return [SimpleNamespace(text=result,language=language)]
        detected,text=parse_asr_output(result,user_language=language)
        return [SimpleNamespace(text=text,language=detected)]

    def init_streaming_state(self,context='',language=None,chunk_size_sec=.8,
                             unfixed_chunk_num=2,unfixed_token_num=5):
        """Initialize per-segment audio buffers, prompt, and rollback settings."""
        if chunk_size_sec<=0:raise ValueError('chunk_size_sec must be positive')
        return SimpleNamespace(context=context,force_language=language,
            prompt_raw=self._build_text_prompt(context,language),chunk_id=0,
            chunk_size_sec=chunk_size_sec,chunk_size_samples=max(1,round(chunk_size_sec*16000)),
            unfixed_chunk_num=unfixed_chunk_num,unfixed_token_num=unfixed_token_num,
            buffer=np.zeros(0,np.float32),audio_accum=np.zeros(0,np.float32),
            _raw_decoded='',text='',language='')

    def _decode_state(self,state,final=False):
        """Decode the accumulated waveform with the retained token prefix and update its text and language.
        """
        from asr_output import parse_asr_output
        prefix=''
        if state.chunk_id>=state.unfixed_chunk_num:
            ids=self.processor.tokenizer.encode(state._raw_decoded);rollback=state.unfixed_token_num
            end=max(1 if final else 0,len(ids)-rollback)
            prefix=self.processor.tokenizer.decode(ids[:end]) if end else ''
            while '\ufffd' in prefix and not final:
                rollback+=1;end=max(0,len(ids)-rollback)
                prefix=self.processor.tokenizer.decode(ids[:end]) if end else ''
        output=self.model.generate([{'prompt':state.prompt_raw+prefix,
            'multi_modal_data':{'audio':[state.audio_accum]}}],
            sampling_params=self.sampling_params,use_tqdm=False)[0].outputs[0].text
        state._raw_decoded=prefix+output
        state.language,state.text=parse_asr_output(state._raw_decoded,user_language=state.force_language)
        state.chunk_id+=1
        return state

    def streaming_transcribe(self,audio,state):
        """Append PCM and decode each complete buffered chunk in the current stream."""
        # R17 complete-prefix policy: one generation consumes every available
        # sample. Reception remains asynchronous in the parent scheduler.
        wave=np.asarray(audio)
        wave=wave.astype(np.float32)/32768 if wave.dtype==np.int16 else wave.astype(np.float32)
        available=np.concatenate([state.buffer,wave]);state.buffer=np.zeros(0,np.float32)
        if not len(available):return state
        state.audio_accum=np.concatenate([state.audio_accum,available])
        return self._decode_state(state)

    def finish_streaming_transcribe(self,state):
        """Decode any remaining buffered audio as the last streaming chunk."""
        if not len(state.buffer):return state
        state.audio_accum=np.concatenate([state.audio_accum,state.buffer])
        state.buffer=np.zeros(0,np.float32)
        return self._decode_state(state,final=True)


class NativeWeightController:
    def __init__(self,generator,primary_dir,alternate_dir,bank_mode='swap'):
        """Prepare exact alternate weights and a scratch buffer for in-place CUDA bank swaps."""
        from safetensors import safe_open
        from model_compatibility import verify_model_compatibility
        compatibility=verify_model_compatibility(primary_dir,alternate_dir)
        indexes=[json.loads((Path(p)/'model.safetensors.index.json').read_text())['weight_map']
                 for p in [primary_dir,alternate_dir]]
        model=generator.hf_model;params=dict(model.named_parameters());different=[]
        for name in sorted(indexes[0]):
            values=[]
            for directory,index in zip([primary_dir,alternate_dir],indexes):
                with safe_open(str(Path(directory)/index[name]),framework='pt',device='cpu') as f:
                    values.append(f.get_tensor(name).to(torch.float16))
            if not torch.equal(values[0].view(torch.int16),values[1].view(torch.int16)):different.append(name)
        if not set(different)<=params.keys():raise ValueError('Native parameter mapping mismatch')
        self.parameters={k:params[k] for k in different}
        self.inactive={}
        for name in different:
            with safe_open(str(Path(alternate_dir)/indexes[1][name]),framework='pt',device='cpu') as f:
                self.inactive[name]=f.get_tensor(name).to(device='cuda',dtype=torch.float16)
        self.scratch=torch.empty(max(p.numel() for p in self.parameters.values()),dtype=torch.float16,device='cuda')
        self.variant='v15'
        self.inventory={'variant':'v15','backend':'native-windows-pytorch','weight_bank_mode':'swap',
            'different_hf_tensors':len(different),'bank_bytes':sum(t.numel()*2 for t in self.inactive.values()),
            'compatibility':compatibility,'cache_policy':'fresh per-generation HF cache; no cross-model KV reuse'}

    @torch.inference_mode()
    def select(self,variant):
        """Exchange the active and inactive native weight banks when a different variant is requested."""
        if variant not in ('v15','base'):raise ValueError('Invalid ASR variant')
        started=time.perf_counter();changed=variant!=self.variant
        if changed:
            for name,param in self.parameters.items():
                tmp=self.scratch[:param.numel()].view_as(param)
                tmp.copy_(param.data);param.data.copy_(self.inactive[name]);self.inactive[name].copy_(tmp)
            torch.cuda.synchronize();self.variant=variant
        elapsed=(time.perf_counter()-started)*1000
        return {'variant':variant,'changed':changed,'weight_copy_ms':elapsed,
                'switch_ms':elapsed,'cache_invalidated':changed,
                'cache_policy':'new KV for every generation'}


class NativePrior:
    """Apply the same bounded native token priors while protecting original boundaries."""
    def __init__(self, bias):
        """Store the token bias mapping used by the native generation callback."""
        self.bias = bias or {}

    def __call__(self, input_ids, scores):
        """Apply the configured logit bias while preserving an already preferred boundary."""
        from asr_boundary_prior_guard import BOUNDARIES
        original = scores.clone()
        best = original.argmax(-1)
        protect = torch.zeros_like(best, dtype=torch.bool)
        for token in BOUNDARIES:
            protect |= best == token
        for token, weight in self.bias.items():
            scores[:, int(token)] += float(weight)
        return torch.where(protect[:, None], original, scores)
