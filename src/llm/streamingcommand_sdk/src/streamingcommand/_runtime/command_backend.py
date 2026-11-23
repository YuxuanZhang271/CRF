"""Async adapter for the resident, trained one-forward semantic parser."""
from __future__ import annotations

import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent

from ..config import _validate_candidates as validate_candidates
from .paths import candidate_root
DEFAULT_CANDIDATES = candidate_root() / "command_candidates.json"


def validate_history(value):
    """Normalize the nonempty H2 and H1 text values in prompt order after checking their schema."""
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value)-{"H1", "H2"} or any(not isinstance(v, str) for v in value.values()):
        raise ValueError("history must map H1/H2 to text")
    return {key: value[key] for key in ("H2", "H1") if value.get(key)}

