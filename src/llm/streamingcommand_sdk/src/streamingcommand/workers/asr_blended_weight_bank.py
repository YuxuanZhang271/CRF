"""Experimental FP16 acoustic weight blends in one serialized ASR engine.

Original V15/base banks stay bit-identical. Additional banks are interpolation
experiments, explicitly not lossless copies. Nothing is written to checkpoints.
"""
import hashlib
import json
import os
import time

from asr_shared_weight_bank import WeightBankExtension, SharedEngineController


def digest(tensor):
    """Hash a tensor's contiguous raw bytes for a bitwise weight-bank roundtrip check."""
    return hashlib.sha256(tensor.detach().cpu().contiguous().view(__import__('torch').uint8).numpy().tobytes()).hexdigest()


class BlendedWeightBankExtension(WeightBankExtension):
    def prepare_asr_banks(self, primary_dir, alternate_dir, bank_mode='swap'):
        """Prepare exact source banks before building the optional blends in swap mode."""
        inventory = super().prepare_asr_banks(primary_dir, alternate_dir, bank_mode)
        if bank_mode != 'swap':
            raise ValueError('The interpolation experiment requires the one-inactive-bank swap implementation')
        extra = self.prepare_experimental_blends()
        return {**inventory, **extra}

    def prepare_experimental_blends(self):
        """Build FP32 interpolations stored at model precision and verify source-bank bytes and graph
        addresses.
        """
        import torch
        fractions = json.loads(os.environ.get('PHONO_ASR_BLEND_FRACTIONS', '[0.25,0.5,0.75,0.9]'))
        assert fractions and len(set(fractions)) == len(fractions)
        assert all(0 < value < 1 for value in fractions)
        params = self._asr_bank_parameters
        assert self._asr_bank_variant == 'v15'
        pointers = {name: parameter.data_ptr() for name, parameter in params.items()}
        v15_hashes = {name: digest(parameter) for name, parameter in params.items()}
        base_hashes = {name: digest(tensor) for name, tensor in self._asr_inactive_bank.items()}
        self._asr_blend_inactive = {'base': self._asr_inactive_bank}
        self._asr_blend_fractions = {}
        with torch.inference_mode():
            for fraction in fractions:
                name = f'mix{round(fraction * 100):02d}'
                self._asr_blend_fractions[name] = fraction
                bank = {}
                for key, parameter in params.items():
                    # Calculate in FP32, then store one FP16 inactive bank.
                    bank[key] = torch.lerp(self._asr_inactive_bank[key].float(), parameter.detach().float(), fraction).to(parameter.dtype)
                self._asr_blend_inactive[name] = bank
        self._asr_original_pointers = pointers
        self.select_asr_bank('base')
        self.select_asr_bank('v15')
        assert {name: digest(parameter) for name, parameter in params.items()} == v15_hashes
        assert {name: digest(tensor) for name, tensor in self._asr_blend_inactive['base'].items()} == base_hashes
        assert {name: parameter.data_ptr() for name, parameter in params.items()} == pointers
        return {'experimental_interpolation': True, 'interpolation_dtype': 'FP32_then_FP16',
                'lossless': False,
                'mix_v15_fractions': self._asr_blend_fractions,
                'bank_names': ['v15', 'base'] + list(self._asr_blend_fractions),
                'stored_inactive_banks': len(self._asr_blend_inactive),
                'original_banks_bit_identical_after_roundtrip': True,
                'original_bank_sha256': {'v15': v15_hashes, 'base': base_hashes},
                'mixed_banks_are_lossless_originals': False,
                'fixed_graph_parameter_addresses': True}

    def select_asr_bank(self, variant):
        """Swap the selected bank into the live parameter storage without changing graph addresses."""
        import torch
        if not hasattr(self, '_asr_blend_inactive'):
            return super().select_asr_bank(variant)
        if variant != self._asr_bank_variant and variant not in self._asr_blend_inactive:
            raise ValueError('Unknown experimental bank ' + variant)
        started = time.perf_counter()
        changed = variant != self._asr_bank_variant
        if changed:
            old = self._asr_bank_variant
            target = self._asr_blend_inactive.pop(variant)
            with torch.inference_mode():
                for name, parameter in self._asr_bank_parameters.items():
                    scratch = self._asr_swap_scratch[:parameter.numel()].view_as(parameter)
                    scratch.copy_(parameter.data)
                    parameter.data.copy_(target[name])
                    target[name].copy_(scratch)
            self._asr_blend_inactive[old] = target
            self._asr_bank_variant = variant
            if next(iter(self._asr_bank_parameters.values())).device.type == 'cuda':
                torch.cuda.synchronize()
            assert {name: parameter.data_ptr() for name, parameter in self._asr_bank_parameters.items()} == self._asr_original_pointers
        return {'variant': variant, 'changed': changed, 'weight_copy_ms': (time.perf_counter() - started) * 1000,
                'v15_fraction': self._asr_blend_fractions.get(variant)}


class BlendedEngineController(SharedEngineController):
    def select(self, variant):
        """Invalidate the engine caches before activating a different interpolated weight bank."""
        if variant not in self.inventory['bank_names']:
            raise ValueError('Unknown experimental ASR variant ' + variant)
        started = time.perf_counter()
        changed = variant != self.variant
        if changed:
            if not self.llm.reset_prefix_cache():
                raise RuntimeError('ASR KV invalidation failed')
            self.llm.reset_mm_cache()
            result = self.llm.collective_rpc('select_asr_bank', args=(variant,))[0]
            self.variant = variant
        else:
            result = {'variant': variant, 'changed': False, 'weight_copy_ms': 0.}
        return {**result, 'cache_invalidated': changed, 'switch_ms': (time.perf_counter() - started) * 1000}
