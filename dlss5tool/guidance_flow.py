"""Torch-free flow backend selection and acknowledgement contract."""
BACKENDS = ('raft', 'nvofa')
GRIDS = (1, 2, 4)


def flow_backend(settings, *, strict=False):
    backend = settings.get('guidance_flow_backend', 'raft')
    if backend not in BACKENDS:
        raise ValueError('guidance.error.flow_backend')
    if strict and int(settings.get('guidance_mode', 0)) in (1, 3) and backend == 'nvofa':
        if settings.get('guidance_device') == 'cpu':
            raise ValueError('guidance.error.nvofa_cuda')
        if int(settings['guidance_mode']) == 3:
            raise ValueError('guidance.error.nvofa_mixed')
    return backend


def flow_grid(settings):
    if flow_backend(settings) != 'nvofa':
        return None
    try:
        grid = int(settings.get('guidance_flow_grid', 4))
    except (TypeError, ValueError):
        raise ValueError('guidance.error.flow_grid') from None
    if grid not in GRIDS:
        raise ValueError('guidance.error.flow_grid')
    return grid


def flow_contract(settings):
    backend = flow_backend(settings)
    hardware = backend == 'nvofa' and int(settings.get('guidance_mode', 0)) in (1, 3)
    return {'flow_backend': backend, 'flow_grid': flow_grid(settings) if hardware else None,
            'flow_quality': 'slow' if hardware else None, 'flow_temporal_hints': False}


def check_flow_handshake(settings, ready):
    expected = flow_contract(settings)
    if int(settings.get('guidance_mode', 0)) not in (1, 3):
        return
    actual_grid = ready.get('flow_grid')
    if (expected['flow_backend'] == 'nvofa' and expected['flow_grid'] is not None
            and actual_grid in GRIDS and actual_grid != expected['flow_grid']):
        raise ValueError('guidance.error.flow_grid_component')
    for key, value in expected.items():
        if ready.get(key, value if expected['flow_backend'] == 'raft' else None) != value:
            raise ValueError('guidance.error.flow_backend_component')
