"""Verified optional resident SDR route, discovered without loading GPU libraries."""
import importlib.metadata
import json
from pathlib import Path

from dlss5tool import paths
from dlss5tool.cuda_vsr_runtime import sha256

FILES = {'nv12_ptx':'nv12-rgba.ptx'}


def load_components(root=None):
    from dlss5tool.resident_sr import safe_path
    runtime = paths.runtime_root()
    root = Path(root) if root is not None else runtime/'resident-sr'
    manifest = root/'manifest.json'
    if not manifest.is_file():
        return None
    safe_path(manifest, exists=True)
    data = json.loads(manifest.read_text(encoding='utf-8'))
    if data.get('schema') != 1 or not data.get('enabled'):
        return None
    if (data.get('format') != 'nv12-sdr-cfr' or data.get('quality') != 4
            or data.get('max_output_pixels') != 3840*2160 or not data.get('verified_devices')):
        raise RuntimeError('Resident SR manifest contract mismatch')
    if importlib.metadata.version('av') != data.get('pyav'):
        return None
    for name, relative in (('cuda_vsr_manifest', 'vsr-cuda/manifest.json'),
                           ('encoder_manifest', 'gpu-export/manifest.json')):
        path = safe_path(runtime/relative, exists=True)
        if sha256(path) != data.get(name):
            return None
    from dlss5tool.cuda_vsr_runtime import load_components as load_vsr
    from dlss5tool.gpu_export_runtime import load_components as load_encoder
    if load_vsr() is None:
        return None
    result = load_encoder()
    if result is None:
        return None
    for field, filename in FILES.items():
        path = safe_path(root/filename, exists=True)
        if sha256(path) != data.get('files', {}).get(filename):
            raise RuntimeError('Resident SR component hash mismatch: '+filename)
        result[field] = str(path)
    encoder_record = json.loads((runtime/'gpu-export/manifest.json').read_text(encoding='utf-8'))
    return dict(result, pyav=data['pyav'], av_libraries=data['av_libraries'],
                verified_devices=data['verified_devices'], cuda_vsr_manifest=data['cuda_vsr_manifest'],
                component_hashes=dict(nv12_ptx=data['files']['nv12-rgba.ptx'],
                    native_library=encoder_record['files']['encoder.dll'], sdr_ptx=encoder_record['files']['sdr-yuv.ptx']))
