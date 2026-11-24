"""Voice interaction, loaded independently of camera and motion workers."""

from pathlib import Path
import sys

# Use this checkout's vendored SDK even if an editable install points at an
# older copy of CRF. Dependencies still come from the active environment.
sys.path.insert(0, str(Path(__file__).with_name('streamingcommand_sdk') / 'src'))

from .service import LLMService

__all__ = ['LLMService']
