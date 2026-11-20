import json
from pathlib import Path
import pytest


def test_native_banks_roundtrip_preserves_weights_and_addresses(tmp_path, monkeypatch):
    """Check that switching between native banks preserves source bytes and live parameter pointers."""
    import torch
    from safetensors.torch import save_file
    import streamingcommand
    monkeypatch.syspath_prepend(str(Path(streamingcommand.__file__).parent / "workers"))
    from streamingcommand.workers.portable_worker import NativeBanks
    from types import SimpleNamespace
    model = torch.nn.Linear(3, 2, dtype=torch.float16)
    primary = {k: value.detach().clone() for k, value in model.state_dict().items()}
    alternate = {k: value.clone() + .5 for k, value in primary.items()}
    for name, values in (("primary", primary), ("alternate", alternate)):
        folder = tmp_path / name
        folder.mkdir()
        save_file(values, str(folder / "weights.safetensors"))
        (folder / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {key: "weights.safetensors" for key in values}}))
        (folder / "config.json").write_text('{"thinker_config": {}}')
        for file in ("preprocessor_config.json", "tokenizer_config.json", "vocab.json", "merges.txt"):
            (folder / file).write_text("same")
    pointers = {k: v.data_ptr() for k, v in model.named_parameters()}
    bank = NativeBanks(SimpleNamespace(hf_model=model), tmp_path / "primary", tmp_path / "alternate")
    for variant in ("base", "mix75", "v15", "mix75", "base", "v15"):
        bank.select(variant)
        expected = primary if variant == "v15" else alternate if variant == "base" else {k: torch.lerp(alternate[k].float(), primary[k].float(), .75).half() for k in primary}
        assert all(torch.equal(value, expected[name]) for name, value in model.state_dict().items())
        assert pointers == {k: v.data_ptr() for k, v in model.named_parameters()}
        assert len(bank.banks) == 2


def test_native_prior_preserves_original_boundary_logits(monkeypatch):
    """Check that applying domain priors leaves logits unchanged when a boundary token already leads."""
    import torch
    import streamingcommand
    monkeypatch.syspath_prepend(str(Path(streamingcommand.__file__).parent / "workers"))
    from streamingcommand.workers.asr_windows_native import NativePrior
    values = torch.zeros((1, 151646))
    values[0, 151645] = 3
    assert torch.equal(NativePrior({100: 8})(None, values.clone()), values)
    values[0, 100] = 4
    biased = NativePrior({101: 2})(None, values.clone())
    assert biased[0, 101] == 2
