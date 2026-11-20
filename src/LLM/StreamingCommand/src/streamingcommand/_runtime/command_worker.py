"""Owned command-model JSON-lines process; no application or experiment imports."""
from contextlib import redirect_stdout
import json
import os
import sys
import traceback
from pathlib import Path
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "streamingcommand._runtime"
from . import device_policy
from ..config import PUBLISHED_MODEL


def main():
    """Pin the worker's device and serve serialized load, parse, feedback, and cleanup operations over JSON
    lines.
    """
    expected = device_policy.pin_device()
    runtime = None
    protocol = sys.stdout
    for line in sys.stdin:
        try:
            request = json.loads(line)
            operation = request.pop('op')
            with redirect_stdout(sys.stderr):
                if operation == 'load':
                    import torch
                    torch.set_num_threads(4)
                    actual_uuid = None
                    if device_policy.DEVICE.startswith('cuda:'):
                        actual_uuid = str(torch.cuda.get_device_properties(0).uuid)
                        if expected and actual_uuid.lower().removeprefix('gpu-') != expected.lower().removeprefix('gpu-'):
                            raise RuntimeError('GPU UUID mismatch')
                    if request["model"] == PUBLISHED_MODEL:
                        from .published_command_runtime import PublishedCommandRuntime
                        runtime = PublishedCommandRuntime(request["checkpoint"], request["base_model_dir"],
                                                          device_policy.DEVICE, request.get("use_cuda_graphs", True))
                    else:
                        from .single_command_runtime import SingleCommandRuntime
                        runtime = SingleCommandRuntime(request['checkpoint'], model_id=request['model'],
                            base_model_dir=request.get('base_model_dir'), use_cuda_graphs=request.get('use_cuda_graphs', True))
                    runtime.warmup(request['candidates'])
                    result = {**runtime.identity, 'model_id': request['model'], 'device': device_policy.DEVICE,
                               'device_uuid': actual_uuid, 'backbones': 1, 'adapters': 1,
                               'adapter_merged': request["model"] == PUBLISHED_MODEL}
                elif operation == 'close':
                    result = {}
                elif runtime is None:
                    raise RuntimeError('Load the command model first')
                elif operation == 'parse':
                    result = runtime.parse_turn(**request)
                elif operation == 'parse_pair':
                    if not hasattr(runtime, 'parse_pair'):
                        raise ValueError('Semantic-pair recognition requires a clarification model')
                    result = runtime.parse_pair(**request)
                elif operation == 'feedback':
                    if not hasattr(runtime, 'feedback'):
                        raise ValueError('Feedback requires a clarification model')
                    result = runtime.feedback(**request)
                elif operation in ('clear', 'end'):
                    if hasattr(runtime, "session_id") and runtime.session_id == request.get('session_id'):
                        runtime.clear()
                        if operation == 'end':
                            runtime.session_id = None
                    result = {}
                else:
                    raise ValueError('Unknown operation: ' + operation)
            print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False), file=protocol, flush=True)
            if operation == 'close':
                return
        except Exception as error:
            traceback.print_exc(file=sys.stderr)
            print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False), file=protocol, flush=True)


if __name__ == '__main__':
    main()
