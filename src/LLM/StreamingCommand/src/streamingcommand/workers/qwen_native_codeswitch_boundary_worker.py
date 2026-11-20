"""Single shared ASR with actual-engine-verified domain prior boundary protection."""
import json
import sys
import qwen_native_codeswitch_eos_worker
from asr_boundary_prior_guard import install, BOUNDARIES
from asr_shared_weight_bank import WeightBankExtension
import qwen_native_prefix_final_stream_server as server
from qwen_asr import Qwen3ASRModel

install()


def audit_prior_guard(self):
    """Check on the active engine that a logit prior cannot displace an original boundary winner."""
    from vllm.v1.sample.logits_processor.builtin import LogitBiasLogitsProcessor
    return {'eos_prior_guard_installed': bool(getattr(LogitBiasLogitsProcessor.apply, '_phono_eos_prior_guard', False)),
            'eos_token_id': 151645,
            'boundary_prior_guard_installed': bool(getattr(LogitBiasLogitsProcessor.apply, '_phono_boundary_prior_guard', False))}


WeightBankExtension.audit_prior_guard = audit_prior_guard
original_llm = Qwen3ASRModel.LLM


def llm(cls, *args, **kwargs):
    """Install the boundary guard on the engine path and verify it after model initialization."""
    model = original_llm(*args, **kwargs)
    tokenizer = model.processor.tokenizer
    expected = {tokenizer.eos_token_id}
    for text in ('.', ',', '!', '?', ';', ':', '。', '，', '！', '？', '；', '：', '\n'):
        ids = tokenizer.encode(text, add_special_tokens=False)
        if len(ids) == 1 and tokenizer.decode(ids) == text:
            expected.add(ids[0])
    assert expected == set(BOUNDARIES)
    audits = model.model.collective_rpc('audit_prior_guard')
    assert all(row['boundary_prior_guard_installed'] for row in audits)
    print(json.dumps({'actual_engine_boundary_prior_guard_audit': audits}), file=sys.stderr, flush=True)
    return model


Qwen3ASRModel.LLM = classmethod(llm)

if __name__ == '__main__':
    raise SystemExit(server.main())
