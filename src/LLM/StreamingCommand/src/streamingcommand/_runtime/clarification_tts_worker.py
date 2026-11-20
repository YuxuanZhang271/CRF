"""CPU-only Kokoro worker, using a pinned model and voice revision."""
import base64
from contextlib import redirect_stdout
import io
import json
import os
import sys
import wave

os.environ["CUDA_VISIBLE_DEVICES"] = ""
REVISION = "f3ff3571791e39611d31c381e3a41a3af07b4987"
REPO = "hexgrad/Kokoro-82M"
VOICE = "af_heart"


def main():
    """Load pinned Kokoro weights lazily and serve validated CPU WAV synthesis over JSON lines."""
    pipeline = None
    for line in sys.stdin:
        try:
            request = json.loads(line)
            with redirect_stdout(sys.stderr):
                if pipeline is None:
                    import torch
                    from huggingface_hub import hf_hub_download
                    from kokoro import KModel, KPipeline
                    torch.set_num_threads(4)
                    config = hf_hub_download(REPO, "config.json", revision=REVISION)
                    weights = hf_hub_download(REPO, "kokoro-v1_0.pth", revision=REVISION)
                    voice = hf_hub_download(REPO, f"voices/{VOICE}.pt", revision=REVISION)
                    model = KModel(repo_id=REPO, config=config, model=weights, disable_complex=True).to("cpu").eval()
                    pipeline = KPipeline(lang_code="a", model=model, repo_id=REPO, device="cpu")
                text = request["text"]
                if not isinstance(text, str) or not text.strip() or len(text) > 500:
                    raise ValueError("Speech text must contain 1–500 characters")
                import numpy as np
                with torch.inference_mode():
                    parts = list(pipeline(text, voice=voice, speed=1.0))
                if not parts:
                    raise ValueError("Kokoro produced no speech")
                audio = np.concatenate([p.audio.cpu().numpy() for p in parts])
                if not len(audio) or not np.isfinite(audio).all():
                    raise ValueError("Invalid speech samples")
                buf = io.BytesIO()
                with wave.open(buf, "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(24000)
                    wav.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
            response = dict(ok=True, audio=base64.b64encode(buf.getvalue()).decode(),
                            sample_rate=24000, duration_s=len(audio) / 24000,
                            engine="Kokoro-82M", revision=REVISION, voice=VOICE, device="cpu")
        except Exception as exc:
            import traceback
            traceback.print_exc(file=sys.stderr)
            response = dict(ok=False, error=str(exc))
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
