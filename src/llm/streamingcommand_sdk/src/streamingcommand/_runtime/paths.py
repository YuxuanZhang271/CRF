"""Resource paths shared by copied inference code, with no research-tree dependency."""
import json
import os
from pathlib import Path
from platformdirs import user_cache_path, user_log_path, user_config_path

ASSETS = Path(__file__).resolve().parents[1] / "assets"
WORKERS = Path(__file__).resolve().parents[1] / "workers"
CACHE = Path(os.environ.get("STREAMINGCOMMAND_CACHE", user_cache_path("StreamingCommand")))
LOGS = Path(os.environ.get("STREAMINGCOMMAND_LOGS", user_log_path("StreamingCommand")))
_overrides = json.loads(os.environ.get("STREAMINGCOMMAND_MODELS", "{}"))


def model_path(name):
    """Choose an override, packaged resource, or user cache directory in that order."""
    if name in _overrides:
        return Path(_overrides[name])
    packaged = ASSETS / "models" / name
    return packaged if packaged.is_dir() else CACHE / "models" / name


def worker_path(name):
    """Locate an ASR worker script relative to the installed package."""
    return WORKERS / name


def candidate_root():
    """Install absent candidate defaults into the user directory under a file lock."""
    from filelock import FileLock
    import shutil
    directory = Path(os.environ.get("STREAMINGCOMMAND_CONFIG", user_config_path("StreamingCommand")))
    directory.mkdir(parents=True, exist_ok=True)
    with FileLock(str(directory / ".profiles.lock")):
        for src in (ASSETS / "config").rglob("*.json"):
            dst = directory / src.relative_to(ASSETS / "config")
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
    return directory
