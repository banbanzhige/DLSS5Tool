"""Shared, Torch-free guidance parameter validation and compatibility contract."""
import math

NUMERIC = {
    'guidance_flow_edge': (720, 128, 1280, int),
    'guidance_depth_edge': (720, 128, 1280, int),
    'guidance_flow_updates': (6, 1, 32, int),
    'guidance_depth_smoothing': (0.9, 0, 0.99, float),
    'guidance_depth_low': (1.0, 0, 49, float),
    'guidance_depth_high': (99.0, 51, 100, float),
    'guidance_flow_range': (32.0, 1, 256, float),
}
DISPLAY_KEYS = ('guidance_flow_range', 'guidance_depth_palette', 'guidance_depth_invert')
ANALYSIS_KEYS = tuple(key for key in NUMERIC if key not in DISPLAY_KEYS)


def parameters(settings, *, strict=False):
    result = {}
    for key, (default, low, high, kind) in NUMERIC.items():
        fallback = settings.get('guidance_edge', default) if key.endswith('_edge') else default
        raw = settings.get(key, fallback)
        try:
            number = float(raw)
            if not math.isfinite(number) or (kind is int and number != int(number)):
                raise ValueError(key)
            if strict and not low <= number <= high:
                raise ValueError(key)
            result[key] = kind(max(low, min(high, number)))
        except (TypeError, ValueError, OverflowError):
            if strict:
                raise ValueError(key) from None
            result[key] = default
    palette = settings.get('guidance_depth_palette', 'gray')
    if strict and palette not in ('gray', 'turbo'):
        raise ValueError('guidance_depth_palette')
    result['guidance_depth_palette'] = palette if palette in ('gray', 'turbo') else 'gray'
    invert = settings.get('guidance_depth_invert', False)
    result['guidance_depth_invert'] = invert is True or invert == 1 or invert == 'true'
    return result


def analysis_parameters(settings):
    values = parameters(settings, strict=True)
    mode = int(settings.get('guidance_mode', 0))
    return {key: values[key] for key in ANALYSIS_KEYS
            if mode in ((1, 3) if key.startswith('guidance_flow_') else (2, 3))}


def check_parameter_handshake(settings, ready):
    expected = analysis_parameters(settings)
    actual = ready.get('analysis_parameters')
    if actual is not None:
        if actual != expected:
            raise ValueError('guidance.error.parameters_component')
    else:
        # Original workers can only honor the shared edge and fixed defaults.
        legacy = analysis_parameters({key: value for key, value in settings.items()
                                      if key not in ANALYSIS_KEYS})
        if expected != legacy:
            raise ValueError('guidance.error.parameters_component')


def analysis_edge(settings):
    values = parameters(settings)
    mode = int(settings.get('guidance_mode', 0))
    edges = ([values['guidance_flow_edge']] if mode in (1, 3) else [])
    edges += [values['guidance_depth_edge']] if mode in (2, 3) else []
    return max(edges, default=720)


def analysis_size(width, height, edge):
    scale = min(1.0, edge / max(width, height))
    return max(128, round(width * scale / 8) * 8), max(128, round(height * scale / 8) * 8)
