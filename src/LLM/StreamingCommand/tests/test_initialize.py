import json
from importlib.resources import files

import pytest

from streamingcommand import CommandConfig, StreamingASR, StreamingCommand, StreamingConfig
from streamingcommand._runtime.selectable_commands import SelectableCommandParser


def test_template_matches_shipped_endoscope_profile():
    """Compare the template's inline candidates with the hybrid endoscope defaults."""
    assets = files("streamingcommand").joinpath("assets")
    config = StreamingConfig.load(assets.joinpath("settings.json"))
    profile = assets.joinpath("config/candidate_profiles/endoscope-hybrid.json")
    assert config.command.candidates == json.loads(profile.read_text(encoding="utf-8"))
    assert config.command.model == CommandConfig().model


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["clarification-new", "clarification-latest"])
async def test_initialize_uses_inline_candidates_for_parser_and_session(
    tmp_path, monkeypatch, model, local_clarification_checkpoint
):
    """Check that JSON candidates reach both text parsing and the audio session without requiring model
    weights.
    """
    pool = {"I": ["give", "stop"], "T": ["gauze", "grasper"], "P": []}
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "asr": {"backend": "fake"},
        "command": {"model": model, "device": "cpu", "candidates": pool, "base_model_dir": ".",
                    "checkpoint": str(local_clarification_checkpoint)},
        "offline": True,
    }), encoding="utf-8")

    async def start_without_weights(self):
        """Assert the parser was constructed with the configured candidate pool and skip model loading."""
        assert self.default_candidates == pool

    async def capture_request(self, operation, **kwargs):
        """Echo the parser request fields so the test can inspect the effective candidate pool."""
        return kwargs

    monkeypatch.setattr(SelectableCommandParser, "start", start_without_weights)
    monkeypatch.setattr(SelectableCommandParser, "_request", capture_request)
    sdk = await StreamingCommand.initialize(config_path=path)
    try:
        status = sdk.info()["command"]
        assert status["model_id"] == model
        assert status["candidate_config"] == "inline"
        assert (await sdk.parse("gauze please"))["candidates"] == pool
        alternate = {"I": ["give"], "T": ["scissor"], "P": []}
        assert (await sdk.parse("scissors", candidates=alternate))["candidates"] == alternate
        async with sdk.session() as session:
            assert session.commands.candidates == pool
            assert session.configure_candidates(alternate)["candidates"] == alternate
        assert sdk.info()["command"]["default_candidates"] == pool
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_asr_initialize_and_existing_config_inputs(tmp_path):
    """Exercise the ASR-only initializer and verify that existing constructor config forms still work."""
    path = tmp_path / "settings.json"
    path.write_text('{"asr":{"backend":"fake"}}', encoding="utf-8-sig")
    asr = await StreamingASR.initialize(path)
    try:
        assert asr.info()["ready"] and asr.commands is None
    finally:
        await asr.close()
    assert StreamingCommand(path).config.command.enabled
    config = StreamingConfig.load(path)
    assert not StreamingASR(config).config.command.enabled
    assert config.command.enabled
    with pytest.raises(ValueError, match="either config or config_path"):
        StreamingCommand(config, config_path=path)


def test_candidate_sources_and_invalid_pool():
    """Reject conflicting candidate sources and empty-P pools outside the give/stop/reset contract."""
    with pytest.raises(ValueError, match="candidates_file"):
        CommandConfig(candidates={}, candidates_file="pool.json")
    with pytest.raises(ValueError, match="Empty P"):
        CommandConfig(candidates={"I": ["bend"], "T": ["none"], "P": []})
