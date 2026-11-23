"""Selectable low-latency repair; fixed context and one complete-audio final."""
from __future__ import annotations
from .paths import model_path, worker_path

import asyncio
from dataclasses import replace

from .acoustic_language import AcousticLanguageHint
from .acoustic_vad import DEFAULT_GATE_CONFIG
from .asr_backend import default_context, WHISPER
from .asr_profiles import ASR_PROFILE_VERSIONS


def realtime_asr_class(base_class):
    """Add early partials and a CPU acoustic-language helper to the supplied ASR backend."""
    # Avoid importing the profile factory from a class it constructs.
    class RealtimeV15ASR(base_class):
        def __init__(self, *args, **kwargs):
            """Configure early partial scheduling, tail decoding, and the resident language helper."""
            defaults = dict(context=default_context(),
                            worker=worker_path("qwen_native_complete_prefix_worker.py"))
            super().__init__(*args, **{**defaults, **kwargs})
            self.first_partial_ms = 800
            self.default_endpoint_ms = 240
            self.final_strategy = 'complete_audio_native_prefix_finish_or_fresh'
            self.allow_language_rescore = False
            self.vad_gate_config = replace(DEFAULT_GATE_CONFIG,
                absolute_floor_dbfs=-55, passthrough_before_calibration=False)
            self.prefetch_acoustic_language = True
            self.partial_compute_rest_fraction = 0
            self.preserve_acoustic_native_prefix = True
            self._acoustic_language = AcousticLanguageHint('cpu', condition_english=True, int8=True)
            self.acoustic_language_for_final = self._acoustic_language.classify
            self._repair_start_lock = asyncio.Lock()
            self.status.update(asr_profile='v15-realtime',
                profile_version=ASR_PROFILE_VERSIONS['v15-realtime'],
                first_partial_ms=self.first_partial_ms, default_endpoint_ms=240,
                partial_compute_rest_fraction=0,
                final_strategy=self.final_strategy,
                final_language_policy='single_Qwen_pass_with_full_audio_acoustic_language_hint',
                acoustic_language_helper={'model':'SenseVoiceSmall', 'device':'cpu', 'linear_dtype':'dynamic_int8',
                    'minimum_language_probability':0.95,
                    'conditioning':'confident_English_or_Chinese_unless_matching_native_prefix_available',
                    'purpose':'language_query_position_only', 'helper_words_used':False,
                    'model_dir':str(self._acoustic_language.model_dir)},
                worker=str(self.worker))

        async def start(self):
            """Warm the backend and language helper as one readiness unit, cleaning up on failure."""
            async with self._repair_start_lock:
                if (self.status.get('ready') and self._process is not None
                        and self._process.returncode is None
                        and self._acoustic_language.is_loaded):
                    return self.status.get('worker_ready', dict(self.status))
                try:
                    ready = await super().start()
                    self._phase('warming')
                    self._acoustic_language.logs_dir = self.logs_dir
                    await self._acoustic_language.start()
                    self._phase('ready')
                    return ready
                except BaseException:
                    await self.close()
                    raise

        async def request(self, op, **kwargs):
            """Supply the preferred rollback length and default prefix reuse for final transcribe requests.
            """
            if op == 'transcribe':
                kwargs.setdefault('reuse_stream_prefix',True)
                kwargs['final_prefix_tokens']=getattr(self,'final_prefix_tokens',5)
            return await super().request(op, **kwargs)

        async def close(self):
            """Release the ASR worker and then its CPU language helper even when ASR cleanup raises."""
            try:
                await super().close()
            finally:
                await self._acoustic_language.close()

    return RealtimeV15ASR
