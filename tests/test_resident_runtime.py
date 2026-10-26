import json
from pathlib import Path
from types import SimpleNamespace
import threading
from unittest import mock

import pytest

from dlss5tool import resident_sr_runtime as runtime
from dlss5tool import resident_sr as sr
from dlss5tool.cuda_vsr_runtime import sha256


def fixture(tmp_path,monkeypatch):
    root=tmp_path/'resident-sr';root.mkdir()
    for name in ('vsr-cuda','gpu-export'):(tmp_path/name).mkdir()
    (tmp_path/'vsr-cuda/manifest.json').write_text('{}')
    (tmp_path/'gpu-export/manifest.json').write_text(json.dumps(dict(files={'encoder.dll':'native','sdr-yuv.ptx':'sdr'})))
    (root/'nv12-rgba.ptx').write_bytes(b'verified-PTX')
    record=dict(schema=1,enabled=True,format='nv12-sdr-cfr',quality=4,max_output_pixels=3840*2160,
        verified_devices=[[4318,10115]],pyav='18.1.0',av_libraries={'libswscale':[9,5,102]},
        cuda_vsr_manifest=sha256(tmp_path/'vsr-cuda/manifest.json'),
        encoder_manifest=sha256(tmp_path/'gpu-export/manifest.json'),
        files={'nv12-rgba.ptx':sha256(root/'nv12-rgba.ptx')})
    (root/'manifest.json').write_text(json.dumps(record))
    monkeypatch.setattr(runtime.paths,'runtime_root',lambda:tmp_path)
    monkeypatch.setattr(runtime.importlib.metadata,'version',lambda _:'18.1.0')
    monkeypatch.setattr('dlss5tool.cuda_vsr_runtime.load_components',lambda:{'verified':True})
    monkeypatch.setattr('dlss5tool.gpu_export_runtime.load_components',lambda:{'native_library':'native','sdr_ptx':'sdr'})
    return root,record


def test_runtime_discovery_loads_no_native_gpu_library(tmp_path,monkeypatch):
    root,_=fixture(tmp_path,monkeypatch)
    with mock.patch('ctypes.WinDLL') as load:
        result=runtime.load_components(root)
    load.assert_not_called()
    assert result['nv12_ptx']==str(root/'nv12-rgba.ptx')


@pytest.mark.parametrize('dependency',['vsr-cuda/manifest.json','gpu-export/manifest.json'])
def test_upgrading_bound_dependency_disables_route_until_reverified(tmp_path,monkeypatch,dependency):
    root,_=fixture(tmp_path,monkeypatch)
    (tmp_path/dependency).write_text('{"changed":true}')
    assert runtime.load_components(root) is None


def test_changed_conversion_binary_is_rejected(tmp_path,monkeypatch):
    root,_=fixture(tmp_path,monkeypatch)
    (root/'nv12-rgba.ptx').write_bytes(b'changed')
    with pytest.raises(RuntimeError,match='hash mismatch'):runtime.load_components(root)


def test_fully_cached_spatial_frames_keep_existing_consumer(tmp_path,monkeypatch):
    from dlss5tool.render_cache import RenderCache
    source=tmp_path/'source.mp4';source.touch()
    cache=RenderCache(1024)
    monkeypatch.setattr('dlss5tool.render_cache.render_identity',lambda *_:'same')
    cache.spatial=SimpleNamespace(key='same',closed=False,error=None,
        condition=threading.Condition(),metadata={'source_frames':2},cache={0:'a',1:'b'})
    assert cache.has_complete_spatial(source,{'super_resolution_scale':2})
    del cache.spatial.cache[0]
    assert not cache.has_complete_spatial(source,{'super_resolution_scale':2})
