"""Discover a separately verified HDR pair without replacing S3 components."""
import importlib.metadata
import json
from pathlib import Path

from dlss5tool import paths
from dlss5tool.cuda_vsr_runtime import sha256

FILES = dict(hdr_vsr_library='vsr_cuda.dll', hdr_pack_ptx='vsr-hdr.ptx', p010_ptx='p010-rgba.ptx',
             device_host='enhancement.dll', device_worker='frame-worker.exe',
             analysis_ptx='analysis.ptx', analysis_tables='analysis-tables.npz')


def load_components(root=None):
    from dlss5tool.resident_sr import safe_path
    from dlss5tool.cuda_vsr_runtime import load_components as load_vsr
    from dlss5tool.gpu_export_runtime import load_components as load_encoder
    runtime = paths.runtime_root()
    root = Path(root) if root is not None else runtime/'resident-hdr'
    manifest = root/'manifest.json'
    if not manifest.is_file():
        return None
    data = json.loads(safe_path(manifest, exists=True).read_text(encoding='utf-8'))
    if data.get('schema') != 1 or not data.get('enabled'):
        return None
    if (data.get('formats') != ['hdr10_pq', 'hdr10_hlg'] or data.get('quality') != 4
            or data.get('geometry') != [[960, 540, 2], [1920, 1080, 2]] or not data.get('verified_devices')):
        raise RuntimeError('Resident HDR manifest contract mismatch')
    if importlib.metadata.version('av') != data.get('pyav'):
        return None
    import numpy as np
    import cv2
    if data.get('numpy') != np.__version__ or data.get('opencv') != cv2.__version__:
        return None
    from dlss5tool.analysis_quantization import cpu_fingerprint
    try:
        fingerprint = cpu_fingerprint()
    except (RuntimeError, ImportError, AttributeError):
        return None
    if data.get('analysis_cpu') != fingerprint:
        return None
    for name, relative in (('cuda_vsr_manifest', 'vsr-cuda/manifest.json'),
                           ('encoder_manifest', 'gpu-export/manifest.json')):
        if sha256(safe_path(runtime/relative, exists=True)) != data.get(name):
            return None
    vsr, encoder = load_vsr(), load_encoder()
    if vsr is None or encoder is None:
        return None
    component_hashes = {}
    for key, name in FILES.items():
        path = safe_path(root/name, exists=True)
        if sha256(path) != data.get('files', {}).get(name):
            raise RuntimeError('Resident HDR component hash mismatch: ' + name)
        encoder[key] = str(path)
        component_hashes[key] = data['files'][name]
    for key in ('native_library', 'sdr_ptx', 'hdr_ptx'):
        component_hashes[key] = sha256(safe_path(encoder[key], exists=True))
    decoder = safe_path(runtime/'resident-sr/nv12-rgba.ptx', exists=True)
    if sha256(decoder) != data.get('nv12_sha256'):
        return None
    encoder['nv12_ptx'] = str(decoder)
    component_hashes['nv12_ptx'] = data['nv12_sha256']
    return dict(encoder, cudart_directory=vsr['cudart_directory'], runtime_directory=vsr['runtime_directory'],
                pyav=data['pyav'], av_libraries=data['av_libraries'], verified_devices=data['verified_devices'],
                component_hashes=component_hashes, cuda_vsr_manifest=data['cuda_vsr_manifest'],
                analysis_cpu=data['analysis_cpu'])
