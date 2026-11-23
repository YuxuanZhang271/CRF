"""Owned CPU speech process; async request serialization and bounded WAV cache."""
import asyncio
import base64
from collections import OrderedDict
from contextlib import suppress
import json
import os
from pathlib import Path
import sys
from ._runtime.paths import LOGS


class ClarificationSpeech:
    """Optional CPU speech synthesis for spoken clarification prompts."""

    def __init__(self, logs_root=None, *, offline=False):
        """Create a lazy CPU speech service with serialized requests and a bounded WAV cache."""
        self.logs_root = Path(logs_root or LOGS)
        self.offline = offline
        self.status = dict(engine="Kokoro-82M", device="cpu", state="idle", ready=False)
        self._process = self._log = None
        self._lock = asyncio.Lock()
        self._cache = OrderedDict()
        self._closed = False

    async def _stop(self):
        """Terminate only the owned Kokoro worker and close its log after it exits."""
        process, self._process = self._process, None
        if process and process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.communicate(), 10)
            except asyncio.TimeoutError:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
        if self._log:
            self._log.close()
            self._log = None

    async def _generate(self, text):
        """Send one text request to the resident worker and decode its WAV response."""
        if self._process is None or self._process.returncode is not None:
            await self._stop()
            self.logs_root.mkdir(parents=True, exist_ok=True)
            self._log = (self.logs_root / "kokoro-clarification.log").open("ab", buffering=0)
            self.status.update(state="loading", ready=False)
            self._process = await asyncio.create_subprocess_exec(
                sys.executable, "-u", str(Path(__file__).parent / "_runtime/clarification_tts_worker.py"),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=self._log,
                env={**os.environ, "CUDA_VISIBLE_DEVICES": "", "OMP_NUM_THREADS": "4",
                     "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                     **({"HF_HUB_OFFLINE": "1"} if self.offline else {})}, limit=8 * 1024 * 1024)
        self._process.stdin.write((json.dumps(dict(text=text)) + "\n").encode())
        await self._process.stdin.drain()
        result = json.loads(await self._process.stdout.readline())
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "Kokoro failed"))
        self.status.update(state="ready", ready=True, error=None, voice=result["voice"], revision=result["revision"])
        return base64.b64decode(result["audio"], validate=True)

    async def speak(self, text):
        """Validate and normalize a prompt, reuse cached audio, and reset the worker on protocol failure."""
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise ValueError("Speech text must contain 1–500 characters")
        text = text.strip()
        async with self._lock:
            if self._closed:
                raise RuntimeError("Speech service is closed")
            if text in self._cache:
                self._cache.move_to_end(text)
                return self._cache[text]
            try:
                audio = await asyncio.wait_for(self._generate(text), 90)
            except BaseException as exc:
                # Do not leave a delayed response in the JSON protocol stream.
                await self._stop()
                self.status.update(state="error", ready=False, error=str(exc) or type(exc).__name__)
                raise
            self._cache[text] = audio
            if len(self._cache) > 64:
                self._cache.popitem(last=False)
            return audio

    async def start(self):
        """Warm the worker with a short clarification prompt."""
        await self.speak("Do you mean bend right or pull back?")

    async def close(self):
        """Prevent further synthesis and wait for the owned CPU worker to exit."""
        async with self._lock:
            self._closed = True
            await self._stop()
            self.status.update(state="closed", ready=False)
