"""Public API. Importing StreamingCommand does not load models or initialize CUDA."""
from .config import ASRConfig, CommandConfig, StreamingConfig
from .pipeline import StreamingASR, StreamingCommand
from .audio import microphone, audio_file

__version__ = "0.1.1"
__all__ = ["StreamingASR", "StreamingCommand", "ASRConfig", "CommandConfig",
           "StreamingConfig", "microphone", "audio_file", "CandidateProfiles"]


def __getattr__(name):
    """Resolve the candidate profile API on demand so importing the package does not open its user
    registry.
    """
    if name == "CandidateProfiles":
        from ._runtime.candidate_configs import CandidateConfigs
        return CandidateConfigs
    raise AttributeError(name)
