"""Recognize the bundled Cantonese recording with the StreamingCommand SDK."""

import asyncio
from pathlib import Path

from streamingcommand import StreamingASR, StreamingCommand, StreamingConfig

# Resolve bundled files relative to the example so it also runs from other directories.
EXAMPLE_DIR = Path(__file__).resolve().parent
AUDIO_PATH = EXAMPLE_DIR / "cantonese.wav"
CONFIG_PATH = EXAMPLE_DIR / "settings.json"
ASR_ONLY = False


async def recognize(
    audio_path: str | Path, config_path: str | Path = CONFIG_PATH, *, asr_only: bool = False
) -> None:
    """Initialize the SDK, read recognition events, and close the owned models."""
    audio_path = Path(audio_path).expanduser()
    if not audio_path.is_file():
        raise FileNotFoundError(f"Set AUDIO_PATH to an existing recording: {audio_path}")

    config = StreamingConfig.load(Path(config_path).expanduser())
    recognizer_class = StreamingASR if asr_only else StreamingCommand
    sdk = await recognizer_class.initialize(config=config)
    try:
        async for event in sdk.transcribe_file(audio_path):
            kind = event["type"]
            if kind == "transcript.final":
                print("[transcript.final]", event["text"])
            elif kind == "command.final":
                print("[command.final]", event["status"], event["slots"])
            elif kind.endswith(".error"):
                raise RuntimeError(f"{kind}: {event.get('error') or event}")
    finally:
        await sdk.close()


if __name__ == "__main__":
    asyncio.run(recognize(AUDIO_PATH, CONFIG_PATH, asr_only=ASR_ONLY))
