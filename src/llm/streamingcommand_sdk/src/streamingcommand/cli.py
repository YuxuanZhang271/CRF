"""Command-line diagnostics, downloads and file/microphone recognition."""
import argparse
import asyncio
import json
import platform
import sys
from .config import ASR_RESOURCES, StreamingConfig


def main(argv=None):
    """Dispatch resource diagnostics, model downloads, or a file or microphone recognition run."""
    parser = argparse.ArgumentParser(prog="streaming-command")
    parser.add_argument("--config", help="JSON config; paths resolve relative to this file")
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("doctor", help="Report dependencies without loading models")
    download = commands.add_parser("download", help="Download and verify pinned model resources")
    download.add_argument("models", nargs="*")
    run = commands.add_parser("run", help="Recognize a file, or listen to the microphone")
    run.add_argument("audio", nargs="?")
    run.add_argument("--asr-only", action="store_true")
    args = parser.parse_args(argv)
    config = StreamingConfig.load(args.config) if args.config else StreamingConfig()
    from .resources import ModelStore
    store = ModelStore.from_config(config)
    active = list(ASR_RESOURCES) if config.asr.backend != "fake" else []
    if config.command.enabled:
        active.extend(["llm-base", config.command.model])
    if args.operation == "doctor":
        import importlib.metadata as metadata
        versions = {}
        for name in ("streamingcommand", "torch", "transformers", "peft", "funasr", "sounddevice", "vllm"):
            try:
                versions[name] = metadata.version(name)
            except metadata.PackageNotFoundError:
                versions[name] = None
        print(json.dumps({"python": sys.executable, "platform": platform.platform(), "versions": versions,
                          "asr_backend": config.asr.backend, "asr_python": config.asr.python or sys.executable,
                          "asr_profile": config.asr.profile,
                          "command_model": config.command.model if config.command.enabled else None,
                          "models": {name: store.source(name) for name in active}}, indent=2))
    elif args.operation == "download":
        for name in args.models or active:
            print(name, store.ensure(name), flush=True)
    else:
        from .pipeline import StreamingCommand, StreamingASR
        async def recognize():
            """Open the selected recognizer and print one JSON object per event until shutdown."""
            kind = StreamingASR if args.asr_only else StreamingCommand
            async with kind(config) as recognizer:
                events = recognizer.transcribe_file(args.audio) if args.audio else recognizer.listen()
                async for event in events:
                    print(json.dumps(event, ensure_ascii=False), flush=True)
        asyncio.run(recognize())


if __name__ == "__main__":
    main()
