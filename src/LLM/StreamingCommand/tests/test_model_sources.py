import json
from pathlib import Path

import pytest

from streamingcommand import CommandConfig, StreamingConfig
from streamingcommand._runtime.paths import ASSETS
from streamingcommand.cli import main
from streamingcommand.config import ASR_RESOURCES, HYBRID_MODEL, PUBLISHED_MODEL
from streamingcommand.resources import ModelStore, sha256


def test_presets_declare_all_active_models():
    """Verify source declarations and model identities in both shipped deployment presets."""
    folder = ASSETS / "presets"
    local = StreamingConfig.load(folder / "local-latest.json")
    assert local.command.model == HYBRID_MODEL
    assert local.models[local.command.model]["expected_step"] == 1392
    assert local.command.recognition_logic == "semantic-pair" and local.offline
    assert set(local.models) == {*ASR_RESOURCES, "llm-base", local.command.model}
    assert all(s["source"] == "local" and s["path"] for s in local.models.values())
    release = StreamingConfig.load(folder / "huggingface-release.json")
    assert release.command.model == HYBRID_MODEL
    assert release.command.recognition_logic == "original"
    assert release.asr.profile == local.asr.profile == "v17-yue-precision"
    assert release.models[HYBRID_MODEL]["expected_step"] == 1392
    assert release.command.candidates == local.command.candidates == json.loads(
        (ASSETS / "config/candidate_profiles/endoscope-hybrid.json").read_text())
    assert set(release.models) == {*ASR_RESOURCES, "llm-base", HYBRID_MODEL}
    for name, source in release.models.items():
        assert source == ModelStore().source(name)
        assert source["source"] == "huggingface"
    template = StreamingConfig.load(ASSETS / "settings.json")
    for name in ASR_RESOURCES:
        assert template.models[name] == ModelStore().source(name)


@pytest.mark.parametrize("entry", [
    {"source": "local"},
    {"source": "local", "path": ".", "expected_step": 3},
    {"source": "huggingface", "url": "org/repo", "revision": "1" * 40},
    {"source": "huggingface", "url": "https://huggingface.co/org/repo", "revision": "main"},
    {"source": "huggingface", "url": "https://huggingface.co/org/repo", "revision": "1" * 40,
     "subfolder": "../model"},
])
def test_invalid_sources(entry):
    """Reject missing local paths and malformed Hugging Face URLs, revisions, or subfolders."""
    with pytest.raises(ValueError):
        StreamingConfig(models={"asr": entry})


def test_relative_local_path_conflicts_and_missing_files(tmp_path, monkeypatch):
    """Check config-relative paths, conflicting overrides, and missing resource errors."""
    directory = tmp_path / "models"
    directory.mkdir()
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"models": {"asr": {"source": "local", "path": "models"}}}))
    monkeypatch.chdir(tmp_path.parent)
    config = StreamingConfig.load(path)
    store = ModelStore.from_config(config)
    assert store.ensure("asr") == directory
    assert store.source("asr") == {"source": "local", "path": str(directory)}
    config.asr.model_dir = str(directory)
    with pytest.raises(ValueError, match="either models.asr"):
        ModelStore.from_config(config)
    with pytest.raises(FileNotFoundError, match="Incompatible checkpoint"):
        store.ensure(PUBLISHED_MODEL, override=directory)


def test_local_checkpoint_step_is_checked(local_clarification_checkpoint):
    """Fail a local resource lookup when the checkpoint step differs from the configured value."""
    name = "clarification-latest"
    checkpoint = local_clarification_checkpoint
    store = ModelStore.from_config(StreamingConfig(models={
        name: {"source": "local", "path": str(checkpoint), "expected_step": 936},
    }))
    assert store.ensure(name) == checkpoint
    wrong = ModelStore.from_config(StreamingConfig(models={
        name: {"source": "local", "path": str(checkpoint), "expected_step": 3003},
    }))
    with pytest.raises(ValueError, match="expected step 3003, found 936"):
        wrong.ensure(name)


def test_explicit_source_takes_precedence_over_worker_model_path(tmp_path, monkeypatch):
    """Ensure a config source overrides an existing worker resource path for the same model."""
    from streamingcommand._runtime import paths

    name = "clarification-new"
    monkeypatch.setitem(paths._overrides, name, str(tmp_path))
    assert ModelStore.from_config(StreamingConfig()).source(name) == {"source": "local", "path": str(tmp_path)}
    source = {"source": "package", "path": "assets/models/" + name}
    assert ModelStore.from_config(StreamingConfig(models={name: source})).source(name) == source


@pytest.mark.parametrize("isolated", [False, True])
def test_custom_snapshot_uses_configured_revision_and_subfolder(tmp_path, monkeypatch, isolated):
    """Check the repo, pinned revision, offline mode, and subfolder supplied to custom snapshot downloads."""
    import huggingface_hub
    from huggingface_hub import constants

    calls = []
    monkeypatch.delenv("STREAMINGCOMMAND_CACHE", raising=False)
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(tmp_path / "shared-hub"))

    def snapshot_download(**kwargs):
        """Record the Hugging Face request and return a temporary checkout for path resolution."""
        calls.append(kwargs)
        snapshot = tmp_path / kwargs["revision"]
        (snapshot / "vad").mkdir(parents=True)
        return str(snapshot)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)
    for rev in ("a" * 40, "b" * 40):
        store = ModelStore.from_config(StreamingConfig(cache_dir=str(tmp_path) if isolated else None, offline=True, models={
            "speech": {"source": "huggingface", "url": "https://huggingface.co/owner/repo",
                       "revision": rev, "subfolder": "vad"},
        }))
        assert store.ensure("speech") == tmp_path / rev / "vad"
        assert calls[-1] == dict(repo_id="owner/repo", revision=rev, local_files_only=True,
                                 allow_patterns="vad/*", cache_dir=str(tmp_path / ("hub" if isolated else "shared-hub")))


@pytest.fixture
def model_cache(tmp_path, monkeypatch):
    """Use a small nested model fixture in isolated shared and legacy cache roots."""
    from huggingface_hub import constants
    import streamingcommand.resources as resources

    monkeypatch.delenv("STREAMINGCOMMAND_CACHE", raising=False)
    monkeypatch.setattr(resources, "CACHE", tmp_path / "app")
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(tmp_path / "hf"))
    payload = tmp_path / "payload"
    payload.write_bytes(b"checked model bytes")
    store = ModelStore()
    spec = store.manifest["speech"] = {
        "repo_id": "owner/repo", "revision": "a" * 40, "prefix": "vad/",
        "files": {"nested/model.onnx": {"size": payload.stat().st_size, "sha256": sha256(payload)}},
    }
    return store, spec, payload


def test_pinned_download_reuses_shared_cache_in_place(model_cache, monkeypatch):
    """Read the public snapshot in place and avoid a second SDK copy or offline download."""
    import huggingface_hub

    store, spec, payload = model_cache
    calls = []

    def download(repo_id, revision, filename, cache_dir):
        """Materialize a file using the Hub's on-disk structure."""
        calls.append((repo_id, revision, filename, cache_dir))
        path = Path(cache_dir) / "models--owner--repo" / "snapshots" / revision / filename
        path.parent.mkdir(parents=True)
        path.write_bytes(payload.read_bytes())
        return str(path)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    first = store.ensure("speech")
    assert first == store.hub_cache_dir / "models--owner--repo" / "snapshots" / spec["revision"] / "vad"
    assert calls == [(spec["repo_id"], spec["revision"], "vad/nested/model.onnx", str(store.hub_cache_dir))]
    assert not store.root.exists()
    store.offline = True
    assert store.ensure("speech") == first
    assert len(calls) == 1
    (first / "nested/model.onnx").write_bytes(b"corrupt archive")
    with pytest.raises(FileNotFoundError, match="Offline model is missing or corrupt: speech"):
        store.ensure("speech")


@pytest.mark.parametrize("legacy_layout", ["hub", "models"])
def test_offline_reads_previous_streamingcommand_cache(model_cache, legacy_layout):
    """Check both old private-cache formats remain usable without fetching any files."""
    store, spec, payload = model_cache
    store.offline = True
    if legacy_layout == "hub":
        path = store.root.parent / "hub/models--owner--repo/snapshots" / spec["revision"] / "vad"
    else:
        path = store.root / "speech"
    (path / "nested").mkdir(parents=True)
    (path / "nested/model.onnx").write_bytes(payload.read_bytes())
    assert store.ensure("speech") == path
    assert not store.hub_cache_dir.exists()


def test_release_cli_shows_model_without_loading_and_downloads_active_resources(tmp_path, monkeypatch, capsys):
    """Verify that CLI diagnostics display the release sources and download selects just the active
    resources.
    """
    path = ASSETS / "presets" / "huggingface-release.json"
    calls = []

    def ensure(store, name):
        """Record a requested resource and return a test path without performing downloads."""
        calls.append((name, store.source(name)))
        return tmp_path / name

    monkeypatch.setattr(ModelStore, "ensure", ensure)
    main(["--config", str(path), "doctor"])
    report = json.loads(capsys.readouterr().out)
    assert not calls
    assert report["command_model"] == HYBRID_MODEL and report["asr_profile"] == "v17-yue-precision"
    main(["--config", str(path), "download"])
    assert dict(calls) == report["models"]
    with pytest.raises(ValueError, match="original"):
        StreamingConfig(command=CommandConfig(model=PUBLISHED_MODEL, recognition_logic="semantic-pair"))


@pytest.mark.parametrize(("command", "target", "status"), [
    (0, 1, "NON_COMMAND"), (1, 0, "UNRESOLVED"), (1, 1, "EXECUTE"),
])
def test_release_decodes_noncommand_unresolved_and_business_none(command, target, status):
    """Check that the release runtime distinguishes a non-command, unresolved slots, and an explicit none
    candidate.
    """
    import torch
    from streamingcommand._runtime.llm_ranker import QwenEncoder
    from streamingcommand._runtime.published_command_runtime import PublishedCommandRuntime

    class Tokenizer:
        pad_token_id = 1

        def encode(self, text, add_special_tokens=False):
            """Return a deterministic stand-in token for any text passed to the fixture tokenizer."""
            return [1]

    runtime = PublishedCommandRuntime.__new__(PublishedCommandRuntime)
    runtime.device = "cpu"
    runtime.encoder = QwenEncoder(Tokenizer())

    def forward(**kwargs):
        """Return controlled command and slot logits to exercise release decoding without real weights."""
        return {"command": torch.tensor([[1 - command, command]]),
                "slots": torch.tensor([[[0, 1], [1 - target, target], [0, 1]]])}

    runtime.model = forward
    result = runtime.parse_turn("Bend.", {"I": ["bend"], "T": ["none"], "P": ["none"]}, {})
    assert result["status"] == status
    assert result["execute"] == (status == "EXECUTE")
    assert result["slots"]["T"] == ("none" if command and target else None)
    assert not result["clarification_required"]
