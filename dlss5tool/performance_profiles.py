"""Independent zero-guidance/guidance scheduling preferences, not render presets."""
from dlss5tool.host_queue import clamp_in_flight

KEYS = ('host_submission', 'host_in_flight', 'host_persistent_buffers', 'host_zero_fast_path')


def mode_key(mode):
    return 'guidance' if int(mode) else 'zero'


def profile(values, mode):
    source = values if isinstance(values, dict) else {}
    return {
        'host_submission': source.get('host_submission') if source.get('host_submission') in
                           ('merged', 'compatibility') else 'compatibility',
        'host_in_flight': clamp_in_flight(source.get('host_in_flight', 3), default=3),
        'host_persistent_buffers': source.get('host_persistent_buffers', True) is not False,
        'host_zero_fast_path': False if int(mode) else source.get('host_zero_fast_path', True) is not False,
    }


def profiles(values, current=None):
    values = values if isinstance(values, dict) else {}
    result = {key: profile(values.get(key), mode) for key, mode in (('zero', 0), ('guidance', 1))}
    if current is not None:
        key = mode_key(current.get('guidance_mode', 0))
        result[key] = profile(current, current.get('guidance_mode', 0))
    return result
