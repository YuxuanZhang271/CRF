"""Deployed380ms endpoint-commit profile for the V12 fusion75 Demo choice.

This is the same explicit configuration tested by the V4 real-time replay.
Use this class instead of the historical440ms experimental base default.
"""
from .endpoint_commit import YueEndpointCommitSession


class BoundedYueEndpointCommitSession(YueEndpointCommitSession):
    def __init__(self,*args,**kwargs):
        """Use the deployed 380 ms continuation bound unless the caller explicitly supplies a different
        one.
        """
        kwargs.setdefault('continuation_gap_ms',380)
        super().__init__(*args,**kwargs)
