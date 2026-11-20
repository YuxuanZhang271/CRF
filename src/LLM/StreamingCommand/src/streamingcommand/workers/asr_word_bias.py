"""Optional positive priors for complete English-word tokens only."""
import re


def whole_word_bias(tokenizer, words, weight):
    """Bias only tokens whose complete encoding matches a supported form of a vocabulary word."""
    weight = float(weight)
    if not 0 <= weight <= 3:
        raise ValueError('Word bias must be between 0 and 3')
    result = {}
    if not weight:
        return result
    for word in words:
        if not isinstance(word, str) or not re.fullmatch('[A-Za-z]+', word):
            raise ValueError('Bias vocabulary accepts complete ASCII English words')
        for form in {word.lower(), word.capitalize()}:
            for text in (form, ' ' + form):
                ids = tokenizer.encode(text, add_special_tokens=False)
                if len(ids) == 1 and tokenizer.decode(ids) == text:
                    result[int(ids[0])] = weight
    return result
