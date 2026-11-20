import pytest


@pytest.fixture
def local_clarification_checkpoint(tmp_path):
    """Provide the files required for checkpoint resolution without loading weights."""
    checkpoint = tmp_path / "checkpoint"
    (checkpoint / "adapter").mkdir(parents=True)
    (checkpoint / "experiment.json").write_text('{"step": 936}', encoding="utf-8")
    (checkpoint / "adapter/adapter_config.json").write_text("{}", encoding="utf-8")
    for name in ("head.safetensors", "clarify_head.pt", "adapter/adapter_model.safetensors"):
        (checkpoint / name).write_bytes(b"fixture")
    return checkpoint
