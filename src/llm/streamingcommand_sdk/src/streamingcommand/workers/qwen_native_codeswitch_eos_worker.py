"""R17 one-engine experimental worker with an audited end-token prior guard."""
import json
import sys
import qwen_native_codeswitch_prior6_worker as worker
from asr_eos_prior_guard import install, EOS_TOKEN_ID

install()

# Executed in both the protocol process and vLLM's spawned engine process.
from asr_shared_weight_bank import WeightBankExtension


def audit_prior_guard(self):
    """Check on the active engine that an original EOS prediction survives a competing logit prior."""
    from vllm.v1.sample.logits_processor.builtin import LogitBiasLogitsProcessor
    return {'eos_prior_guard_installed': bool(getattr(LogitBiasLogitsProcessor.apply, '_phono_eos_prior_guard', False)),
            'eos_token_id': EOS_TOKEN_ID}


WeightBankExtension.audit_prior_guard = audit_prior_guard
original_llm = worker.worker.Qwen3ASRModel.LLM


def llm(cls, *args, **kwargs):
    """Initialize the shared engine and verify that its EOS prior guard is active."""
    model = original_llm(*args, **kwargs)
    assert model.processor.tokenizer.eos_token_id == EOS_TOKEN_ID
    audits = model.model.collective_rpc('audit_prior_guard')
    assert audits and all(row['eos_prior_guard_installed'] and row['eos_token_id'] == EOS_TOKEN_ID for row in audits)
    print(json.dumps({'actual_engine_eos_prior_guard_audit': audits}), file=sys.stderr, flush=True)
    return model


worker.worker.Qwen3ASRModel.LLM = classmethod(llm)

if __name__ == '__main__':
    raise SystemExit(worker.worker.server.main())
