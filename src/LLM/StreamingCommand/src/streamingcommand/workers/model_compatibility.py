import json
import hashlib
from pathlib import Path

def verify_model_compatibility(primary_dir, alternate_dir):
    """Check computational config and frontend bytes before sharing weights in one native ASR model."""
    directories=[Path(primary_dir),Path(alternate_dir)]
    def normalized(value):
        """Discard known metadata-only fields recursively before comparing the two model configs."""
        if isinstance(value,dict):
            return {k:normalized(v) for k,v in value.items()
                    if k not in {'model_type','dtype','transformers_version'}}
        if isinstance(value,list):return [normalized(v) for v in value]
        return value
    configs=[json.loads((p/'config.json').read_text()) for p in directories]
    for c in configs:c['thinker_config'].setdefault('user_token_id',872)
    if normalized(configs[0])!=normalized(configs[1]):
        raise ValueError('Cannot share ASR models with different computational configurations')
    digests={}
    for name in ['preprocessor_config.json','tokenizer_config.json','vocab.json','merges.txt']:
        values=[(p/name).read_bytes() for p in directories]
        if values[0]!=values[1]:raise ValueError('Cannot share different ASR frontends: '+name)
        digests[name]=hashlib.sha256(values[0]).hexdigest()
    return {'computational_config_verified':True,'identical_frontend_sha256':digests,
            'metadata_differences_allowed':['model_type','dtype_forced_fp16','transformers_version'],
            'default_user_token_id':872}
