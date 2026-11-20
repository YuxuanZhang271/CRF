"""Lightweight profile identities shared by the server and launchers."""
DEFAULT_ASR_PROFILE = 'v15-canto-commit'
ASR_PROFILE_VERSIONS = {
    'v15-canto-commit': 'v12-fusion75-endpoint380-20261115',
    'v17-yue-precision': 'fusion75-classifier20-endpoint380-20261117',
    'v15': 'v15-supported-margins-v3',
    'v15-realtime': 'v15-acoustic-lid-prefix-20261021r4',
    'v15-continuity': 'v15-short-acoustic-continuity-20261022r7',
    'v15-temporal': 'v15-temporal-acoustic-consensus-20261022r8',
    'v15-temporal-stream': 'v15-parallel-language-stream-20261023r15',
    'v15-speech-aware': 'v15-shared-asr-20261023r17',
    'v15-speech-aware-dual': 'v15-acoustic-speech-evidence-20261023r16',
    'legacy': 'asr-repair-v1-hint800',
}
ASR_PROFILES = tuple(ASR_PROFILE_VERSIONS)
