"""Language-conditioned final ASR; no transcript rewriting or command decisions."""
from __future__ import annotations

try:
    from .asr_backend import encode_pcm16
except ImportError:
    from asr_backend import encode_pcm16


async def decode_final(asr, segment_id, pcm, *, on_probe=None, should_retry=None,
                       streaming_language_hint=None, prefetched_acoustic_hint=None):
    """Reuse a native language hint, or auto-decode then rescore eligible audio.

    This fixed policy also applies to chat. Both passes see identical complete
    audio and context. Publish the first raw response before rescoring so a
    failed second request cannot erase it. No keywords or reference select it.
    """
    configured_language = getattr(asr, "language", "English")
    initial_language = configured_language
    hint = streaming_language_hint or {}
    hint_language = hint.get("native_language")
    hint_outside_domain = (getattr(asr, "retry_out_of_domain_language", False)
                           and hint_language not in (None, "", "None", "English", "Cantonese", "Chinese"))
    language_source = "configured" if configured_language is not None else "fresh_auto"
    if (configured_language is None and getattr(asr, "reuse_streaming_language", False)
            and hint.get("segment_id") == segment_id
            and (hint_language == "Cantonese" or hint_outside_domain)
            and hint.get("text_nonempty")
            and hint.get("audio_processed_samples",0) >= getattr(asr,"minimum_streaming_hint_samples",0)
            and 0 < hint.get("audio_processed_samples", 0) <= len(pcm) // 2):
        if (getattr(asr, 'preserve_native_prefix_for_final', False)
                and hint.get('reusable_prefix_tokens', 0) > 3):
            # Preserve a native body prefix rather than discard it merely to
            # change the language condition and regenerate the entire utterance.
            language_source = 'same_segment_native_prefix_preserved'
        else:
            initial_language = getattr(asr, "cantonese_final_language", None)
        if initial_language is not None:
            language_source = "same_segment_native_streaming_language"
    acoustic_hint = prefetched_acoustic_hint
    classifier = getattr(asr, 'acoustic_language_for_final', None)
    if configured_language is None and (classifier is not None or acoustic_hint is not None):
        if acoustic_hint is None:
            acoustic_hint = await classifier(pcm)
        if acoustic_hint.get('requested_language') is not None:
            initial_language = acoustic_hint['requested_language']
            language_source = ('same_speech_prefetched_acoustic_language_hint'
                               if acoustic_hint.get('prefetched') else 'full_audio_acoustic_language_hint')
            if (getattr(asr,'preserve_acoustic_native_prefix',False)
                    and hint.get('segment_id')==segment_id
                    and hint.get('native_language')==initial_language
                    and hint.get('reusable_prefix_tokens',0)>3):
                initial_language = None
                language_source = 'same_speech_acoustic_native_prefix_agreement'
    context = getattr(asr, "context", "")
    conditioner = getattr(asr, 'final_request_policy', None)
    request_policy = conditioner(pcm=pcm, acoustic_hint=acoustic_hint,
                                language=initial_language, context=context) if conditioner else None
    if request_policy is not None:
        # Opt-in experiments decide before the sole ASR generation; the default
        # has no conditioner. Retain actual language and policy in final logs.
        initial_language = request_policy['language']
        context = request_policy['context']
        language_source = 'acoustic_codeswitch_request_policy'
    audio = encode_pcm16(pcm)
    kwargs = dict(segment_id=segment_id, audio_float32_b64=audio,
                  context=context)
    if request_policy is not None:
        kwargs.update({key: request_policy[key] for key in ('model_variant', 'reuse_stream_prefix')
                       if key in request_policy})
    if (language_source == "same_segment_native_streaming_language" and hint_outside_domain
            and getattr(asr, "out_of_domain_fallback", None) == 'whisper'
            and hint_language in getattr(asr,"out_of_domain_whisper_languages",())):
        kwargs['decoder_engine'] = 'whisper'
    result = await asr.request("transcribe", language=initial_language, **kwargs)
    samples = len(pcm) // 2

    def check_coverage(response):
        """Compare the worker's processed sample count against the requested audio range."""
        if int(response.get("audio_processed_samples", samples)) != samples:
            raise AssertionError("Fresh final did not cover the complete received segment")

    check_coverage(result)
    detected = result.get("detected_language")
    retry_language = getattr(asr, "cantonese_final_language", None)
    outside_domain = (getattr(asr, "retry_out_of_domain_language", False)
                      and detected not in (None, "", "None", "English", "Cantonese", "Chinese"))
    passes = [{"requested_language": initial_language, "response": result}]
    selected_language, probe = initial_language, None
    can_retry = should_retry or (lambda: True)
    if (initial_language is None and retry_language is not None
            and getattr(asr, 'allow_language_rescore', True) and can_retry()
            and (detected == "Cantonese" or outside_domain) and result.get("text")):
        probe = result
        if on_probe is not None:
            await on_probe(probe)
        if can_retry():
            if (outside_domain and getattr(asr, "out_of_domain_fallback", None) == 'whisper'
                    and detected in getattr(asr,"out_of_domain_whisper_languages",())):
                kwargs['decoder_engine'] = 'whisper'
            result = await asr.request("transcribe", language=retry_language, **kwargs)
            check_coverage(result)
            selected_language = retry_language
            passes.append({"requested_language": retry_language, "response": result})
    generations = []
    for index, attempt in enumerate(passes):
        generations.extend({**generation, "source_op": "transcribe", "source_pass": index,
                            "independent_audio_decode": not attempt['response'].get('final_prefix_reused', False),
                            "text_prefix_reuse_tokens": generation.get('prefix_tokens_used', 0)
                            if attempt['response'].get('final_prefix_reused', False) else 0}
                           for generation in attempt["response"].get("generations", []))
    if request_policy is not None:
        result['acoustic_codeswitch_policy'] = request_policy['audit']
    return {"result": result, "language_probe": probe,
            "acoustic_language_hint": acoustic_hint,
            "configured_language": configured_language, "language_policy_source": language_source,
            "streaming_language_hint": dict(hint) if language_source.startswith("same_segment_native_")
                or language_source=='same_speech_acoustic_native_prefix_agreement' else None,
            "initial_language": initial_language, "initial_detected_language": detected,
            "requested_language": selected_language, "decoding_passes": passes,
            "generations": generations, "request_ops": ["transcribe"] * len(passes)}
