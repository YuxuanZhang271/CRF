"""Pure construction of explicitly bounded per-word experimental priors."""
from math import isfinite


def make_bias(tokenizer, spec, whole_word_bias):
    """Build a bounded token prior from a fixed vocabulary and explicitly allowed per-word overrides."""
    if set(spec) != {'context', 'words', 'weight', 'word_weights'}:
        raise ValueError('Invalid explicit ASR prior specification')
    weights = spec['word_weights']
    if not isinstance(weights, dict) or not set(weights) <= set(spec['words']):
        raise ValueError('Overrides must be members of the fixed domain vocabulary')
    if not all(isfinite(float(value)) and 0 <= float(value) <= 8
               for value in [spec['weight'], *weights.values()]):
        raise ValueError('Prior weight must be finite and between zero and eight')
    base = float(spec['weight'])
    if base > 6:
        raise ValueError('Uniform prior remains bounded by six')
    result = whole_word_bias(tokenizer, spec['words'], base)
    for word, value in weights.items():
        value = float(value)
        bounded = min(value, 6.)
        bias = whole_word_bias(tokenizer, [word], bounded)
        result.update({token: score * value / bounded if bounded else 0.
                       for token, score in bias.items()})
    return result
