"""Runtime acquisition and GPU selection without loading real DLLs."""
import hashlib
import io
from pathlib import Path
from unittest import mock
import zipfile

import numpy as np
import pytest

from dlss5tool import app_settings, dlss_runtime as runtime, mod_paths


@pytest.fixture
def layout(tmp_path, monkeypatch):
    monkeypatch.setattr(mod_paths, 'app_root', lambda: tmp_path)
    monkeypatch.setattr(mod_paths.paths, 'runtime_root', lambda: tmp_path / '_internal')
    monkeypatch.setattr(runtime, 'selected_adapter', lambda settings=None, adapters=None: {})
    bundled = tmp_path / '_internal' / runtime.RUNTIME_NAME
    bundled.parent.mkdir()
    bundled.write_bytes(b'bundled')
    monkeypatch.setitem(runtime.PROFILES['rtx40'], 'sha256', hashlib.sha256(b'bundled').hexdigest())
    return tmp_path


def adapter(name, id='primary'):
    return {'name': name, 'id': id, 'vendor_id': 0x10DE}


@pytest.mark.parametrize('name,profile', [
    ('NVIDIA GeForce RTX 3060 Ti', 'rtx30'), ('GeForce RTX 3090', 'rtx30'),
    ('NVIDIA GeForce RTX 4090 D', 'rtx40'), ('RTX 4060 Laptop GPU', 'rtx40'),
    ('NVIDIA GeForce RTX 5080', 'rtx50'), ('RTX 5090 Laptop GPU', 'rtx50'),
    ('NVIDIA RTX 5000 Ada Generation', ''), ('RTX A4000', ''), ('RTX 2080', ''),
    ('RTX 40900', ''), ('Radeon RX 7900 XTX', ''),
])
def test_geforce_generation_is_not_professional_model_number(name, profile):
    assert runtime.gpu_profile(adapter(name)) == profile


def test_uses_selected_physical_gpu_and_does_not_guess_missing_id():
    records = [adapter('RTX 4090', '40'), adapter('RTX 5090', '50')]
    assert runtime.selected_adapter({}, records)['id'] == '40'
    assert runtime.selected_adapter({'render_gpu': '50'}, records)['id'] == '50'
    assert runtime.selected_adapter({'render_gpu': 'removed'}, records) == {}


def test_generation_choice_overrides_wrong_loose_dll_and_keeps_explicit_path(layout):
    mods = layout / 'mods'
    mods.mkdir()
    wrong = mods / runtime.RUNTIME_NAME
    wrong.write_bytes(b'wrong generation')
    chosen = adapter('RTX 5090')
    automatic = mod_paths.runtime_info({}, adapter=chosen)
    assert automatic['profile'] == 'rtx50' and automatic['needs_download']
    assert automatic['path'] != str(wrong)
    assert mod_paths.runtime_info({'dlss_runtime': str(wrong)}, adapter=chosen)['path'] == str(wrong)
    assert mod_paths.runtime_info({'dlss_runtime': '__bundled__'}, adapter=chosen)['source'] == 'bundled'
    assert mod_paths.runtime_info({}, adapter=adapter('RTX 4090'))['path'] == mod_paths.bundled_runtime()
    assert mod_paths.runtime_info({'dlss_runtime': '__rtx30__'}, adapter=chosen)['profile'] == 'rtx30'


def test_known_legacy_dll_is_reused_without_download(layout, monkeypatch):
    mods = layout / 'mods'
    mods.mkdir()
    dll = mods / runtime.RUNTIME_NAME
    dll.write_bytes(b'known')
    monkeypatch.setitem(runtime.PROFILES['rtx30'], 'sha256', hashlib.sha256(b'known').hexdigest())
    info = mod_paths.runtime_info({}, adapter=adapter('RTX 3060'))
    assert info['path'] == str(dll) and not info['needs_download']


def test_modified_bundled_runtime_is_preserved_and_auto_fetches_correct_40_series(layout):
    bundled = layout / '_internal' / runtime.RUNTIME_NAME
    bundled.write_bytes(b'user modified')
    info = mod_paths.runtime_info({}, adapter=adapter('RTX 4070'))
    assert info['profile'] == 'rtx40' and info['needs_download']
    assert info['path'] != str(bundled)
    assert mod_paths.runtime_info({'dlss_runtime': '__bundled__'})['path'] == str(bundled)
    assert bundled.read_bytes() == b'user modified'


def make_archive(contents=b'fixture-dll', names=('nested/nvngx_dlssnr.dll',)):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as package:
        for name in names:
            package.writestr(name, contents)
        package.writestr('../../outside.txt', b'must not be extracted')
    return output.getvalue()


class Response(io.BytesIO):
    def geturl(self):
        return 'https://release-assets.githubusercontent.com/fixture'


@pytest.fixture
def download(layout, monkeypatch):
    data = make_archive()
    spec = dict(runtime.PROFILES['rtx30'], size=len(data),
                archive_sha256=hashlib.sha256(data).hexdigest(), sha256=hashlib.sha256(b'fixture-dll').hexdigest())
    monkeypatch.setitem(runtime.PROFILES, 'rtx30', spec)
    return data


def test_download_verifies_both_hashes_extracts_only_dll_and_reuses_file(layout, download):
    opener = mock.Mock(side_effect=lambda *a, **k: Response(download))
    progress = mock.Mock()
    target = Path(runtime.install({}, 'rtx30', opener=opener, progress=progress))
    assert target.read_bytes() == b'fixture-dll'
    assert not (layout / 'outside.txt').exists()
    assert not list(target.parent.glob('.download-*'))
    assert runtime.install({}, 'rtx30', opener=opener) == str(target)
    opener.assert_called_once()
    assert progress.call_args.args == (len(download), len(download))
    assert (layout / '_internal' / runtime.RUNTIME_NAME).read_bytes() == b'bundled'


@pytest.mark.parametrize('failure', ['archive_hash', 'dll_hash', 'oversized', 'truncated', 'duplicate', 'redirect'])
def test_failed_download_never_publishes_dll_and_cleans_stage(layout, download, monkeypatch, failure):
    data = download
    response = Response
    if failure == 'archive_hash':
        monkeypatch.setitem(runtime.PROFILES['rtx30'], 'archive_sha256', '0' * 64)
    elif failure == 'dll_hash':
        monkeypatch.setitem(runtime.PROFILES['rtx30'], 'sha256', '0' * 64)
    elif failure == 'oversized':
        data += b'extra'
    elif failure == 'truncated':
        data = data[:-1]
    elif failure == 'duplicate':
        data = make_archive(names=('a/nvngx_dlssnr.dll', 'b/nvngx_dlssnr.dll'))
        monkeypatch.setitem(runtime.PROFILES['rtx30'], 'size', len(data))
        monkeypatch.setitem(runtime.PROFILES['rtx30'], 'archive_sha256', hashlib.sha256(data).hexdigest())
    elif failure == 'redirect':
        class BadResponse(Response):
            def geturl(self):
                return 'https://untrusted.example/runtime.zip'
        response = BadResponse
    with pytest.raises(runtime.RuntimeErrorWithGuidance):
        runtime.install({'ui_language': 'en_US'}, 'rtx30', opener=lambda *a, **k: response(data))
    assert not runtime.managed_path({}, 'rtx30').exists()
    assert not list((layout / 'mods').rglob('.download-*'))


def test_modified_managed_dll_is_preserved(layout, download):
    target = runtime.managed_path({}, 'rtx30')
    target.parent.mkdir(parents=True)
    target.write_bytes(b'user-modified')
    with pytest.raises(runtime.RuntimeErrorWithGuidance, match='modified'):
        runtime.install({'ui_language': 'en_US'}, 'rtx30')
    assert target.read_bytes() == b'user-modified'


def test_install_rejects_junction_before_network(layout, download):
    original = runtime.Path.lstat
    def lstat(path, *a, **k):
        info = original(path, *a, **k)
        if path == layout / 'mods':
            return mock.Mock(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    (layout / 'mods').mkdir()
    with mock.patch.object(runtime.Path, 'lstat', lstat), mock.patch.object(runtime.urllib.request, 'urlopen') as network:
        with pytest.raises(ValueError, match='junction'):
            runtime.install({}, 'rtx30')
    network.assert_not_called()


def test_profile_selection_is_persistent_and_translates_without_changing_value():
    for value in ('', '__bundled__', *runtime.PROFILE_SELECTIONS):
        assert runtime.selection_value(runtime.selection_label(value)) == value
        assert app_settings.validate({'dlss_runtime': value})['dlss_runtime'] == value


@pytest.mark.parametrize('failure', [False, True])
def test_frame_test_forces_real_nr_and_closes_process_even_after_failure(failure):
    live = mock.Mock(adapter_info={'name': 'RTX Fixture'}, runtime_path='fixture.dll', backend='v2')
    live.process.return_value = None if failure else np.ones((256, 256, 4), np.uint8)
    factory = mock.Mock(return_value=live)
    if failure:
        with pytest.raises(runtime.RuntimeErrorWithGuidance):
            runtime.test_runtime({}, live_factory=factory)
    else:
        result = runtime.test_runtime({'intensity': 0, 'output_mix': 0, '_nr_bypass': 'source',
            'nr_quality': {'total': 0}}, live_factory=factory)
        assert result['path'] == 'fixture.dll'
        from dlss5tool.appearance import enhancement_bypass
        assert enhancement_bypass(factory.call_args.args[2]) is None
    live.close.assert_called_once()


def test_realtime_choice_uses_dxgi_index_and_skips_integrated_gpu(monkeypatch):
    monkeypatch.setattr(runtime, 'selected_adapter', lambda settings: {})
    records = [{'index': 0, 'vendor': 0x8086, 'name': 'Intel'},
               {'index': 1, 'vendor': 0x10DE, 'name': 'RTX 4090', 'device': 1},
               {'index': 2, 'vendor': 0x10DE, 'name': 'RTX 5090', 'device': 2}]
    assert runtime.gpu_profile(runtime.realtime_adapter({'gpu_index': 2}, records)) == 'rtx50'
    assert runtime.gpu_profile(runtime.realtime_adapter({}, records)) == 'rtx40'
    assert runtime.realtime_adapter({'gpu_index': 9}, records) == {}
    assert runtime.gpu_profile(runtime.realtime_adapter({'gpu_device': 2}, records)) == 'rtx50'


def test_cancel_download_cleans_partial_files(layout, download):
    import threading
    cancel = threading.Event()
    with pytest.raises(runtime.RuntimeErrorWithGuidance, match='cancelled'):
        runtime.install({'ui_language': 'en_US'}, 'rtx30', opener=lambda *a, **k: Response(download),
            progress=lambda *_: cancel.set(), cancel=cancel)
    assert not runtime.managed_path({}, 'rtx30').exists()
    assert not list((layout / 'mods').rglob('.download-*'))


def test_insufficient_space_stops_before_download(layout, download):
    with mock.patch.object(runtime.shutil, 'disk_usage', return_value=mock.Mock(free=1)), \
            mock.patch.object(runtime.urllib.request, 'urlopen') as network:
        with pytest.raises(runtime.RuntimeErrorWithGuidance, match='disk space'):
            runtime.install({'ui_language': 'en_US'}, 'rtx30')
        network.assert_not_called()


def test_cached_inventory_is_read_only_and_does_not_launch_native_probe(monkeypatch):
    import threading
    from types import SimpleNamespace
    from dlss5tool import adapter_probe
    monkeypatch.setattr(adapter_probe, '_default', None)
    with mock.patch.object(adapter_probe, 'AdapterProbe', side_effect=AssertionError('must not probe')):
        assert adapter_probe.cached_inventory() is None
    probe = SimpleNamespace(_condition=threading.Condition(), _cache={'adapters': [adapter('RTX 4090')]})
    monkeypatch.setattr(adapter_probe, '_default', probe)
    snapshot = adapter_probe.cached_inventory()
    snapshot['adapters'].clear()
    assert len(probe._cache['adapters']) == 1


def test_realtime_feature_directory_is_independent_of_managed_nr(layout):
    from dlss5tool.realtime import RealtimeClient
    host = layout / 'host.exe'
    host.touch()
    client = RealtimeClient({'dlss_runtime': '__rtx50__'}, host=host, state_root=layout / 'state')
    with mock.patch('dlss5tool.realtime.subprocess.Popen', side_effect=OSError('stop before spawn')) as process, \
            mock.patch('dlss5tool.realtime_safety.RealtimeSafety'):
        with pytest.raises(OSError, match='stop before spawn'):
            client.launch()
    environment = process.call_args.kwargs['env']
    assert environment['DLSS5_REALTIME_RUNTIME_ROOT'] == str(layout / '_internal')
    assert environment['DLSS5_REALTIME_NR_PATH'] == str(runtime.managed_path({}, 'rtx50'))


def test_engine_uses_actual_gpu_and_preserves_runtime_on_legacy_fallback():
    from dlss5tool import dlss_engine
    actual = adapter('RTX 5090')
    live = dlss_engine.Live.__new__(dlss_engine.Live)
    live.settings = {'_runtime_adapter': adapter('RTX 3090')}
    live._w = live._h = 256
    live.backend = 'v2'
    live._lib = mock.Mock()
    live._refresh_capabilities = mock.Mock()
    with mock.patch.object(dlss_engine, '_configure_host'), mock.patch.object(dlss_engine, '_set_options'), \
            mock.patch.object(dlss_engine, '_selected_render_adapter', return_value=actual), \
            mock.patch.object(dlss_engine, '_configure_render_adapter', side_effect=[([actual], actual), ([], {})]), \
            mock.patch.object(mod_paths, 'runtime_info', return_value={'path': '50-series.dll'}) as info:
        live._open()
        assert info.call_args.kwargs['adapter'] == actual
        live.backend = 'legacy'
        live._open()
        assert info.call_args.kwargs['adapter'] == actual
        assert live.runtime_path == '50-series.dll'
