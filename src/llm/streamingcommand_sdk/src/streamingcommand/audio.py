"""Built-in audio sources. Callers never need to implement microphone framing."""
import asyncio
import time
from pathlib import Path


class AudioOverflowError(RuntimeError):
    pass


async def microphone(device=None, *, sample_rate=None, block_ms=20, queue_seconds=5):
    """Yield mono PCM16, including silence. An overflow fails visibly, never drops audio."""
    import sounddevice as sd
    import numpy as np
    import soxr
    sample_rate = int(sample_rate or sd.query_devices(device, 'input')['default_samplerate'])
    converter = soxr.ResampleStream(sample_rate, 16000, 1, dtype="float32") if sample_rate != 16000 else None
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue(maxsize=max(1, round(queue_seconds * 1000 / block_ms)))
    failure = None
    stopping = False

    def enqueue(pcm, status):
        """Transfer one microphone block to the async queue and record overflow for the consumer."""
        nonlocal failure
        if stopping:
            return
        if status:
            failure = AudioOverflowError(str(status))
        try:
            queue.put_nowait(pcm)
        except asyncio.QueueFull:
            failure = AudioOverflowError("Microphone consumer is too slow; audio continuity was lost")

    def callback(data, frames, timing, status):
        """Copy a PortAudio input block before passing it from the audio thread to the event loop."""
        loop.call_soon_threadsafe(enqueue, bytes(data), status)

    stream = sd.RawInputStream(samplerate=sample_rate, channels=1, dtype="int16", device=device,
                               blocksize=round(sample_rate * block_ms / 1000), callback=callback)
    try:
        stream.start()
        while True:
            if failure:
                raise failure
            pcm = await queue.get()
            if failure:
                raise failure
            if converter:
                wave = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
                wave = converter.resample_chunk(wave)
                pcm = np.rint(wave * 32768).clip(-32768, 32767).astype("<i2").tobytes()
            if pcm:
                yield pcm
    finally:
        stopping = True
        stream.stop()
        stream.close()


async def audio_file(path, *, realtime=True, block_ms=20):
    """Decode, downmix and continuously resample a file to mono16k PCM16."""
    import numpy as np
    import soundfile as sf
    import soxr
    with sf.SoundFile(str(Path(path))) as source:
        converter = soxr.ResampleStream(source.samplerate, 16000, 1, dtype="float32") if source.samplerate != 16000 else None
        block = max(1, round(source.samplerate * block_ms / 1000))
        total = 0
        started = time.monotonic()
        while True:
            raw = await asyncio.to_thread(source.read, block, dtype="float32", always_2d=True)
            last = len(raw) < block
            mono = np.mean(raw, axis=1, dtype=np.float32)
            if converter:
                mono = converter.resample_chunk(mono, last=last)
            pcm = np.rint(mono * 32768).clip(-32768, 32767).astype("<i2").tobytes()
            if pcm:
                total += len(pcm) // 2
                if realtime:
                    await asyncio.sleep(max(0, started + total / 16000 - time.monotonic()))
                yield pcm
            if last:
                break
