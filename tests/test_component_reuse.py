import unittest

from dlss5tool import updater
from scripts.package_component_reuse import reuse_notices


class ComponentReuseTests(unittest.TestCase):
    def setUp(self):
        self.lite = {'DLSS5Tool.exe': 'new', 'mods/README.md': 'new docs'}
        self.old = {'DLSS5Tool.exe': 'old', 'mods/enhancement/guidance_worker.exe': 'worker',
                    'mods/models/raft.pth': 'model',
                    'mods/enhancement/DISTRIBUTION-REVIEW.txt': 'old notice'}
        self.full = {**self.lite, **{n: r for n, r in self.old.items() if n.startswith('mods/')},
                     'mods/ADDON-INSTALL.txt': 'new notice',
                     'mods/enhancement/DISTRIBUTION-REVIEW.txt': 'new notice'}

    def test_exact_overlay_with_notices_only(self):
        self.assertEqual(reuse_notices(self.old, self.lite, self.full),
                         {'mods/ADDON-INSTALL.txt', 'mods/enhancement/DISTRIBUTION-REVIEW.txt'})

    def test_changed_missing_extra_binary_or_model_rejected(self):
        for operation in ('changed', 'missing', 'extra'):
            with self.subTest(operation=operation):
                old = dict(self.old)
                if operation == 'changed':
                    old['mods/models/raft.pth'] = 'different'
                elif operation == 'missing':
                    del old['mods/models/raft.pth']
                else:
                    old['mods/enhancement/extra.dll'] = 'extra'
                with self.assertRaises(ValueError):
                    reuse_notices(old, self.lite, self.full)

    def test_reuse_archive_never_selected_as_portable_app(self):
        asset = updater.ReleaseAsset('DLSS5Tool-v2.3.1-addon-component-reuse.zip', 'https://github.com/a', 2000)
        release = updater.ReleaseInfo('v2.3.1', updater.RELEASES_URL, '', (asset,))
        self.assertIsNone(updater.select_portable_asset(release))
