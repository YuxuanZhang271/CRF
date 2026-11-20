"""Portable PyTorch CUDA-graph decoder for legacy Qwen3-ASR.

No vLLM/Triton/compiler/quantization. Audio frontend/encoder and prefill stay
unchanged. Only batch-one greedy token steps are captured. A fresh prefill is
copied for every request, including after model weight-bank switches.
"""
import time
import threading
import torch


class DecodeGraph:
    def __init__(self,thinker,capacity):
        """Allocate fixed decode buffers and capture one token step against the static KV cache."""
        from transformers.cache_utils import StaticCache
        self.thinker=thinker;self.capacity=capacity
        device=next(thinker.parameters()).device
        self.cache=StaticCache(config=thinker.model.config,max_cache_len=capacity)
        self.token=torch.zeros((1,1),dtype=torch.long,device=device)
        self.position=torch.zeros((1,),dtype=torch.long,device=device)
        self.delta=torch.zeros((1,1),dtype=torch.long,device=device)
        self.columns=torch.arange(capacity,device=device)
        self.graph=torch.cuda.CUDAGraph()
        stream=torch.cuda.Stream(device=device);stream.wait_stream(torch.cuda.current_stream(device))
        with torch.cuda.stream(stream),torch.inference_mode():
            for _ in range(3):self._step()
        torch.cuda.current_stream(device).wait_stream(stream)
        with torch.inference_mode(),torch.cuda.graph(self.graph):self._step()
        torch.cuda.synchronize(device)

    def _step(self):
        """Decode one buffered token and write the next-token prediction into persistent storage."""
        # Explicit mask avoids data-dependent mask construction and prevents
        # reading stale values outside this request's valid prefix.
        dtype=next(self.thinker.parameters()).dtype
        mask=torch.where(self.columns<=self.position,0.,torch.finfo(dtype).min).to(dtype)[None,None,None,:]
        positions=(self.position+self.delta).reshape(1,1,1).expand(3,1,1)
        output=self.thinker.model(input_ids=self.token,attention_mask=mask,
            position_ids=positions,past_key_values=self.cache,use_cache=True,
            cache_position=self.position)
        self.token.copy_(self.thinker.lm_head(output.last_hidden_state).argmax(-1))
        self.position.add_(1)

    @torch.inference_mode()
    def begin(self,prefill,first_token,delta):
        """Initialize static graph state from the current prefill cache, first token, and RoPE delta."""
        length=prefill.get_seq_length()
        if length>=self.capacity:raise ValueError('Static decoder capacity exceeded')
        for target,source in zip(self.cache.layers,prefill.layers):
            target.keys[:,:,:length].copy_(source.keys)
            target.values[:,:,:length].copy_(source.values)
        self.token.copy_(first_token.reshape(1,1));self.position.fill_(length)
        self.delta.copy_(delta)

    def step(self):
        """Replay one decode step and return its persistent token buffer."""
        self.graph.replay()
        return self.token


class GreedyAccelerator:
    def __init__(self,model,mode='graph',capacity=1024):
        """Store the thinker model, decoding mode, and static cache capacity."""
        if mode not in ('greedy','graph'):raise ValueError(mode)
        self.model=model;self.mode=mode;self.capacity=capacity;self.graph=None
        self.warmup_seconds=0.;self.calls=0;self.fallbacks=0;self.last_timing={}
        self._lock=threading.RLock()

    @torch.inference_mode()
    def warmup(self):
        """Capture a fixed-capacity token decode graph once when graph mode is enabled."""
        if self.mode=='graph' and self.graph is None:
            started=time.perf_counter();self.graph=DecodeGraph(self.model.thinker,self.capacity)
            self.warmup_seconds=time.perf_counter()-started

    @torch.inference_mode()
    def generate(self,inputs,max_new_tokens):
        """Run request generation in inference mode using the configured decoding path."""
        with self._lock:return self._generate(inputs,max_new_tokens)

    def _generate(self,inputs,max_new_tokens):
        """Prefill the multimodal input, then decode tokens until the configured stop or capacity
        condition.
        """
        if max_new_tokens<1:raise ValueError('max_new_tokens must be positive')
        if inputs['input_ids'].shape[0]!=1:raise ValueError('ASR graph requires batch size one')
        thinker=self.model.thinker
        size=inputs['input_ids'].shape[1]
        if self.mode=='graph' and size+max_new_tokens>self.capacity:
            self.fallbacks+=1
            return self.model.generate(**inputs,max_new_tokens=max_new_tokens,do_sample=False,use_cache=True).sequences[:,size:]
        self.warmup()
        started=time.perf_counter()
        # Exactly the original prefill implementation, including full lm_head.
        output=thinker(**inputs,use_cache=True,return_dict=True)
        token=output.logits[:,-1].argmax(-1,keepdim=True)
        delta=thinker.rope_deltas.clone()
        prefill_ms=(time.perf_counter()-started)*1000
        cache=output.past_key_values
        del output
        generated=[token.clone()]
        value=token.item();eos={151643,151645}
        bank_ms=0.
        if value not in eos and max_new_tokens>1:
            if self.mode=='graph':
                t=time.perf_counter();self.graph.begin(cache,token,delta)
                bank_ms=(time.perf_counter()-t)*1000
            for index in range(1,max_new_tokens):
                if self.mode=='graph':token=self.graph.step()
                else:
                    position=torch.tensor([size+index-1],device=token.device)
                    positions=(position+delta).reshape(1,1,1).expand(3,1,1)
                    result=thinker.model(input_ids=token,past_key_values=cache,use_cache=True,
                        cache_position=position,position_ids=positions)
                    token=thinker.lm_head(result.last_hidden_state).argmax(-1)
                generated.append(token.clone())
                if token.item() in eos:break
        self.calls+=1
        self.last_timing={'prefill_enqueue_ms':prefill_ms,'cache_copy_enqueue_ms':bank_ms,
            'generated_tokens':len(generated),'mode':self.mode}
        return torch.cat(generated,dim=1)
