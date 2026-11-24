"""Worker-local loading controls for CRF's pinned Transformers runtime."""
import os
import warnings


def configure_loading():
    workers = int(os.environ.get('STREAMINGCOMMAND_LOADING_WORKERS', '4'))
    if not 1 <= workers <= 32:
        raise ValueError('Model loading workers must be from 1 to 32')
    # Transformers 5.23 uses this module constant, not the older HF_* env vars.
    # Keep the compatibility adjustment inside the owned model process.
    try:
        from transformers import core_model_loading
    except ImportError:
        core_model_loading = None
    if core_model_loading is not None and hasattr(core_model_loading, 'GLOBAL_WORKERS'):
        core_model_loading.GLOBAL_WORKERS = workers
    else:
        warnings.warn('Transformers loading API changed; num_workers was not applied', RuntimeWarning)
    return workers
