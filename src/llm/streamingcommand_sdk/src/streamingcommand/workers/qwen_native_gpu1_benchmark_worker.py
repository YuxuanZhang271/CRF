"""GPU1 wrapper: skip the optional naming query across all physical GPUs.

The local CUDA platform differs only at the all-device warning invocation.
Selected-device checks and inference remain unchanged. Spawned workers inherit
the same setup; installed vLLM files are not edited.
"""
import contextlib
from pathlib import Path
import sys

sys.modules['kernels'] = None
with contextlib.redirect_stdout(sys.stderr):
    import vllm.platforms
    overlay = Path(__file__).resolve().parents[1] / '.runtime/gpu1-only-platform-20261022'
    vllm.platforms.__path__.insert(0, str(overlay))

import qwen_native_safe_stream_server as worker

if __name__ == '__main__':
    raise SystemExit(worker.main())
