"""Immediate word-level Stop detection on streaming ASR updates."""

import re

STOP_WORD = re.compile(r'(?<![a-z0-9_])stop(?![a-z0-9_])', re.IGNORECASE | re.ASCII)


def has_stop_word(text):
    return isinstance(text, str) and STOP_WORD.search(text) is not None
