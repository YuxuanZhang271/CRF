"""Single ASR engine with optional fixed Cantonese classifier token priors.

Only exact single-token classifier words are eligible, once per token. Scores
are bounded by two; English priors and EOS/punctuation protection stay intact.
This changes native generation logits and never edits generated transcripts.
"""
import json
import sys
from math import isfinite
import qwen_native_canto_fusion_worker as fusion

original_make_bias=fusion.make_bias


def make_bias(tokenizer,spec,whole_word_bias):
    """Extend the inherited prior with bounded exact single-token Cantonese classifier weights."""
    if 'cjk_words' not in spec:return original_make_bias(tokenizer,spec,whole_word_bias)
    if set(spec)!={'context','words','weight','word_weights','cjk_words','cjk_weight'}:
        raise ValueError('Invalid explicit bilingual prior')
    if spec['cjk_words']!=['支','個','把']:
        raise ValueError('Only the fixed balanced classifier vocabulary is supported')
    weight=float(spec['cjk_weight'])
    if not isfinite(weight) or not 0<=weight<=2:raise ValueError('Classifier weight outside [0,2]')
    result=original_make_bias(tokenizer,{k:v for k,v in spec.items() if not k.startswith('cjk_')},whole_word_bias)
    applied={}
    if weight:
        for word in spec['cjk_words']:
            ids=tokenizer.encode(word,add_special_tokens=False)
            if len(ids)==1 and tokenizer.decode(ids)==word:
                result[int(ids[0])]=weight
                applied[word]=int(ids[0])
    print(json.dumps(dict(native_Cantonese_classifier_prior=True,weight=weight,
        applied=applied,extra_ASR_passes=0,transcript_rewrite=False),ensure_ascii=False),file=sys.stderr,flush=True)
    return result


fusion.make_bias=make_bias

if __name__=='__main__':raise SystemExit(fusion.server.main())
