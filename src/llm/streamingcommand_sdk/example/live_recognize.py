"""Stream recognition from the operating system's default microphone."""

import asyncio
from pathlib import Path
import sys

from streamingcommand import StreamingCommand

CONFIG_PATH = Path(__file__).resolve().with_name("settings.json")


async def main() -> None:
    """Load the example config, print live results, and release the microphone on exit."""
    print("正在加载识别模型。", flush=True)
    async with StreamingCommand(config_path=CONFIG_PATH) as recognizer:
        print("正在打开系统默认麦克风。按 Ctrl+C 停止。", flush=True)
        async for event in recognizer.listen():
            kind = event["type"]
            if kind in ("transcript.partial", "transcript.final"):
                print(f"[{kind}]", event["text"], flush=True)
            elif kind == "command.final":
                print("[command.final]", event["status"], event["slots"], flush=True)
            elif kind.endswith(".error"):
                print(f"[{kind}]", event.get("message", event), file=sys.stderr, flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
