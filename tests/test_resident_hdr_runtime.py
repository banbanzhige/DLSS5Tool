"""S4 pairing checks precede native loading and preserve S3 independently."""
import json
from unittest import mock

import pytest

from dlss5tool import resident_hdr_runtime as runtime
from dlss5tool.analysis_quantization import cpu_fingerprint
from dlss5tool.cuda_vsr_runtime import sha256


def fixture(tmp_path, monkeypatch):
    root = tmp_path/'resident-hdr'
    root.mkdir()
    for name in ('vsr-cuda','gpu-export','resident-sr'):
        (tmp_path/name).mkdir()
    for name in ('vsr-cuda/manifest.json','gpu-export/manifest.json','resident-sr/nv12-rgba.ptx'):
        (tmp_path/name).write_bytes(b'paired')
    fingerprint = cpu_fingerprint()
    record = dict(schema=1,enabled=True,formats=['hdr10_pq','hdr10_hlg'],quality=4,
        geometry=[[960,540,2],[1920,1080,2]],verified_devices=[[4318,10115]],pyav='verified',
        numpy=fingerprint['numpy'],opencv=fingerprint['opencv'],analysis_cpu=fingerprint,
        cuda_vsr_manifest=sha256(tmp_path/'vsr-cuda/manifest.json'),
        encoder_manifest=sha256(tmp_path/'gpu-export/manifest.json'),
        nv12_sha256=sha256(tmp_path/'resident-sr/nv12-rgba.ptx'),av_libraries={},files={})
    for name in runtime.FILES.values():
        (root/name).write_bytes(name.encode())
        record['files'][name] = sha256(root/name)
    for name in ('encoder.dll','sdr.ptx','hdr.ptx'):
        (tmp_path/name).write_bytes(name.encode())
    monkeypatch.setattr(runtime.paths,'runtime_root',lambda:tmp_path)
    monkeypatch.setattr(runtime.importlib.metadata,'version',lambda _:'verified')
    monkeypatch.setattr('dlss5tool.cuda_vsr_runtime.load_components',lambda:dict(cudart_directory='paired',runtime_directory='paired'))
    monkeypatch.setattr('dlss5tool.gpu_export_runtime.load_components',lambda:dict(
        native_library=str(tmp_path/'encoder.dll'),sdr_ptx=str(tmp_path/'sdr.ptx'),hdr_ptx=str(tmp_path/'hdr.ptx')))
    (root/'manifest.json').write_text(json.dumps(record))
    return root, record


def test_discovery_verifies_files_without_loading_cuda(tmp_path,monkeypatch):
    root,_ = fixture(tmp_path,monkeypatch)
    with mock.patch('ctypes.WinDLL') as native:
        result = runtime.load_components(root)
    native.assert_not_called()
    assert result['hdr_vsr_library'] == str(root/'vsr_cuda.dll')


@pytest.mark.parametrize('dependency',['vsr-cuda/manifest.json','gpu-export/manifest.json','resident-sr/nv12-rgba.ptx'])
def test_dependency_change_disables_only_s4(tmp_path,monkeypatch,dependency):
    root,_ = fixture(tmp_path,monkeypatch)
    (tmp_path/dependency).write_bytes(b'changed')
    assert runtime.load_components(root) is None


def test_cpu_dispatch_mismatch_keeps_existing_route(tmp_path,monkeypatch):
    root,record = fixture(tmp_path,monkeypatch)
    record['analysis_cpu']['numpy_cpu_features']['AVX2'] = not record['analysis_cpu']['numpy_cpu_features']['AVX2']
    (root/'manifest.json').write_text(json.dumps(record))
    assert runtime.load_components(root) is None


def test_changed_native_component_is_rejected_before_loading(tmp_path,monkeypatch):
    root,_ = fixture(tmp_path,monkeypatch)
    (root/'frame-worker.exe').write_bytes(b'changed')
    with pytest.raises(RuntimeError,match='hash mismatch'):
        runtime.load_components(root)
