"""Stable settings/models for a managed portable app; no updater installation."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from dlss5tool import paths, mod_paths
from dlss5tool.portable_layout import LAYOUT_FILE, LayoutError, load_portable_layout


class ManagedPortableLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binary = self.root / 'current'
        self.resources = self.binary / '_internal'
        self.resources.mkdir(parents=True)
        (self.root / '.portable').touch()
        (self.root / 'Update.exe').write_bytes(b'updater fixture, not executed')
        self.marker = self.resources / LAYOUT_FILE
        self.config = {'schema': 1, 'provider': 'velopack-portable',
                       'package_id': 'DLSS5Tool', 'edition': 'lite', 'channel': 'win-lite'}
        self.write_layout()

    def write_layout(self, *, package_id='DLSS5Tool', channel='win-lite', main='DLSS5Tool.exe'):
        self.marker.write_text(json.dumps(self.config), encoding='utf-8')
        (self.binary / 'sq.version').write_text(
            '<package xmlns="http://schemas.microsoft.com/packaging/2010/07/nuspec.xsd">'
            f'<metadata><id>{package_id}</id><channel>{channel}</channel>'
            f'<mainExe>{main}</mainExe><version>1.0.0</version></metadata></package>', encoding='utf-8')

    def load(self):
        return load_portable_layout(self.binary, self.resources, 'DLSS5Tool.exe')

    def frozen(self):
        return (mock.patch.object(sys, 'frozen', True, create=True),
                mock.patch.object(sys, 'executable', str(self.binary / 'DLSS5Tool.exe')),
                mock.patch.object(sys, '_MEIPASS', str(self.resources), create=True))

    def test_settings_queue_and_models_stay_outside_replaced_current(self):
        for edition in ('lite', 'full'):
            with self.subTest(edition=edition):
                self.config.update(edition=edition, channel='win-' + edition)
                self.write_layout(channel='win-' + edition)
                patches = self.frozen()
                with patches[0], patches[1], patches[2]:
                    self.assertEqual(paths.app_root(), self.binary)
                    self.assertEqual(paths.resource_root(), self.resources)
                    self.assertEqual(paths.runtime_root(), self.resources)
                    self.assertEqual(paths.portable_root(), self.root)
                    self.assertEqual(paths.state_path('dlss5_settings.json'), self.root / 'dlss5_settings.json')
                    self.assertEqual(paths.state_path('dlss5_queue.json'), self.root / 'dlss5_queue.json')
                    self.assertEqual(mod_paths.mods_root(), self.root / 'mods')
                    self.assertEqual(mod_paths.mods_root({'mods_directory': 'my-mods'}), self.root / 'my-mods')
                    external = self.root / 'external'
                    self.assertEqual(mod_paths.mods_root({'mods_directory': str(external)}), external)
                    self.assertEqual(mod_paths.bundled_runtime(), str(self.resources / 'nvngx_dlssnr.dll'))

    def test_ordinary_portable_package_needs_no_new_metadata(self):
        ordinary = self.root / 'ordinary'
        (ordinary / '_internal').mkdir(parents=True)
        self.assertIsNone(load_portable_layout(ordinary, ordinary / '_internal', 'DLSS5Tool.exe'))
        with mock.patch.object(sys, 'frozen', True, create=True), \
             mock.patch.object(sys, 'executable', str(ordinary / 'DLSS5Tool.exe')), \
             mock.patch.object(sys, '_MEIPASS', str(ordinary / '_internal'), create=True):
            self.assertEqual(paths.portable_root(), ordinary)
            self.assertEqual(paths.state_path('dlss5_settings.json'), ordinary / 'dlss5_settings.json')
            self.assertEqual(mod_paths.mods_root(), ordinary / 'mods')

    def test_source_layout_does_not_load_managed_metadata(self):
        with mock.patch.object(sys, 'frozen', False, create=True), \
             mock.patch.object(paths, 'project_root', return_value=self.root):
            self.assertEqual(paths.portable_root(), self.root)
            self.assertEqual(mod_paths.mods_root(), self.root / 'mods')

    def test_missing_marker_in_managed_package_cannot_create_fresh_state_in_current(self):
        self.marker.unlink()
        with self.assertRaises(LayoutError):
            self.load()
        self.assertFalse((self.binary / 'dlss5_settings.json').exists())

    def test_invalid_marker_is_not_a_silent_legacy_fallback(self):
        for field, value in (('schema', True), ('schema', 2), ('provider', 'other'),
                             ('package_id', 'OtherApp'), ('package_id', '../DLSS5Tool'),
                             ('channel', 'win-full'), ('edition', 'unknown')):
            with self.subTest(field=field, value=value):
                config = {**self.config, field: value}
                self.marker.write_text(json.dumps(config))
                with self.assertRaises(LayoutError):
                    self.load()
        self.marker.write_text(json.dumps({**self.config, 'data_root': '../../elsewhere'}))
        with self.assertRaises(LayoutError):
            self.load()

    def test_package_id_channel_and_executable_must_match(self):
        for arguments in ({'package_id': 'OtherApp'}, {'channel': 'win-full'}, {'main': 'OtherApp.exe'}):
            with self.subTest(arguments=arguments):
                self.write_layout(**arguments)
                with self.assertRaises(LayoutError):
                    self.load()

    def test_missing_framework_metadata_is_rejected(self):
        for name in ('.portable', 'Update.exe', 'current/sq.version'):
            with self.subTest(name=name):
                path = self.root / name
                original = path.read_bytes()
                path.unlink()
                try:
                    with self.assertRaises(LayoutError):
                        self.load()
                finally:
                    path.write_bytes(original)

    def test_large_or_invalid_metadata_is_rejected(self):
        for content in (b'broken json', b'x' * (64 * 1024 + 1)):
            self.marker.write_bytes(content)
            with self.assertRaises(LayoutError):
                self.load()
        self.write_layout()
        (self.binary / 'sq.version').write_bytes(b'broken xml')
        with self.assertRaises(LayoutError):
            self.load()

    def test_hardlinked_layout_marker_is_rejected(self):
        copy = self.resources / 'shared.json'
        os.link(self.marker, copy)
        with self.assertRaises(LayoutError):
            self.load()


if __name__ == '__main__':
    unittest.main()
