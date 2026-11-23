"""Named, editable candidate JSON files; independent of model weights and ASR."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading

from .command_backend import validate_candidates

from ..config import HYBRID_MODEL
from .paths import ASSETS, candidate_root
CONFIG_ROOT = candidate_root()
PROFILE_ROOT = CONFIG_ROOT / "candidate_profiles"
REGISTRY = CONFIG_ROOT / "candidate_profiles.json"


class CandidateConfigs:
    def __init__(self, directory=PROFILE_ROOT, registry=None):
        """Select the candidate file directory and registry and establish a lock for profile updates."""
        self.directory = Path(directory)
        self.registry = Path(registry) if registry else (REGISTRY if self.directory == PROFILE_ROOT else self.directory.parent / "candidate_profiles.json")
        self._lock = threading.RLock()

    @staticmethod
    def filename(name):
        """Normalize a profile name to a safe single JSON filename."""
        if not isinstance(name, str):
            raise ValueError("请输入配置名称")
        name = name.strip()
        if name.endswith(".json"):
            name = name[:-5]
        if (not name or len(name) > 80 or name.startswith(".") or ".." in name or
                any(not (c.isalnum() or c in " _-") for c in name)):
            raise ValueError("配置名称只能包含文字、数字、空格、下划线或短横线，最多80字")
        return name + ".json"

    def _path(self, name):
        """Build the profile path while rejecting symbolic links."""
        path = self.directory / self.filename(name)
        if path.is_symlink():
            raise ValueError("配置文件不能是符号链接")
        return path

    @staticmethod
    def _write(path, value):
        """Flush a complete JSON record before atomically replacing its destination."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".candidate-", delete=False) as f:
                temp_name = f.name
                json.dump(value, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_name, path)
        finally:
            if temp_name and Path(temp_name).exists():
                Path(temp_name).unlink()

    def _defaults(self):
        """Read and validate the registry that maps model IDs to default profiles."""
        if not self.registry.exists():
            return {"version": 1, "defaults": {}}
        value = json.loads(self.registry.read_text())
        if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("defaults"), dict):
            raise ValueError("候选配置索引格式错误")
        return value

    def default_path(self, model, fallback):
        """Choose the model's registered candidate profile or the supplied fallback."""
        name = self._defaults()["defaults"].get(model)
        if name:
            return self._path(name)
        if model == HYBRID_MODEL:
            profile = self._path("endoscope-hybrid.json")
            if profile.is_file():
                return profile
            return ASSETS / "config/candidate_profiles/endoscope-hybrid.json"
        return Path(fallback)

    def list(self):
        """List readable profiles with slot counts alongside unreadable-file errors."""
        with self._lock:
            files, errors = [], []
            for path in sorted(self.directory.glob("*.json"), key=lambda p: p.name.casefold()):
                try:
                    record = self.read(path.name)
                    files.append({"name": record["name"], "counts": {s: len(v) for s, v in record["candidates"].items()}})
                except (ValueError, OSError) as exc:
                    errors.append({"name": path.name, "error": str(exc)})
            return {"configs": files, "errors": errors, "defaults": self._defaults()["defaults"]}

    def read(self, name):
        """Read and validate one named I/T/P profile under the directory lock."""
        with self._lock:
            path = self._path(name)
            if not path.is_file():
                raise FileNotFoundError("配置文件不存在")
            return {"name": path.name, "candidates": validate_candidates(json.loads(path.read_text()))}

    def save(self, name, candidates, *, create=False):
        """Create or update a profile with separate checks for the two operations."""
        candidates = validate_candidates(candidates)
        with self._lock:
            path = self._path(name)
            if create and path.exists():
                raise FileExistsError("同名配置已存在；请换一个名称")
            if not create and not path.is_file():
                raise FileNotFoundError("配置文件不存在；请先新建或另存为")
            self._write(path, candidates)
            return {"name": path.name, "candidates": candidates}

    def rename(self, name, new_name):
        """Rename a profile and update model defaults, restoring the filename if the registry update fails.
        """
        with self._lock:
            old, new = self._path(name), self._path(new_name)
            if not old.is_file():
                raise FileNotFoundError("配置文件不存在")
            if old == new:
                return self.read(old.name)
            if new.exists():
                raise FileExistsError("同名配置已存在；请换一个名称")
            self.read(old.name)
            index = self._defaults()
            changed = False
            for model, selected in index["defaults"].items():
                if selected == old.name:
                    index["defaults"][model] = new.name
                    changed = True
            old.rename(new)
            try:
                if changed:
                    self._write(self.registry, index)
            except BaseException:
                new.rename(old)
                raise
            return self.read(new.name)
