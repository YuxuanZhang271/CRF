import json
import pytest
from streamingcommand import CandidateProfiles
from streamingcommand._runtime.command_models import model_history, NEW_MODEL
from streamingcommand._runtime.command_backend import validate_candidates


def test_create_save_rename_and_save_as(tmp_path):
    """Exercise candidate profile creation, replacement, rename, and independent copy behavior."""
    profiles = CandidateProfiles(tmp_path / "profiles")
    pool = {"I": ["give", "stop", "reset"], "T": ["grasper"], "P": []}
    profiles.save("scrub", pool, create=True)
    profiles.save("scrub-copy", profiles.read("scrub")["candidates"], create=True)
    profiles.rename("scrub", "nurse")
    assert profiles.read("nurse")["candidates"] == pool
    assert profiles.read("scrub-copy")["candidates"] == pool
    with pytest.raises(FileExistsError):
        profiles.rename("nurse", "scrub-copy")
    with pytest.raises(ValueError):
        profiles.read("../outside")


def test_h1_mapping_and_absent_p_contract():
    """Verify the current models' H1 normalization and restricted empty-P candidate schema."""
    assert model_history({"H2": "old", "H1": "I=rotate;T=none;P=left"}, NEW_MODEL) == {"H1": "I=self rotate;T=none;P=left"}
    with pytest.raises(ValueError):
        validate_candidates({"I": ["bend"], "T": ["none"], "P": []})
