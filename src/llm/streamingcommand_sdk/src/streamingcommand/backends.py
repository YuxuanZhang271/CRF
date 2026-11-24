"""Factory boundary used by both the public API and Demo integration."""
import os
from pathlib import Path
import sys
from .config import ASR_RESOURCES, StreamingConfig
from .resources import ModelStore


async def build_backends(config: StreamingConfig):
    """Resolve resources off the audio loop, then construct unloaded backends."""
    import asyncio
    from ._runtime import paths
    from ._runtime.device_policy import resolve_device
    store = ModelStore.from_config(config)
    spec = config.asr
    names = list(ASR_RESOURCES) if spec.backend != 'fake' else []
    if config.command.enabled:
        names.extend(('llm-base', config.command.model))
    resolved = await asyncio.to_thread(store.ensure_many, names, parallel=config.parallel_start)
    if spec.backend == "fake":
        from ._runtime.asr_backend import FakeASR
        asr = FakeASR()
    else:
        resources = {name: resolved[name] for name in ASR_RESOURCES}
        # These model locations are resolved once for the single resident runtime.
        paths._overrides.update({name: str(value) for name, value in resources.items()})
        from ._runtime import acoustic_speech
        acoustic_speech.MODEL_DIR = resources["speech"]
        from ._runtime.asr_defaults import build_asr
        device, expected_uuid = resolve_device(spec.device)
        if spec.backend.startswith("vllm") and not device.startswith("cuda:"):
            raise ValueError("vLLM requires a compatible NVIDIA CUDA device; select native for CPU")
        asr = build_asr(spec.profile, device=device)
        asr._instance_lease.compatibility_lock_file = (
            Path(spec.compatibility_lock_file) if spec.compatibility_lock_file else None)
        if hasattr(asr, "_continuity_asr") and not asr.status.get("asr_engine_count") == 1:
            from ._runtime.shared_asr import configure_shared_engine
            configure_shared_engine(asr)
        asr.model_dir = resources["asr"]
        asr.alternate_model_dir = resources["asr-base"] if hasattr(asr, "_continuity_asr") else None
        if hasattr(asr, "_continuity_asr"):
            asr._continuity_asr.model_dir = resources["asr-base"]
        asr.python = Path(spec.python or sys.executable).absolute()
        if spec.backend == "vllm":
            # Explicit legacy SDK backend. It runs in a separately configured interpreter.
            asr.worker_environment["STREAMINGCOMMAND_RUNTIME_CACHE"] = str(store.root.parent / "runtime")
        else:
            asr.worker = paths.WORKERS / "portable_worker.py"
            asr.worker_environment.update(STREAMINGCOMMAND_ASR_BACKEND=spec.backend)
        asr.warmup = spec.warmup
        asr.parallel_start = config.parallel_start
        if hasattr(asr, '_acoustic_language'):
            asr._acoustic_language.parallel_start = config.parallel_start
        asr.worker_environment['STREAMINGCOMMAND_LOADING_WORKERS'] = str(spec.num_workers)
        asr.status['num_workers'] = spec.num_workers
        asr.gpu_uuid = spec.expected_gpu_uuid or expected_uuid
        asr.status.update(device=device, backend=spec.backend, model_dir=str(asr.model_dir), implementation="streamingcommand")
        asr.status["model_sources"] = {name: {**store.source(name), "resolved_path": str(path)}
                                       for name, path in resources.items()}
    if config.logs_dir:
        asr.logs_dir = Path(config.logs_dir)
    command = None
    if config.command.enabled:
        llm = config.command
        base = resolved['llm-base']
        checkpoint = resolved[llm.model]
        from ._runtime.selectable_commands import SelectableCommandParser
        command = SelectableCommandParser(model=llm.model, device=llm.device,
            python=llm.python, base_model_dir=str(base), candidates_file=llm.candidates_file,
            candidates=llm.candidates, candidate_profiles_dir=llm.candidate_profiles_dir,
            checkpoint_overrides={llm.model: checkpoint},
            checkpoint_sources={llm.model: store.source(llm.model)},
            use_cuda_graphs=llm.use_cuda_graphs, logs_root=config.logs_dir, expected_gpu_uuid=llm.expected_gpu_uuid)
        command.status["base_model_source"] = {**store.source("llm-base"), "resolved_path": str(base)}
        command.num_workers = llm.num_workers
        command.status['num_workers'] = llm.num_workers
    return asr, command
