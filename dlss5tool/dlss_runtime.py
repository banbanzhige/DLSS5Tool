"""GPU-specific NR runtimes, verified downloads and disposable frame tests."""
from __future__ import annotations

from functools import lru_cache
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile

from dlss5tool import i18n


RUNTIME_NAME = 'nvngx_dlssnr.dll'
RELEASE_URL = 'https://github.com/banbanzhige/DLSS5Tool/releases/download/zip/'
# Asset hashes checked against the project's zip Release on 2026-11-10.
PROFILES = {
    'rtx30': {
        'asset': '30.-310.8.SF-v2.zip', 'size': 117898662,
        'archive_sha256': '01626f7ffe14c54928e9b2eaa09baf1886fa9200b247bbb51895f935f301886c',
        'sha256': '6eb209e764f39872625debd6abaf45e2bb6322f6f270f781f70c059ae30b3927',
    },
    'rtx40': {
        'asset': '40.zip', 'size': 106991529,
        'archive_sha256': '3fdeb4f3b44165bfd31d98e288a46dc16eedd40168819c8ed6f73c45fc92c7a1',
        'sha256': 'ceb6432f6fbdf44d886014bcd47241932bf8b67439feef9bbdd0961436662650',
    },
    'rtx50': {
        'asset': '50.zip', 'size': 109425424,
        'archive_sha256': 'e730e1ea95b0a4f6420b9b1bbb1c2948cbb1bc9241aebfdeb10efef8a3bc906a',
        'sha256': 'e16bcf15e16e13f527491cdf7845b2fe6521a738d8f7c9c721866a8496e1fc8e',
    },
}
PROFILE_SELECTIONS = {f'__{key}__': key for key in PROFILES}
MAX_DLL_BYTES = 256 * 1024 * 1024
_install_lock = threading.Lock()


def selection_label(value):
    if value == '':
        return i18n.tr('mods.auto')
    if value == '__bundled__':
        return i18n.tr('mods.bundled')
    profile = PROFILE_SELECTIONS.get(value)
    return i18n.tr('runtime.option.' + profile) if profile else value


def selection_value(label):
    for value in ('', '__bundled__', *PROFILE_SELECTIONS):
        if label == selection_label(value):
            return value
    return label


def selection_choices(settings=None):
    from dlss5tool import mod_paths
    return [selection_label(value) for value in ('', *PROFILE_SELECTIONS, '__bundled__')] + mod_paths.runtime_choices(settings)


class RuntimeErrorWithGuidance(RuntimeError):
    pass


def _error(settings, key, **values):
    return RuntimeErrorWithGuidance(i18n.tr_for((settings or {}).get('ui_language'), 'runtime.error.' + key, **values))


def gpu_profile(adapter):
    """Only known GeForce model ranges; do not mistake RTX 5000 Ada for 50xx."""
    if not adapter or adapter.get('vendor_id', 0x10DE) != 0x10DE:
        return ''
    match = re.search(r'\bRTX\s*(30\d{2}|40\d{2}|50\d{2})(?!\d)', str(adapter.get('name', '')), re.I)
    if not match or int(match[1][-2:]) < 50:
        return ''
    return 'rtx' + match[1][:2]


def selected_adapter(settings=None, adapters=None):
    if adapters is None:
        from dlss5tool.adapter_probe import cached_inventory
        adapters = (cached_inventory() or {}).get('adapters', [])
    selected = (settings or {}).get('render_gpu', 'auto')
    if selected == 'auto':
        return adapters[0] if adapters else {}
    return next((item for item in adapters if item.get('id') == selected), {})


def realtime_adapter(config, adapters):
    """Map the realtime picker's DXGI index to its advertised device name."""
    index = config.get('gpu_index', -1)
    records = [dict(item, vendor_id=item.get('vendor', 0)) for item in adapters
               if not item.get('software') and item.get('vendor') == 0x10DE]
    if index != -1:
        return next((item for item in records if item.get('index') == index), {})
    vendor, device = config.get('gpu_vendor', 0), config.get('gpu_device', 0)
    if vendor or device:
        return next((item for item in records if (not vendor or item['vendor_id'] == vendor)
                     and (not device or item.get('device') == device)), {})
    preferred = selected_adapter({})
    return preferred or (records[0] if records else {})


@lru_cache(maxsize=128)
def _file_hash(path, size, modified, created):
    with open(path, 'rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def file_hash(path):
    path = Path(path)
    info = path.stat()
    return _file_hash(str(path.resolve()), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def matches(path, profile):
    try:
        return Path(path).is_file() and file_hash(path) == PROFILES[profile]['sha256']
    except OSError:
        return False


def managed_path(settings, profile):
    from dlss5tool import mod_paths
    return mod_paths.mods_root(settings) / 'dlss' / (profile + '-' + PROFILES[profile]['sha256'][:12]) / RUNTIME_NAME


def _safe_path(path, root):
    """Reject links/junctions on every existing ancestor before any write."""
    path, root = Path(os.path.abspath(path)), Path(os.path.abspath(root))
    if path != root and root not in path.parents:
        raise ValueError('Runtime path escapes its module directory')
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError('Runtime path contains a link or junction: ' + str(item))
    if path.resolve() != path:
        raise ValueError('Runtime path resolves outside its expected location')
    return path


def install(settings, profile, *, opener=None, progress=None, cancel=None):
    """Install one pinned DLL without replacing bundled or user-provided files."""
    from dlss5tool import mod_paths
    spec = PROFILES[profile]
    if 'asset' not in spec:
        return mod_paths.bundled_runtime()
    root = mod_paths.mods_root(settings)
    with _install_lock:
        if cancel is not None and cancel.is_set():
            raise _error(settings, 'cancelled')
        target = _safe_path(managed_path(settings, profile), root)
        if matches(target, profile):
            return str(target)
        if target.exists():
            try:
                file_hash(target)
            except OSError as exc:
                raise _error(settings, 'unreadable', path=target) from exc
            raise _error(settings, 'modified', path=target)
        parent = _safe_path(target.parent, root)
        parent.mkdir(parents=True, exist_ok=True)
        _safe_path(parent, root)
        if shutil.disk_usage(parent).free < spec['size'] + MAX_DLL_BYTES + 128 * 1024 * 1024:
            raise _error(settings, 'space')
        stage = Path(tempfile.mkdtemp(prefix='.download-', dir=parent))
        archive, candidate = stage / 'runtime.zip', stage / RUNTIME_NAME
        try:
            request = urllib.request.Request(RELEASE_URL + urllib.parse.quote(spec['asset']),
                headers={'User-Agent': 'DLSS5Tool-Runtime/1', 'Accept': 'application/octet-stream'})
            digest = hashlib.sha256()
            received = 0
            deadline = time.monotonic() + 600
            with (opener or urllib.request.urlopen)(request, timeout=15) as response, archive.open('xb') as output:
                final = urllib.parse.urlsplit(response.geturl())
                if final.scheme != 'https' or final.hostname not in ('github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'):
                    raise _error(settings, 'download')
                while True:
                    if cancel is not None and cancel.is_set():
                        raise _error(settings, 'cancelled')
                    if time.monotonic() > deadline:
                        raise _error(settings, 'download')
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > spec['size']:
                        raise _error(settings, 'checksum')
                    digest.update(chunk)
                    output.write(chunk)
                    if progress:
                        progress(received, spec['size'])
            if received != spec['size'] or digest.hexdigest() != spec['archive_sha256']:
                raise _error(settings, 'checksum')
            with zipfile.ZipFile(archive) as package:
                files = [item for item in package.infolist()
                    if not item.is_dir() and item.filename.replace('\\', '/').rsplit('/', 1)[-1].casefold() == RUNTIME_NAME]
                if len(files) != 1 or not 0 < files[0].file_size <= MAX_DLL_BYTES:
                    raise _error(settings, 'archive')
                # Never extract archive paths; copy only the one expected member.
                with package.open(files[0]) as source, candidate.open('xb') as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
            if not matches(candidate, profile):
                raise _error(settings, 'checksum')
            if cancel is not None and cancel.is_set():
                raise _error(settings, 'cancelled')
            _safe_path(candidate, root)
            _safe_path(target, root)
            if target.exists():
                if matches(target, profile):
                    return str(target)
                raise _error(settings, 'modified', path=target)
            # Windows rename refuses to overwrite a concurrent installation.
            candidate.rename(target)
            return str(target)
        except (OSError, zipfile.BadZipFile) as exc:
            raise _error(settings, 'download_detail', error=str(exc)) from exc
        finally:
            # Only the two files created by this attempt are removed.
            for path in (candidate, archive):
                if _safe_path(path, root).is_file():
                    path.unlink()
            _safe_path(stage, root).rmdir()


def ensure_runtime(settings=None, *, adapter=None, progress=None, cancel=None):
    from dlss5tool import mod_paths
    info = mod_paths.runtime_info(settings, adapter=adapter)
    if info.get('needs_download'):
        install(settings, info['profile'], progress=progress, cancel=cancel)
        info = mod_paths.runtime_info(settings, adapter=adapter)
    if not Path(info['path']).is_file():
        raise _error(settings, 'missing', path=info['path'])
    return info


def test_runtime(settings, *, live_factory=None):
    """Initialize and render in ProcessLive; no input media or models required."""
    import numpy as np
    from dlss5tool.dlss_host_process import ProcessLive
    request = {**settings, 'guidance_mode': 0, 'frame_format': 'rgba8', 'color_profile': 'srgb',
               'intensity': 1.0, 'use_intensity': True, 'output_mix': 1.0, 'use_output_mix': True,
               'nr_quality': {}, 'nr_residual': {}}
    request.pop('_nr_bypass', None)
    live = (live_factory or ProcessLive)(256, 256, request)
    try:
        y, x = np.indices((256, 256))
        image = np.stack((x, y, (x + y) // 2, np.full_like(x, 255)), axis=-1).astype(np.uint8)
        output = live.process(image, reset=True)
        if output is None or output.shape != image.shape or output.dtype != np.uint8 or not np.isfinite(output).all():
            raise _error(settings, 'frame')
        return {'adapter': dict(live.adapter_info), 'path': live.runtime_path, 'backend': live.backend}
    finally:
        live.close()
