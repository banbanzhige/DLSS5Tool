# Execution policy shared by the base app and the frozen enhancement worker.
# ``raft_final`` remains accepted only so older saved settings fail safe to the
# original serial path. Production inference always uses torchvision's original
# RAFT forward implementation and all requested intermediate outputs (default 6).
PROFILES = ('serial', 'raft_final', 'raft_streams')


def execution_contract(settings, device=None):
    requested = settings.get('guidance_execution', 'serial')
    if requested not in PROFILES:
        raise ValueError('guidance.error.execution')
    mode = int(settings.get('guidance_mode', 0))
    has_flow = mode in (1, 3)
    dual = mode == 3 and requested == 'raft_streams'
    if dual and device == 'cpu':
        raise ValueError('guidance.error.streams_cuda')
    return {'execution': 'raft_streams' if dual else 'serial',
            'raft_output': 'all' if has_flow else 'off',
            'schedule': 'dual_stream' if dual else 'serial'}
