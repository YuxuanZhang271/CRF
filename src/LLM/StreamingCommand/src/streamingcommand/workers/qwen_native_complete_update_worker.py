"""Experimental complete-prefix updates, retaining supported-margin finals."""
import json
import sys
import qwen_native_supported_margins_v3_worker as worker
from qwen_asr import Qwen3ASRModel
from asr_stream_complete_update import complete_update
original_streaming=Qwen3ASRModel.streaming_transcribe

def streaming_transcribe(self,audio,state):
    """Wrap the SDK streaming update so one generation consumes the full available causal prefix."""
    result,audit=complete_update(original_streaming,self,audio,state)
    if audit is not None:
        print(json.dumps({'stream_complete_prefix':audit}),file=sys.stderr,flush=True)
    return result

Qwen3ASRModel.streaming_transcribe=streaming_transcribe
if __name__=='__main__':raise SystemExit(worker.server.main())
