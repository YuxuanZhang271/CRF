"""Isolated prior-strength ablation, same single-engine worker as v3."""
import qwen_native_codeswitch_bias_worker as worker

original_bias = worker.whole_word_bias


def bounded_domain_prior(tokenizer, words, weight):
    """Allow the experiment's bounded uniform prior while reusing the original whole-word matcher."""
    weight = float(weight)
    if not 0 <= weight <= 6:
        raise ValueError('Experimental domain prior must be within [0, 6]')
    base = original_bias(tokenizer, words, min(weight, 3))
    return {token: value * weight / 3 for token, value in base.items()} if weight > 3 else base


worker.whole_word_bias = bounded_domain_prior

if __name__ == '__main__':
    raise SystemExit(worker.server.main())
