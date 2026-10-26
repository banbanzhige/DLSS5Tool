"""Discover a verified optional CUDA VSR bridge without loading CUDA in GUI."""
import hashlib
import json
from pathlib import Path

from dlss5tool import paths

FILES = {'native_library': 'vsr_cuda.dll', 'cudart': 'cudart64_12.dll'}


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def load_components(root=None, *, runtime_root=None):
    runtime = Path(runtime_root) if runtime_root is not None else paths.runtime_root()
    root = Path(root) if root is not None else runtime/'vsr-cuda'
    manifest = root/'manifest.json'
    if not manifest.is_file():
        return None
    for target in (runtime, root, manifest):
        for item in (target, *target.parents):
            if item.exists() and item.lstat().st_file_attributes & 0x400:
                raise RuntimeError('CUDA VSR component path contains a reparse point')
    record = json.loads(manifest.read_text(encoding='utf-8'))
    if record.get('schema') != 1 or not record.get('enabled'):
        return None
    if (record.get('abi') != 1 or record.get('quality') != 4 or
            record.get('formats') != ['rgba8-srgb'] or record.get('scales') != [2, 4] or
            record.get('max_output_pixels') != 3840*2160):
        raise RuntimeError('CUDA VSR manifest contract mismatch')
    result = {}
    for field, name in FILES.items():
        path = root/name
        if not path.is_file() or path.lstat().st_file_attributes & 0x400:
            raise RuntimeError('CUDA VSR component missing or redirected: '+name)
        if sha256(path) != record.get('files', {}).get(name):
            raise RuntimeError('CUDA VSR component hash mismatch: '+name)
        result[field] = str(path.resolve())
    provider = runtime/'nvngx_vsr.dll'
    if not provider.is_file() or provider.lstat().st_file_attributes & 0x400:
        raise RuntimeError('CUDA VSR provider missing or redirected')
    if sha256(provider) != record.get('provider_sha256'):
        raise RuntimeError('CUDA VSR provider differs from the verified pair')
    return dict(result, cudart_directory=str(root.resolve()), runtime_directory=str(runtime.resolve()))


def verified_geometry(width, height, scale):
    return (all(type(value) is int and value > 0 for value in (width, height, scale)) and
            scale in (2, 4) and width*height*scale*scale <= 3840*2160)
