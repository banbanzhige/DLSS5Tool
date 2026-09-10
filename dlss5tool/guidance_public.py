"""Public guidance policy. No model, GPU or GUI imports.

Depth remains available to maintainers; restart the GUI after changing the env.
Callers retain their original settings (especially persisted queue snapshots).
"""
import os


def depth_enabled():
    return os.environ.get('DLSS5TOOL_ENABLE_DEPTH') == '1'


def public_modes():
    return (0, 1, 2, 3) if depth_enabled() else (0, 1)


def public_targets():
    return ('depth', 'flow') if depth_enabled() else ('flow',)


def public_mode(mode):
    mode = int(mode)
    if mode not in (0, 1, 2, 3):
        raise ValueError('Invalid guidance mode')
    return mode if depth_enabled() else {2: 0, 3: 1}.get(mode, mode)


def normalize_public_settings(settings):
    result = dict(settings or {})
    result['guidance_mode'] = public_mode(result.get('guidance_mode', 0))
    if not depth_enabled():
        for key in ('guidance_preview_view', 'guidance_compare_target'):
            if result.get(key) == 'depth':
                result[key] = 'flow'
    return result
