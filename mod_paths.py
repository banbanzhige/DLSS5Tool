"""User-owned, external modules beside the executable (never in _internal)."""
from pathlib import Path
import json
import sys
import i18n


def app_root():
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def mods_root(settings=None):
    value = str((settings or {}).get('mods_directory', '')).strip() or 'mods'
    path = Path(value)
    return path if path.is_absolute() else app_root() / path


def module_path(settings, key, default):
    value = str(settings.get(key, '')).strip() or default
    if key == 'guidance_depth_weights':
        value = value.replace('{encoder}', settings.get('guidance_depth_encoder', 'vitl'))
    path = Path(value)
    return path if path.is_absolute() else mods_root(settings) / path


def search_roots(settings=None):
    """Bounded, deterministic search; no system/PATH scan or code execution."""
    roots = []
    for path in (mods_root(settings), app_root() / 'mods'):
        if str(path.resolve()).casefold() not in {str(p.resolve()).casefold() for p in roots}:
            roots.append(path)
    return roots


def bundled_runtime():
    return str(Path(getattr(sys, '_MEIPASS', app_root())) / 'nvngx_dlssnr.dll')


def runtime_info(settings=None):
    settings = settings or {}
    selected = str(settings.get('dlss_runtime', '')).strip()
    if selected == '__bundled__':
        return {'path': bundled_runtime(), 'source': 'bundled', 'fallback': False, 'ambiguous': False}
    if selected:
        path = module_path(settings, 'dlss_runtime', selected)
        if path.is_file():
            return {'path': str(path.resolve()), 'source': 'selected', 'fallback': False, 'ambiguous': False}
    roots = search_roots(settings)
    for root in roots:
        path = root / 'nvngx_dlssnr.dll'
        if path.is_file():
            return {'path': str(path.resolve()), 'source': 'detected', 'fallback': bool(selected), 'ambiguous': False}
    candidates = set()
    for root in roots:
        for path in list(root.glob('nvngx_dlssnr*.dll')) + list((root / 'dlss').glob('*/nvngx_dlssnr.dll')):
            if path.is_file():
                candidates.add(str(path.resolve()))
    if len(candidates) == 1:
        return {'path': candidates.pop(), 'source': 'detected', 'fallback': bool(selected), 'ambiguous': False}
    return {'path': bundled_runtime(), 'source': 'bundled', 'fallback': bool(selected), 'ambiguous': len(candidates) > 1}


def runtime_path(settings=None):
    return runtime_info(settings)['path']


def runtime_choices(settings=None):
    root = mods_root(settings)
    candidates = list(root.glob("nvngx_dlssnr*.dll")) + list((root / "dlss").glob("*/nvngx_dlssnr.dll"))
    return sorted(str(path.relative_to(root)) for path in candidates if path.is_file())


GUIDANCE_PROTOCOL = 1


def enhancement_path(settings=None):
    for root in search_roots(settings):
        path = root / 'enhancement'
        if path.exists():
            return path
    return mods_root(settings) / 'enhancement'


def enhancement_info(settings=None):
    """Inspect only the fixed component contract; never execute discovery results."""
    root = enhancement_path(settings)
    worker = root / 'guidance_worker.exe'
    language = (settings or {}).get('ui_language')
    if not root.exists():
        raise FileNotFoundError(i18n.tr_for(language, 'guidance.error.component_missing'))
    try:
        manifest_path = root / 'enhancement.json'
        if manifest_path.stat().st_size > 65536:
            raise ValueError('manifest too large')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if (manifest.get('id') != 'dlss5-guidance' or
                type(manifest.get('protocol')) is not int or
                manifest['protocol'] != GUIDANCE_PROTOCOL or
                manifest.get('architectures') != ['raft_large', 'depth_anything_v2']):
            raise ValueError('incompatible contract')
        if not worker.is_file() or not any(path.is_file() for path in (root / '_internal').glob('python3[0-9]*.dll')):
            raise ValueError('incomplete component')
    except (OSError, ValueError, AttributeError) as exc:
        raise ValueError(i18n.tr_for(language, 'guidance.error.component_invalid')) from exc
    return {'worker': str(worker.resolve())}


def find_module(settings, key, relatives, directory=False):
    expected = module_path(settings, key, relatives[0])
    def exists(path):
        return (path / 'dpt.py').is_file() and (path / '__init__.py').is_file() if directory else path.is_file()
    if exists(expected):
        return expected
    for root in search_roots(settings):
        for relative in relatives:
            path = root / relative
            if exists(path):
                return path
    return expected  # caller reports precisely what is missing


def component_build(settings=None):
    """Build capability only; not proof of the device used in a live session."""
    manifest = enhancement_path(settings) / 'enhancement.json'
    try:
        if manifest.stat().st_size <= 65536:
            value = json.loads(manifest.read_text(encoding='utf-8')).get('build')
            if value in ('cuda', 'cpu'):
                return value
    except (OSError, ValueError, AttributeError):
        pass
    return 'unknown'


def depth_encoder(settings):
    """Resolve known checkpoint names only; no torch import or checkpoint execution."""
    selected = settings.get('guidance_depth_encoder', 'auto')
    if selected != 'auto':
        if selected not in {'vits', 'vitb', 'vitl'}:
            raise ValueError(i18n.tr_for(settings.get('ui_language'), 'guidance.error.encoder'))
        return selected
    preferred = 'vitl'  # compatible with existing components without a default
    manifest = enhancement_path(settings) / 'enhancement.json'
    try:
        if manifest.stat().st_size <= 65536:
            value = json.loads(manifest.read_text(encoding='utf-8')).get('default_depth_encoder')
            if value in ('vits', 'vitb', 'vitl'):
                preferred = value
    except (OSError, ValueError, AttributeError):
        pass  # component validation reports installation errors separately
    encoders = [preferred] + [name for name in ('vits', 'vitb', 'vitl') if name != preferred]
    custom = str(settings.get('guidance_depth_weights', '')).strip()
    if custom:
        for encoder in encoders:
            path = module_path({**settings, 'guidance_depth_encoder': encoder}, 'guidance_depth_weights', '')
            if path.is_file():
                # An arbitrary renamed checkpoint uses the component default;
                # advanced users can explicitly choose a different architecture.
                return next((name for name in encoders if path.stem == f'depth_anything_v2_{name}'), encoder)
    for root in search_roots(settings):
        for relative in ('models', 'enhancement/models', 'models/checkpoints', '.'):
            for encoder in encoders:
                if (root / relative / f'depth_anything_v2_{encoder}.pth').is_file():
                    return encoder
    return preferred


def guidance_candidates(settings):
    """Read-only discovery, including missing paths, for compact UI statuses."""
    mode = int(settings.get("guidance_mode", 0))
    if not mode:
        return {}
    result = {"worker": str(enhancement_path(settings) / 'guidance_worker.exe')}
    if mode in (1, 3):
        name = 'raft_large_C_T_SKHT_V2-ff5fadd5.pth'
        result["flow_weights"] = str(find_module(settings, 'guidance_flow_weights',
            [f'models/{name}', f'enhancement/models/{name}', f'torch_home/hub/checkpoints/{name}', f'models/checkpoints/{name}', name]))
    if mode in (2, 3):
        encoder = depth_encoder(settings)
        name = f'depth_anything_v2_{encoder}.pth'
        result["depth_weights"] = str(find_module({**settings, 'guidance_depth_encoder': encoder}, 'guidance_depth_weights',
            [f'models/{name}', f'enhancement/models/{name}', f'models/checkpoints/{name}', name]))
    return result


def guidance_files(settings):
    if not int(settings.get('guidance_mode', 0)):
        return {}
    component = enhancement_info(settings)
    result = guidance_candidates(settings)
    result.update(component)
    missing = [path for path in result.values() if not Path(path).is_file()]
    if missing:
        raise FileNotFoundError(i18n.tr_for(settings.get('ui_language'), 'guidance.error.files_missing', paths='\n'.join(missing)))
    return result
