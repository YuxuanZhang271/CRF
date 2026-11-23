from __future__ import annotations
import torch
from torch import nn
from .llm_ranker import QwenEncoder,QwenCandidateRanker
SLOTS="ITP"
class ClarificationEncoder(QwenEncoder):
    def encode(self,record):
        """Append clarification and second-choice queries to the compact prompt without truncating its
        input.
        """
        # False is the original compact prompt. The only new input is this short stage flag.
        working=dict(record)
        if record.get('clarification_input',False):
            working['history']={'Clarification':'true',**record['history']}
        item=super().encode(working)
        item['record']=record
        ids=item['ids'];queries=item['queries']
        for marker in ('Clarify','I2','T2','P2'):
            ids.extend(self.tokens(marker+'?\n'));queries.append(len(ids)-1)
        if len(ids)>self.max_tokens:raise ValueError(f'{len(ids)} tokens exceeds budget; no truncation')
        labels=item['labels']
        labels.append(int(record.get('clarification_required',False)))
        alt=record.get('alternative_slots',dict.fromkeys(SLOTS))
        labels.extend(0 if alt[s] is None else record['candidates'][s].index(alt[s])+1 for s in SLOTS)
        return item

class UnifiedModel(QwenCandidateRanker):
    def __init__(self,parser):
        """Reuse the original backbone and candidate head while initializing a separate clarification flag
        head.
        """
        nn.Module.__init__(self)
        self.language_model=parser.language_model
        self.head=parser.head
        d=self.language_model.config.hidden_size
        self.clarify_head=nn.Sequential(nn.LayerNorm(d),nn.Linear(d,2)).to(next(parser.parameters()).device).float()
        nn.init.zeros_(self.clarify_head[-1].weight)
        with torch.no_grad():self.clarify_head[-1].bias.copy_(torch.tensor([2.,-2.],device=next(parser.parameters()).device))

    def forward(self,input_ids,attention_mask,candidate_positions,candidate_mask,query_positions):
        """Use one backbone forward to score command slots, alternative slots, and whether clarification is
        needed.
        """
        if not self.training and input_ids.shape[0]==1:
            length=input_ids.shape[1]
            causal=torch.ones((length,length),device=input_ids.device,dtype=torch.bool).tril()[None,None]
            mask={'full_attention':causal & attention_mask[:,None,None,:].bool(),'linear_attention':attention_mask}
        else:mask=attention_mask
        hidden=self.backbone(input_ids=input_ids,attention_mask=mask,use_cache=False).last_hidden_state
        with torch.autocast(device_type=input_ids.device.type,enabled=False):
            out=self.head(hidden,candidate_positions,candidate_mask,query_positions[:,:4])
            second=torch.cat((query_positions[:,:1],query_positions[:,5:8]),dim=1)
            out['alternative']=self.head(hidden,candidate_positions,candidate_mask,second)['slots']
            q=hidden.gather(1,query_positions[:,4:5,None].expand(-1,-1,hidden.shape[-1])).squeeze(1).float()
            out['clarify']=self.clarify_head(q).float()
        return out

def action_text(slots):
    """Render resolved model slots as a short phrase for the clarification prompt."""
    if any(slots.get(s) is None for s in SLOTS):return None
    i,t,p=(slots[s] for s in SLOTS)
    if i=='move':return 'pull back' if p=='backward' else ('move in' if p=='forward' else f'move {p}')
    if i=='track':return 'track'+('' if t=='none' else ' '+t)
    return i+('' if p=='none' else ' '+p)

def decode(out,records,include_scores=False):
    """Decode both candidate rankings and the clarification flag, allowing execution only for a resolved
    ordinary command.
    """
    commands=out['command'].argmax(-1).cpu().tolist()
    primary=out['slots'].argmax(-1).cpu().tolist()
    alt=out['alternative'].argmax(-1).cpu().tolist()
    flags=out['clarify'].argmax(-1).cpu().tolist()
    probs=out['clarify'].softmax(-1).cpu().tolist()
    results=[]
    for k,r in enumerate(records):
        # Map 1-based candidate ranks to their original labels and use None for the unresolved index.
        def slots(indices):return {s:None if index==0 else r['candidates'][s][index-1] for s,index in zip(SLOTS,indices)}
        a,b=slots(primary[k]),slots(alt[k])
        clarify=bool(flags[k]);command='Y' if commands[k] else 'N'
        choices=[a,b] if clarify else []
        # No inverse computation or keyword detection here: both proposals are model predictions.
        render=[action_text(x) for x in choices]
        question=('Do you mean '+render[0]+' or '+render[1]+'?') if clarify and all(render) and a!=b else ('What do you mean?' if clarify else None)
        execute=not clarify and command=='Y' and all(a[s] is not None for s in SLOTS)
        result=dict(command=command,slots=a if (command=='Y' or clarify) else dict.fromkeys(SLOTS),
            clarification_required=clarify,alternative_slots=b if clarify else dict.fromkeys(SLOTS),
            choices=choices,question=question,execute=execute,clarification_probability=probs[k][1],
            next_clarification_input=bool(clarify or (r.get('clarification_input',False) and not execute)))
        if include_scores:
            result['scores']={'C':dict(zip(['N','Y'],out['command'][k].softmax(-1).cpu().tolist())),
                'clarify':dict(zip(['false','true'],probs[k]))}
            for label,tensor in [('primary',out['slots']),('alternative',out['alternative'])]:
                result['scores'][label]={}
                for i,s in enumerate(SLOTS):
                    names=['<unresolved>',*r['candidates'][s]]
                    values=tensor[k,i,:len(names)].softmax(-1).cpu().tolist()
                    result['scores'][label][s]=sorted([dict(value=n,probability=p) for n,p in zip(names,values)],key=lambda x:-x['probability'])
        results.append(result)
    return results
