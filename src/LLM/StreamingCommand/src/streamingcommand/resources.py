"""Pinned model downloads and offline checks; no model download during pip/import."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from .config import BUNDLED_CHECKPOINTS, CHECKPOINTS, PUBLISHED_MODEL
from ._runtime.paths import ASSETS, CACHE, model_path


def sha256(path):
    """Hash a file incrementally so multi-gigabyte model weights need not fit in memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _matches(path, expected):
    """Check the manifest's size and hash for one resolved model file."""
    return path.is_file() and path.stat().st_size == expected["size"] and sha256(path) == expected["sha256"]


def _snapshot_root(path, filename):
    """Find the model directory containing a possibly nested manifest filename."""
    return Path(path).parents[len(PurePosixPath(filename).parts) - 1]


class ModelStore:
    def __init__(self, cache_dir=None, *, offline=False):
        """Read the bundled model manifest and record the cache root and offline policy."""
        root = Path(cache_dir or os.environ.get("STREAMINGCOMMAND_CACHE") or CACHE).expanduser().absolute()
        self.root = root / "models"
        self._hub_override = root / "hub" if cache_dir or os.environ.get("STREAMINGCOMMAND_CACHE") else None
        self.offline = offline
        self.manifest = json.loads((ASSETS / "models.json").read_text(encoding="utf-8"))
        self._sources = {}

    @property
    def hub_cache_dir(self):
        """Return the effective Hugging Face cache, including any explicit package override."""
        from huggingface_hub.constants import HF_HUB_CACHE

        return self._hub_override or Path(HF_HUB_CACHE).expanduser().absolute()

    @classmethod
    def from_config(cls, config):
        """Use the same resource selection for startup, doctor and download."""
        store = cls(config.cache_dir, offline=config.offline)
        store._sources = {name: dict(entry) for name, entry in config.models.items()}
        asr, cmd = config.asr, config.command
        legacy_paths = {"asr": asr.model_dir, "asr-base": asr.alternate_model_dir,
                        "language": asr.language_model_dir, "speech": asr.speech_model_dir,
                        "llm-base": cmd.base_model_dir, cmd.model: cmd.checkpoint}
        for name, path in legacy_paths.items():
            if path:
                if name in store._sources:
                    raise ValueError(f"Choose either models.{name} or its legacy local path")
                store._sources[name] = {"source": "local", "path": str(Path(path).absolute())}
        # Preserve existing local checkpoints selected through STREAMINGCOMMAND_MODELS.
        for name in BUNDLED_CHECKPOINTS:
            path = model_path(name)
            if name not in store._sources and path != ASSETS / "models" / name and path.is_dir():
                store._sources[name] = {"source": "local", "path": str(path.absolute())}
        return store

    def _default_source(self, name):
        """Resolve a known resource to its pinned upstream entry or its packaged checkpoint."""
        if name in self.manifest:
            spec = self.manifest[name]
            entry = {"source": "huggingface", "url": "https://huggingface.co/" + spec["repo_id"],
                     "revision": spec["revision"]}
            if spec.get("prefix"):
                entry["subfolder"] = spec["prefix"].rstrip("/")
            if "expected_step" in spec:
                entry["expected_step"] = spec["expected_step"]
            return entry
        if name in BUNDLED_CHECKPOINTS:
            path = f"assets/models/{name}"
            if (ASSETS.parent / path).is_dir():
                return {"source": "package", "path": path}
            raise FileNotFoundError(
                f"No checkpoint configured for {name}. Set models.{name} to a local directory "
                "or a pinned Hugging Face repository."
            )
        raise ValueError(f"Unknown model resource: {name}")

    def source(self, name):
        """Effective source without starting workers or downloading weights."""
        return dict(self._sources.get(name) or self._default_source(name))

    def ensure(self, name, override=None):
        """Materialize a configured resource and check the required files and optional checkpoint step."""
        entry = self.source(name) if not override else {"source": "local", "path": override}
        path = self._resolve(name, entry)
        if name in CHECKPOINTS:
            files = ["experiment.json", "head.safetensors"]
            adapter = path if name == PUBLISHED_MODEL else path / "adapter"
            required = [adapter / "adapter_config.json", adapter / "adapter_model.safetensors"]
            if name != PUBLISHED_MODEL:
                files.append("clarify_head.pt")
            required.extend(path / filename for filename in files)
            for file in required:
                if not file.is_file():
                    raise FileNotFoundError(f"Incompatible checkpoint for {name}: missing {file}")
            if "expected_step" in entry:
                metadata = json.loads((path / "experiment.json").read_text(encoding="utf-8"))
                step = metadata.get("global_step" if name == PUBLISHED_MODEL else "step")
                if step != entry["expected_step"]:
                    raise ValueError(f"{name}: expected step {entry['expected_step']}, found {step} in {path}")
        return path

    def _resolve(self, name, entry):
        """Resolve local or packaged files, or return a pinned Hugging Face model directory."""
        if entry["source"] == "local":
            path = Path(entry["path"]).expanduser().absolute()
            if not path.is_dir():
                raise FileNotFoundError(f"Model directory missing for {name}: {path}")
            return path
        if entry["source"] == "package":
            packaged = ASSETS / "models" / name
            integrity = json.loads((packaged / "integrity.json").read_text())
            for filename, expected in integrity["weights"].items():
                if sha256(packaged / filename) != expected:
                    raise ValueError(f"Packaged checkpoint checksum mismatch: {name}/{filename}")
            return packaged
        default = self._default_source(name) if name in self.manifest else {}
        if (entry["url"], entry["revision"], entry.get("subfolder", "")) != (
                default.get("url"), default.get("revision"), default.get("subfolder", "")):
            return self._custom_snapshot(name, entry)
        return self._verified_snapshot(name)

    def _cached_hubs(self):
        """Prefer the configured cache and look for snapshots left by older installations."""
        preferred = self.hub_cache_dir
        legacy = self.root.parent / "hub"
        return (preferred, legacy) if legacy != preferred and legacy.is_dir() else (preferred,)

    def _custom_snapshot(self, name, entry):
        """Resolve a user-supplied pinned revision with the Hugging Face snapshot API."""
        from huggingface_hub import snapshot_download
        from huggingface_hub.errors import LocalEntryNotFoundError

        folder = entry.get("subfolder", "")
        options = dict(repo_id=entry["url"].removeprefix("https://huggingface.co/"),
                       revision=entry["revision"], allow_patterns=f"{folder}/*" if folder else None,
                       local_files_only=self.offline)
        caches = self._cached_hubs() if self.offline else (self.hub_cache_dir,)
        for cache in caches:
            try:
                path = Path(snapshot_download(**options, cache_dir=str(cache))) / folder
            except LocalEntryNotFoundError:
                if cache == caches[-1]:
                    raise
                continue
            if path.is_dir():
                return path
        raise FileNotFoundError(f"Missing {name} subfolder in pinned snapshot: {folder}")

    def _verified_snapshot(self, name):
        """Verify pinned model files in place and reuse older complete caches when available."""
        from huggingface_hub import hf_hub_download, try_to_load_from_cache

        spec = self.manifest[name]
        prefix = spec.get("prefix", "")
        files = spec["files"]
        first = next(iter(files))
        identity = dict(repo_id=spec["repo_id"], revision=spec["revision"])
        for cache in self._cached_hubs():
            source = try_to_load_from_cache(**identity, filename=prefix + first, cache_dir=str(cache))
            if isinstance(source, str):
                path = _snapshot_root(source, first)
                if all(_matches(path / filename, expected) for filename, expected in files.items()):
                    return path
        legacy = self.root / name
        if all(_matches(legacy / filename, expected) for filename, expected in files.items()):
            return legacy
        if self.offline:
            raise FileNotFoundError(f"Offline model is missing or corrupt: {name} in {self.hub_cache_dir}")
        for filename, expected in files.items():
            source = Path(hf_hub_download(**identity, filename=prefix + filename,
                                         cache_dir=str(self.hub_cache_dir)))
            if not _matches(source, expected):
                raise ValueError(f"Downloaded model checksum mismatch: {name}/{filename}")
        return _snapshot_root(source, filename)
