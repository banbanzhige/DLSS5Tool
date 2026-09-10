import tempfile
import unittest
from pathlib import Path
import zipfile

from scripts.package_editions import (
    archive, inventory, link_or_copy, portable_update_name, split_archive, sha,
)


class EditionArchiveTests(unittest.TestCase):
    def test_flow_only_staging_recursively_excludes_depth_weights(self):
        from scripts.package_editions import copy_tree, validate_flow_inventory, MODELS, release_notes_path
        self.assertEqual(len(MODELS), 1)
        self.assertEqual(release_notes_path().parent.name, 'release')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'source'
            (source / '_internal/models').mkdir(parents=True)
            (source / '_internal/models/depth_anything_v2_vitl.pth').write_bytes(b'fixture')
            (source / 'guidance_worker.exe').write_bytes(b'unchanged')
            copy_tree(source, root / 'staged', flow_only=True)
            validate_flow_inventory(inventory(root / 'staged'))
            self.assertTrue((source / '_internal/models/depth_anything_v2_vitl.pth').exists())
            self.assertEqual((root / 'staged/guidance_worker.exe').read_bytes(), b'unchanged')
        with self.assertRaises(ValueError):
            validate_flow_inventory({'mods/enhancement/models/depth_anything_v2_vits.pth': {}})

    def test_archive_paths_crc_inventory_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / 'addon'
            (folder / 'mods/models').mkdir(parents=True)
            (folder / 'mods/models/fixture.pth').write_bytes(b'test-model-bytes')
            (folder / 'mods/README.md').write_text('说明', encoding='utf-8')
            manifest = inventory(folder)
            target = root / 'addon.zip'
            result = archive(folder, target, manifest)
            self.assertTrue(result['crc_verified'])
            self.assertEqual(result['sha256'], sha(target))
            with zipfile.ZipFile(target) as zip:
                self.assertEqual(set(zip.namelist()), set(manifest))
                self.assertTrue(all(name.startswith('mods/') for name in zip.namelist()))
            with self.assertRaises(FileExistsError):
                archive(folder, target, manifest)

    def test_upload_volumes_reconstruct_and_small_files_are_not_split(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_path = root / 'fixture.zip'
            data = bytes(range(256))*5
            archive_path.write_bytes(data)
            self.assertEqual(split_archive(archive_path, root / 'unused'), [])
            parts = split_archive(archive_path, root / 'volumes', part_bytes=500, max_asset_bytes=600)
            self.assertEqual([p['size'] for p in parts], [500, 500, 280])
            self.assertEqual(b''.join((root / 'volumes' / p['name']).read_bytes() for p in parts), data)
            self.assertTrue(all(p['size'] < 600 for p in parts))
            for p in parts:
                self.assertEqual(p['sha256'], sha(root / 'volumes' / p['name']))

    def test_canonical_update_name_and_hardlink_copy(self):
        self.assertEqual(portable_update_name('v2.1.1'), 'DLSS5Tool-v2.1.1-win64.zip')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'DLSS5Tool-v2.1.1-win64-lite.zip'
            source.write_bytes(b'lite-bytes')
            destination = root / 'github-assets' / portable_update_name('v2.1.1')
            link_or_copy(source, destination)
            self.assertEqual(destination.read_bytes(), b'lite-bytes')
            self.assertEqual(sha(source), sha(destination))
