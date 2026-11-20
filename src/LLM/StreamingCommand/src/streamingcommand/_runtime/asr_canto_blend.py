"""Acoustic-language-selected interpolation diagnostics, no extra ASR pass."""
from .asr_codeswitch_calibrated import policy as make_policy
from .asr_codeswitch_banks import configure as configure_metadata

PROFILES = ('control', 'mix25', 'mix50', 'mix75', 'mix90', 'original_calibrated')


def policy(profile, **kwargs):
    """Select the requested weight blend only when causal Cantonese evidence meets the diagnostic
    threshold.
    """
    if profile not in PROFILES:
        raise ValueError(profile)
    if profile == 'control':
        return None
    selected = make_policy('calibrated_classifier', **kwargs)
    if selected is None:
        return None
    hint = kwargs['acoustic_hint'] or {}
    current = ((hint.get('temporal_language_queries') or {}).get('current')) or hint
    mix = (profile.startswith('mix') and current.get('native_language_tag') == 'yue'
           and current.get('language_probability', 0) >= .9)
    if mix:
        selected['model_variant'] = profile
    selected['audit'].update(profile=profile, model_variant=profile if mix else 'retained_R17_route',
                             calibrated_weights={'rotate': 6., 'track': 8.},
                             fixed_priors={'default': 4., 'rotate': 6., 'track': 8.},
                             original_checkpoint_files_modified=False)
    return selected


def configure(asr, profile):
    """Require a single shared engine and install the diagnostic interpolation policy."""
    configure_metadata(asr, 'control')
    if profile not in PROFILES:
        raise ValueError(profile)
    if not getattr(asr, '_blend_metadata_installed', False):
        original = asr._continuity.original_request

        async def request(op, **kwargs):
            """Report the actual interpolated bank on transcribe responses from the shared worker."""
            result = await original(op, **kwargs)
            variant = (result.get('shared_engine') or {}).get('variant', '')
            if op == 'transcribe' and variant.startswith('mix'):
                result['selected_model_dir'] = 'runtime_FP16_interpolation_' + variant
                result['experimental_interpolated_bank'] = variant
            return result

        asr._continuity.original_request = request
        asr._blend_metadata_installed = True
    if profile != 'control':
        asr.final_request_policy = lambda **kwargs: policy(profile, **kwargs)
    asr.status.update(experimental_codeswitch_profile=profile,
                      experimental_interpolation=True, original_checkpoint_files_modified=False)
    return asr
