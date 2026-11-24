"""Serializable configuration; independent of application paths and GPU UUIDs."""
from dataclasses import dataclass, field, fields
from pathlib import Path, PureWindowsPath
import re
import json
import copy

HYBRID_MODEL = "clarification-hybrid"


def _validate_candidates(value):
    """Copy an ordered I/T/P pool after checking uniqueness and the empty-P command restriction."""
    if not isinstance(value, dict) or set(value) != {"I", "T", "P"}:
        raise ValueError("candidates must contain independent I/T/P lists")
    for slot, values in value.items():
        if (not isinstance(values, list) or (not values and slot != "P") or
                any(not isinstance(v, str) or not v.strip() or v == "__unresolved__" for v in values) or
                len(set(values)) != len(values)):
            raise ValueError(f"Invalid {slot} candidate list")
    if not value["P"]:
        if not set(value["I"]) <= {"give", "stop", "reset"}:
            raise ValueError("Empty P requires give/stop/reset candidates")
    elif any("none" not in value[s] for s in ("T", "P")):
        raise ValueError("T/P candidate lists must include business value none")
    return copy.deepcopy(value)


@dataclass
class ASRConfig:
    backend: str = "native"
    device: str = "auto"
    profile: str = "v15-canto-commit"
    python: str | None = None
    model_dir: str | None = None
    alternate_model_dir: str | None = None
    language_model_dir: str | None = None
    speech_model_dir: str | None = None
    expected_gpu_uuid: str | None = None
    warmup: bool = True
    num_workers: int = 4
    compatibility_lock_file: str | None = None

    def __post_init__(self):
        """Check that the ASR backend exists and that the profile uses a single engine."""
        if type(self.num_workers) is not int or not 1 <= self.num_workers <= 32:
            raise ValueError("asr.num_workers must be an integer from 1 to 32")
        if self.backend not in {"native", "vllm", "vllm-builtin", "fake"}:
            raise ValueError("ASR backend must be native, vllm, vllm-builtin or fake")
        if self.profile == "v15-speech-aware-dual":
            raise ValueError("Parallel ASR engines are unsupported; use v15-speech-aware")


@dataclass
class CommandConfig:
    enabled: bool = True
    num_workers: int = 4
    model: str = HYBRID_MODEL
    device: str = "auto"
    python: str | None = None
    checkpoint: str | None = None
    base_model_dir: str | None = None
    candidates_file: str | None = None
    recognition_logic: str = "original"
    use_cuda_graphs: bool = True
    expected_gpu_uuid: str | None = None
    candidates: dict[str, list[str]] | None = None
    candidate_profiles_dir: str | None = None

    def __post_init__(self):
        """Validate inline candidates and require a single candidate source."""
        if type(self.num_workers) is not int or not 1 <= self.num_workers <= 32:
            raise ValueError("command.num_workers must be an integer from 1 to 32")
        if self.candidates is not None and self.candidates_file is not None:
            raise ValueError("Choose either command.candidates or command.candidates_file")
        if self.candidates is not None:
            self.candidates = _validate_candidates(self.candidates)


PUBLISHED_MODEL = "phonocommand-llm-0.8b"
ASR_RESOURCES = ("asr", "asr-base", "language", "speech")
BUNDLED_CHECKPOINTS = ("clarification-new", "clarification-latest")
CLARIFICATION_MODELS = (*BUNDLED_CHECKPOINTS, HYBRID_MODEL)
CHECKPOINTS = (*CLARIFICATION_MODELS, PUBLISHED_MODEL)


def _validate_sources(models):
    """Verify model names, source-specific fields, and pinned Hugging Face revisions."""
    valid_names = {*ASR_RESOURCES, "llm-base", *CHECKPOINTS}
    if not isinstance(models, dict) or set(models) - valid_names:
        raise ValueError(f"models must map known resource names: {sorted(valid_names)}")
    for name, entry in models.items():
        if not isinstance(entry, dict):
            raise ValueError(f"models.{name} must be a source object")
        if "expected_step" in entry:
            step = entry["expected_step"]
            if name not in CHECKPOINTS or type(step) is not int or step < 0:
                raise ValueError(f"models.{name}: expected_step requires a nonnegative checkpoint step")
        kind = entry.get("source")
        if kind in ("local", "package"):
            path = entry.get("path")
            if not isinstance(path, str) or not path.strip():
                raise ValueError(f"models.{name}: {kind} requires an explicit path")
            if set(entry) - {"source", "path", "expected_step"}:
                raise ValueError(f"models.{name}: unexpected {kind} source fields")
            if kind == "package" and (name not in BUNDLED_CHECKPOINTS or path != f"assets/models/{name}"):
                raise ValueError(f"models.{name}: no such bundled checkpoint")
        elif kind == "huggingface":
            url, revision = entry.get("url"), entry.get("revision")
            if not isinstance(url, str) or not re.fullmatch(r"https://huggingface\.co/[\w-][\w.-]*/[\w-][\w.-]*", url):
                raise ValueError(f"models.{name}: expected a Hugging Face model repository URL")
            if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
                raise ValueError(f"models.{name}: revision must be a full commit SHA")
            folder = entry.get("subfolder", "")
            if not isinstance(folder, str) or (folder and (
                    PureWindowsPath(folder).drive or any(p in ("", ".", "..") for p in folder.split("/"))
                    or any(c in folder for c in "\\*?[]"))):
                raise ValueError(f"models.{name}: invalid repository subfolder")
            if set(entry) - {"source", "url", "revision", "subfolder", "expected_step"}:
                raise ValueError(f"models.{name}: unexpected Hugging Face source fields")
        else:
            raise ValueError(f"models.{name}: choose local, huggingface or package")


@dataclass
class StreamingConfig:
    asr: ASRConfig = field(default_factory=ASRConfig)
    command: CommandConfig = field(default_factory=CommandConfig)
    interval_ms: int = 800
    endpoint_ms: int = 240
    max_segment_ms: int = 16000
    event_queue_size: int = 256
    max_pending_audio_ms: int = 30000
    cache_dir: str | None = None
    logs_dir: str | None = None
    offline: bool = False
    parallel_start: bool = False
    models: dict[str, dict[str, str | int]] = field(default_factory=dict)

    def __post_init__(self):
        """Check model and recognition-logic compatibility along with segment and queue capacities."""
        _validate_sources(self.models)
        if type(self.parallel_start) is not bool:
            raise ValueError("parallel_start must be a boolean")
        if self.command.enabled and self.command.model not in CHECKPOINTS:
            raise ValueError(f"Unknown command.model: {self.command.model}")
        if self.command.enabled and self.command.model == PUBLISHED_MODEL and self.command.recognition_logic != "original":
            raise ValueError("The published candidate ranker requires original recognition logic")
        if self.interval_ms < 20 or self.endpoint_ms < 20 or self.max_segment_ms < self.interval_ms:
            raise ValueError("Invalid streaming intervals")
        if self.event_queue_size < 1 or self.max_pending_audio_ms < self.max_segment_ms:
            raise ValueError("Queue capacities must be positive and cover one maximum segment")

    @classmethod
    def load(cls, path):
        """Read a JSON config and anchor local resource paths to that file's directory."""
        path = Path(path)
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON configuration object")
        allowed = {f.name for f in fields(cls)}
        if set(value) - allowed:
            raise ValueError(f"Unknown configuration keys: {sorted(set(value)-allowed)}")
        value["asr"] = ASRConfig(**value.get("asr", {}))
        value["command"] = CommandConfig(**value.get("command", {}))
        # User paths are relative to their config, never to the current cwd.
        for obj, names in [(value["asr"], ("python", "model_dir", "alternate_model_dir", "language_model_dir", "speech_model_dir", "compatibility_lock_file")),
                           (value["command"], ("python", "checkpoint", "base_model_dir", "candidates_file", "candidate_profiles_dir"))]:
            for name in names:
                item = getattr(obj, name)
                if item and not Path(item).is_absolute():
                    setattr(obj, name, str((path.parent / item).absolute()))
        for name in ("cache_dir", "logs_dir"):
            if value.get(name) and not Path(value[name]).is_absolute():
                value[name] = str((path.parent / value[name]).absolute())
        config = cls(**value)
        for entry in config.models.values():
            if entry["source"] == "local":
                entry["path"] = str((path.parent / Path(entry["path"]).expanduser()).absolute())
        return config
