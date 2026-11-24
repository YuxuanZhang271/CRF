"""One resident LLM worker; explicit idle-only switches never restart ASR."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import sys
import time

from .command_backend import DEFAULT_CANDIDATES, validate_candidates, validate_history
from .candidate_configs import CandidateConfigs
from .command_models import DEFAULT_MODEL, model_specs, model_candidates, model_history
from ..config import PUBLISHED_MODEL
from .device_policy import DEFAULT_DEVICE, resolve_device, worker_visibility

ROOT = Path(__file__).resolve().parent


class CommandWorkerError(ValueError):
    """Preserve the owned worker's traceback for application diagnostics."""
    def __init__(self, result, log_path):
        super().__init__(result.get('error', 'LLM request failed'))
        self.worker_traceback = result.get('traceback')
        self.worker_error_type = result.get('error_type')
        self.worker_log_path = log_path


class SelectableCommandParser:
    def __init__(self, model=DEFAULT_MODEL, *, checkpoint=None, candidates_file=None, candidates=None,
                 device=DEFAULT_DEVICE, logs_root=None, python=None, checkpoint_overrides=None, base_model_dir=None, use_cuda_graphs=True, expected_gpu_uuid=None,
                 checkpoint_sources=None, candidate_profiles_dir=None):
        """Configure checkpoint specs, candidate profiles, and an isolated command worker without loading
        it.
        """
        self.device, self.gpu_uuid = resolve_device(device)
        self.gpu_uuid = expected_gpu_uuid or self.gpu_uuid
        self.python = str(python or sys.executable)
        self.base_model_dir = base_model_dir
        self.use_cuda_graphs = use_cuda_graphs
        self.num_workers = 4
        self.specs = model_specs(checkpoint)
        if PUBLISHED_MODEL in (checkpoint_overrides or {}):
            self.specs[PUBLISHED_MODEL] = dict(
                id=PUBLISHED_MODEL, label="PhonoCommand-LLM-0.8B · candidate ranker",
                clarification=False, session_memory=False, max_history_depth=1)
        for key, value in (checkpoint_overrides or {}).items():
            self.specs[key]["checkpoint"] = str(Path(value).resolve())
        for key, source in (checkpoint_sources or {}).items():
            self.specs[key]["checkpoint_source"] = source
        if model not in self.specs:
            raise ValueError("Unknown command model")
        self._candidates_file = Path(candidates_file) if candidates_file else None
        self._profiles = CandidateConfigs(candidate_profiles_dir) if candidate_profiles_dir else CandidateConfigs()
        self._inline_candidates = validate_candidates(candidates) if candidates is not None else None
        self.logs_root = Path(logs_root or __import__("streamingcommand._runtime.paths",fromlist=["LOGS"]).LOGS)
        self.status = {"backend": "qwen-prefill", "state": "idle", "ready": False,
                       "is_fake": False, "device": self.device,
                       "requested_device_uuid": self.gpu_uuid, "error": None}
        self._select(model)
        self._lock = asyncio.Lock()
        self._process = None
        self._log = None
        self._closed = False

    def _select(self, model):
        """Install a model's checkpoint and candidate defaults while clearing identity fields from the
        prior model.
        """
        spec = self.specs[model]
        for key in ("base_model", "base_model_id", "adapter_layout", "lora_rank", "initial_ar_adapter",
                    "checkpoint_step", "source_checkpoint", "source_best_step", "parser_checkpoint",
                    "reader_checkpoint", "feedback_checkpoint", "checkpoint_epoch", "reply_routes",
                    "memory_policy", "device_name", "device_uuid", "backbones", "adapters", "adapter_merged",
                    "checkpoint_hashes_verified"):
            self.status.pop(key, None)
        self.checkpoint = Path(spec["checkpoint"])
        candidates = self._inline_candidates
        self._candidate_config_path = None
        if candidates is None:
            candidate_file = self._candidates_file or self._profiles.default_path(model, DEFAULT_CANDIDATES)
            self._candidate_config_path = candidate_file.resolve()
            candidates = validate_candidates(json.loads(candidate_file.read_text(encoding="utf-8-sig")))
        self.default_candidates = model_candidates(candidates, model)
        self.status.update(model_id=model, model_label=spec["label"],
                           checkpoint_source=spec.get("checkpoint_source", {"source": "local", "path": str(self.checkpoint)}),
                           checkpoint=str(self.checkpoint), clarification=spec["clarification"],
                           session_memory=spec.get("session_memory", spec["clarification"]),
                           max_history_depth=spec.get("max_history_depth", 2),
                           candidate_config=self._candidate_config_path.name if self._candidate_config_path else "inline",
                           default_candidates=self.default_candidates,
                           available_models=[s for s in self.specs.values() if not s.get("archived") or s["id"] == model])

    def prepare_candidates(self, candidates):
        """Validate a pool and normalize it for the model's candidate schema."""
        return model_candidates(validate_candidates(candidates), self.status["model_id"])

    def candidate_config_renamed(self, old_path, new_path):
        """Update the tracked candidate file when the active profile is renamed."""
        if self._candidate_config_path == Path(old_path).resolve():
            self._candidate_config_path = Path(new_path).resolve()
            self.status["candidate_config"] = self._candidate_config_path.name
            if self._candidates_file is not None:
                self._candidates_file = self._candidate_config_path

    def prepare_history(self, history):
        """Validate the request history and enforce the selected model's history depth."""
        return model_history(validate_history(history), self.status["model_id"])

    async def _rpc(self, operation, **kwargs):
        """Send one JSON operation to the owned worker and return its result or reported error."""
        if self._process is None or self._process.returncode is not None:
            raise RuntimeError("LLM worker is not running")
        self._process.stdin.write((json.dumps({"op": operation, **kwargs}, ensure_ascii=True) + "\n").encode('utf-8'))
        await self._process.stdin.drain()
        line = await asyncio.wait_for(self._process.stdout.readline(), 240 if operation == "load" else 120)
        if not line:
            raise RuntimeError(f"LLM worker exited; see {self.status.get('log_path')}")
        result = json.loads(line)
        if not result.get("ok"):
            raise CommandWorkerError(result, self.status.get('log_path'))
        return result["result"]

    async def _owned_rpc(self, operation, **kwargs):
        """Keep reply consumption shielded so cancellation does not desynchronize subsequent requests."""
        # Cancellation cannot leave an unread response in the protocol stream.
        task = asyncio.create_task(self._rpc(operation, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            except Exception:
                pass
            raise

    async def _stop_worker(self):
        """Detach and stop the retained worker handle, then close its log."""
        process, self._process = self._process, None
        try:
            if process is not None:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.terminate()  # Only the model worker owned by this backend.
                try:
                    await asyncio.wait_for(process.wait(), 15)
                except asyncio.TimeoutError:
                    with suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
        finally:
            if self._log:
                self._log.close()
                self._log = None
            self.status.pop("worker_pid", None)

    async def _load(self):
        """Resolve worker GPU visibility and load the requested checkpoint in a fresh child process."""
        visible = worker_visibility(self.device, os.environ)
        self.status.update(state="loading", ready=False, error=None)
        self.logs_root.mkdir(parents=True, exist_ok=True)
        path = self.logs_root / f"llm-{self.status['model_id']}-{time.time_ns()}.log"
        self._log = path.open("ab", buffering=0)
        self.status["log_path"] = str(path)
        environment = dict(os.environ, CUDA_VISIBLE_DEVICES=visible,
                           STREAMINGCOMMAND_LOADING_WORKERS=str(self.num_workers),
                           STREAMINGCOMMAND_DEVICE=self.device,
                           STREAMINGCOMMAND_EXPECTED_GPU_UUID=self.gpu_uuid,
                           TOKENIZERS_PARALLELISM="false", OMP_NUM_THREADS="4",
                           PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        try:
            self._process = await asyncio.create_subprocess_exec(
                self.python, "-u", str(ROOT / "command_worker.py"),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=self._log, env=environment, limit=4 * 1024 * 1024)
            self.status["worker_pid"] = self._process.pid
            identity = await self._owned_rpc("load", model=self.status["model_id"],
                                             checkpoint=str(self.checkpoint), candidates=self.default_candidates,
                                             base_model_dir=self.base_model_dir, use_cuda_graphs=self.use_cuda_graphs)
            self.status.update(identity, state="ready", ready=True, error=None)
        except BaseException as exc:
            self.status.update(state="error", ready=False, error=str(exc) or type(exc).__name__)
            await self._stop_worker()
            raise

    async def start(self):
        """Load the selected command model if it is not already ready."""
        async with self._lock:
            if self._closed:
                raise RuntimeError("Command backend is closed")
            if not self.status["ready"]:
                await self._load()

    async def switch(self, model):
        """Release the previous command worker before starting the selected one."""
        if model not in self.specs:
            raise ValueError("Unknown command model")
        async with self._lock:
            if self._closed:
                raise RuntimeError("Command backend is closed")
            if model != self.status["model_id"] or not self.status["ready"]:
                self.status.update(state="switching", ready=False, error=None)
                await self._stop_worker()
                self._select(model)
                await self._load()
            return dict(self.status)

    async def _request(self, operation, **kwargs):
        """Serialize operations against a ready worker and expose fatal transport failures in status."""
        async with self._lock:
            if self._closed or not self.status["ready"]:
                raise RuntimeError("Command model is not ready")
            try:
                return await self._owned_rpc(operation, **kwargs)
            except (RuntimeError, BrokenPipeError, ConnectionError, asyncio.TimeoutError) as exc:
                self.status.update(state="error", ready=False, error=str(exc))
                raise

    async def parse(self, utterance, candidates, history=None):
        """Parse a standalone utterance using the supplied candidates and history."""
        return await self.parse_turn(utterance, candidates, history)

    async def parse_turn(self, utterance, candidates, history=None, *, session_id=None, commit=False):
        """Validate an utterance and send a preview or committing turn with its optional session identity."""
        if not isinstance(utterance, str) or not utterance.strip():
            raise ValueError("utterance must be nonempty text")
        return await self._request("parse", utterance=utterance, candidates=self.prepare_candidates(candidates),
                                   history=self.prepare_history(history), session_id=session_id, commit=commit)

    async def feedback(self, session_id, reply, clarification_id):
        """Validate a bounded natural-language reply and send it with the pending clarification ID."""
        if not isinstance(reply, str) or not reply.strip() or len(reply) > 2000:
            raise ValueError("请输入自然语言回答（最多 2000 字符）")
        return await self._request("feedback", session_id=session_id, reply=reply,
                                   clarification_id=clarification_id)

    async def parse_pair(self, utterance, previous, candidates, history, *, session_id):
        """Request the current and adjacent-context parses for the same session and candidate pool."""
        return await self._request('parse_pair', utterance=utterance, previous=previous,
            candidates=self.prepare_candidates(candidates), history=self.prepare_history(history),
            session_id=session_id)

    async def clear_memory(self, session_id):
        """Reset the command worker's pending state for the identified session."""
        return await self._request("clear", session_id=session_id)

    async def end_session(self, session_id):
        """Release session state if the command worker is still running."""
        if not self._closed and self.status["ready"]:
            await self._request("end", session_id=session_id)

    async def close(self):
        """Stop the command worker under its request lock and prevent further use."""
        async with self._lock:
            self._closed = True
            self.status.update(state="closed", ready=False)
            await self._stop_worker()
