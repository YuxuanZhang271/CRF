"""Measure cached speech startup in fresh processes, without audio or robot devices.

Run with the crf environment: python tests/benchmark_llm_loading.py --repeats 2
Results and worker logs go under ignored logs/llm-loading/ by default.
"""
import argparse
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


async def measure(args):
    sys.path.insert(0, str(ROOT / 'src'))
    import llm  # Select the same vendored SDK as the app.
    from streamingcommand import StreamingCommand, StreamingConfig
    from streamingcommand.resources import ModelStore
    from streamingcommand._runtime.asr_backend import NativeASR
    from streamingcommand._runtime.acoustic_language import AcousticLanguageHint
    from streamingcommand._runtime.acoustic_speech import AcousticSpeechEvidence

    config = StreamingConfig.load(args.config)
    config = replace(config, offline=True, parallel_start=not args.sequential,
                     logs_dir=str(args.output.parent),
                     asr=replace(config.asr, num_workers=args.single),
                     command=replace(config.command, num_workers=args.single))
    resources = {}
    ensure = ModelStore.ensure

    def timed_ensure(store, name, *positional, **keywords):
        started = time.perf_counter()
        try:
            return ensure(store, name, *positional, **keywords)
        finally:
            resources[name] = time.perf_counter() - started
            print(f'  {name}: {resources[name]:.2f}s', file=sys.stderr, flush=True)

    ModelStore.ensure = timed_ensure
    phases = {}
    originals = []
    for cls, method, label in ((NativeASR, 'start', 'asr_engine'),
                               (AcousticLanguageHint, '_start_cpu', 'language_worker'),
                               (AcousticSpeechEvidence, 'start', 'speech_helper')):
        original = getattr(cls, method)

        def timer(original, label):
            async def measured(self, *positional, **keywords):
                started = time.perf_counter()
                try:
                    return await original(self, *positional, **keywords)
                finally:
                    elapsed = time.perf_counter() - started
                    phases.setdefault(label, []).append(elapsed)
                    print(f'  {label}: {elapsed:.2f}s', file=sys.stderr, flush=True)
            return measured

        originals.append((cls, method, original))
        setattr(cls, method, timer(original, label))
    sdk = StreamingCommand(config)
    started = time.perf_counter()
    try:
        await sdk.start()
        info = sdk.info()
        result = dict(workers=args.single, parallel_start=config.parallel_start,
                      wall_seconds=time.perf_counter() - started,
                      resource_timings=resources, phase_timings=phases, info=info)
        # Exercise the loaded parser as well as the ASR's normal startup warmup.
        result['command_check'] = await sdk.commands.parse(
            'Stop.', sdk.commands.default_candidates, history={})
        if args.audio:
            result['audio_events'] = [event async for event in sdk.transcribe_file(
                args.audio, realtime=False)]
        return result
    finally:
        await sdk.close()
        ModelStore.ensure = ensure
        for cls, method, original in originals:
            setattr(cls, method, original)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'src/llm/settings.json')
    parser.add_argument('--workers', type=int, nargs='+', default=[8, 1, 4])
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--sequential', action='store_true')
    parser.add_argument('--audio', type=Path, help='Also recognize a saved audio file after startup')
    parser.add_argument('--output', type=Path, default=ROOT / 'logs/llm-loading/results.json')
    parser.add_argument('--single', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.repeats < 1 or any(not 1 <= n <= 32 for n in args.workers):
        parser.error('repeats must be positive; workers must be 1–32')
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.single is not None:
        print(json.dumps(asyncio.run(measure(args)), ensure_ascii=True), flush=True)
        return
    results = []
    # Reverse order on alternate repeats to reduce filesystem-cache/order bias.
    for repeat in range(args.repeats):
        for workers in args.workers if repeat % 2 == 0 else reversed(args.workers):
            print(f'Run {repeat + 1}, {workers} loading threads per model', flush=True)
            command = [sys.executable, str(Path(__file__).resolve()), '--single', str(workers),
                       '--config', str(args.config.resolve()), '--output', str(args.output)]
            if args.sequential:
                command.append('--sequential')
            if args.audio:
                command.extend(['--audio', str(args.audio.resolve())])
            child = subprocess.run(command, stdout=subprocess.PIPE, text=True, encoding='utf-8',
                                   env={**os.environ, 'PYTHONUTF8': '1'}, check=True)
            result = json.loads(child.stdout)
            results.append(result)
            args.output.write_text(json.dumps(results, indent=2, ensure_ascii=True), encoding='utf-8')
            info = result['info']
            print(f"  total={result['wall_seconds']:.2f}s resources={info['resource_seconds']:.2f}s "
                  f"ASR={info['asr']['startup_seconds']:.2f}s "
                  f"LLM={info['command']['startup_seconds']:.2f}s", flush=True)
    print('\nMedian startup seconds (fresh processes; OS file cache retained):')
    for workers in args.workers:
        rows = [r['wall_seconds'] for r in results if r['workers'] == workers]
        print(f'  {workers} threads: {statistics.median(rows):.2f}s')
    print(f'Full results: {args.output}')


if __name__ == '__main__':
    main()
