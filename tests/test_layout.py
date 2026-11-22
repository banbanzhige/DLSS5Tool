"""Source layout contracts; no native loading, GPU or PyInstaller build."""
import ast
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from dlss5tool import app_settings, export_queue, paths, mod_paths


ROOT = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def _temporary_environ(**updates):
    """Mutate only specific keys so Windows empty-string env vars stay intact."""
    sentinel = object()
    previous = {key: os.environ.get(key, sentinel) for key in updates}
    try:
        for key, value in updates.items():
            os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is sentinel:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class LayoutTests(unittest.TestCase):
    def test_source_resources_and_runtimes_are_separate(self):
        self.assertEqual(paths.project_root(), ROOT)
        self.assertEqual(paths.resource_root(), ROOT)
        self.assertEqual(paths.runtime_root(), ROOT / 'runtime')
        self.assertEqual(mod_paths.mods_root(), ROOT / 'mods')
        self.assertTrue((paths.resource_root() / 'assets/app.ico').is_file())
        self.assertTrue((paths.resource_root() / 'locales/en_US.json').is_file())

    def test_legacy_state_is_copied_once_without_overwriting_or_deleting(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(paths, 'project_root', return_value=Path(directory)):
            root = Path(directory)
            for name in ('dlss5_settings.json', 'dlss5_queue.json', 'dlss_run.log'):
                old = root / name
                old.write_bytes(b'original state')
                new = paths.state_path(name)
                self.assertEqual(new, root / 'var' / name)
                self.assertEqual(new.read_bytes(), old.read_bytes())
                new.write_bytes(b'new state')
                self.assertEqual(paths.state_path(name).read_bytes(), b'new state')
                self.assertEqual(old.read_bytes(), b'original state')

    def test_explicit_state_overrides_do_not_trigger_migration(self):
        with _temporary_environ(DLSS5TOOL_SETTINGS_PATH='custom-settings.json',
                                DLSS5TOOL_QUEUE_PATH='custom-queue.json'), \
                mock.patch.object(paths, 'state_path', side_effect=AssertionError('must not migrate')):
            self.assertEqual(app_settings.settings_path(), os.path.abspath('custom-settings.json'))
            self.assertEqual(export_queue.queue_path(), os.path.abspath('custom-queue.json'))

    def test_portable_layout_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(sys, 'frozen', True, create=True), \
                mock.patch.object(sys, 'executable', str(Path(directory) / 'DLSS5Tool.exe')), \
                mock.patch.object(sys, '_MEIPASS', str(Path(directory) / '_internal'), create=True):
            root = Path(directory)
            self.assertEqual(paths.app_root(), root)
            self.assertEqual(paths.resource_root(), root / '_internal')
            self.assertEqual(paths.runtime_root(), root / '_internal')
            self.assertEqual(mod_paths.bundled_runtime(), str(root / '_internal/nvngx_dlssnr.dll'))
            self.assertEqual(mod_paths.mods_root(), root / 'mods')
            self.assertEqual(paths.state_path('dlss5_settings.json'), root / 'dlss5_settings.json')
            self.assertEqual(paths.state_path('dlss5_queue.json'), root / 'dlss5_queue.json')
            self.assertFalse((root / 'var').exists())

    def test_application_imports_are_package_qualified(self):
        names = {p.stem for p in (ROOT / 'dlss5tool').glob('*.py')} - {'__init__', '__main__'}
        for folder in ('dlss5tool', 'scripts', 'tests', 'packaging'):
            for path in (ROOT / folder).rglob('*'):
                if path.suffix not in ('.py', '.spec'):
                    continue
                for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
                    if isinstance(node, ast.Import):
                        self.assertFalse(names.intersection(alias.name for alias in node.names), str(path))
                    elif isinstance(node, ast.ImportFrom) and node.level == 0:
                        self.assertNotIn(node.module, names, str(path))

    def test_root_has_no_application_modules_or_build_payloads(self):
        forbidden = {'.dll', '.obj', '.lib', '.exp', '.pdb', '.spec', '.ps1'}
        for path in ROOT.iterdir():
            if path.is_file():
                self.assertNotIn(path.suffix, forbidden, path.name)
                if path.suffix == '.py':
                    self.assertEqual(path.name, 'gui.py')

    def test_worker_cli_imports_without_gpu_or_gui_startup(self):
        for module in ('dlss5tool.parallel_export_worker', 'dlss5tool.amd_devtest', 'dlss5tool.guidance_worker'):
            result = subprocess.run([sys.executable, '-B', '-m', module, '--help'], cwd=ROOT,
                                    capture_output=True, text=True, timeout=20,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('usage:', result.stdout.lower())

    def test_compatibility_launcher_works_outside_project_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-B', str(ROOT / 'gui.py'), '--parallel-worker', '--help'],
                                    cwd=directory, capture_output=True, text=True, timeout=20,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('usage:', result.stdout.lower())
        result = subprocess.run([sys.executable, '-B', '-m', 'dlss5tool', '--parallel-worker', '--help'],
                                cwd=ROOT, capture_output=True, text=True, timeout=20,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_packaging_source_paths_exist_in_reorganized_layout(self):
        # Evaluate recipe setup only, stopping at Analysis. No packaging tools,
        # builds, downloads or proprietary DLLs are required by this fixture.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures = ('gui.py', 'dlss5tool/amd_devtest.py', 'dlss5tool/guidance_worker.py',
                        'assets/app.ico', 'assets/app.png', 'locales/en_US.json', 'locales/zh_CN.json',
                        'LICENSE', 'README.md', 'README.en.md', 'THIRD_PARTY_NOTICES.md',
                        'licenses/DLSS5Tool-original-MIT.txt',
                        'docs/guidance/GUIDANCE_PARAMETERS.md', 'licenses/torchvision-LICENSE.txt',
                        'runtime/dlssnr_host_v2.dll', 'runtime/nvngx_dlssnr.dll',
                        'runtime/vsr_host.dll', 'runtime/nvngx_vsr.dll',
                        'runtime/dlssg_video_worker.exe', 'runtime/nvngx_dlssg.dll',
                        'licenses/NVIDIA-Optical-Flow-Headers-LICENSE.txt', 'scripts/rtxmfg_temporal/LICENSE.txt',
                        'third_party/NVIDIA-DLSS/LICENSE.txt',
                        'third_party/NVIDIA-DLSS/lib/Windows_x86_64/rel/nvngx_dlss.dll',
                        'media/bin/ffmpeg.exe', 'media/bin/ffprobe.exe', 'media/LICENSE', 'media/README.txt',
                        'build/amd_probe/amd_probe.exe',
                        'third_party/FidelityFX-1.1.4/PrebuiltSignedDLL/amd_fidelityfx_dx12.dll',
                        'third_party/FidelityFX-1.1.4/LICENSE.txt', 'depth/depth_anything_v2/dpt.py')
            for name in fixtures:
                file = root / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.touch()
            realtime = root / 'runtime/realtime'
            realtime_files = {}
            for name in ('dlss5_realtime_host.exe', 'licenses/DLSS5Tool-GPLv3.txt',
                         'REALTIME-NOTICE.txt', 'sources.lock.json'):
                file = realtime / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.touch()
                realtime_files[name] = {'size': 0, 'sha256': hashlib.sha256(b'').hexdigest()}
            (realtime / 'build-manifest.json').write_text(json.dumps({
                'version': 1, 'files': realtime_files, 'packaging_license_review_complete': False,
            }), encoding='utf-8')
            for recipe in (ROOT / 'packaging').glob('*.spec'):
                tree = ast.parse(recipe.read_text(encoding='utf-8'))
                prefix = []
                for node in tree.body:
                    if isinstance(node, ast.ImportFrom) and node.module == 'PyInstaller.utils.hooks':
                        continue
                    prefix.append(node)
                    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == 'Analysis':
                        break
                capture = mock.Mock()
                namespace = {'SPECPATH': str(root / 'packaging'), 'Analysis': capture,
                             'collect_all': lambda _: ([], [], []), 'copy_metadata': lambda _: []}
                real_file_digest = hashlib.file_digest
                def fixture_digest(handle, algorithm):
                    if Path(handle.name) == root/'third_party/NVIDIA-DLSS/lib/Windows_x86_64/rel/nvngx_dlss.dll':
                        value = mock.Mock()
                        value.hexdigest.return_value = 'be6e434a94ca32499515eb62ca0e6c274526055d568d0426e4c652dcdfb6ee6e'
                        return value
                    return real_file_digest(handle, algorithm)
                with mock.patch.object(sys, 'path', list(sys.path)), \
                        mock.patch.object(hashlib, 'file_digest', side_effect=fixture_digest), \
                        _temporary_environ(DLSS5_DEPTH_SOURCE=str(root / 'depth'), DLSS5_LOCAL_CANDIDATE='1'), \
                        mock.patch('dlss5tool.gpu_export_runtime.load_components', return_value=None), \
                        mock.patch('dlss5tool.cuda_vsr_runtime.load_components', return_value=None), \
                        mock.patch('dlss5tool.resident_sr_runtime.load_components', return_value=None), \
                        mock.patch('dlss5tool.resident_hdr_runtime.load_components', return_value=None), \
                        mock.patch('dlss5tool.frame_generation.runtime_files', return_value=(root/'runtime/dlssg_video_worker.exe', root/'runtime/nvngx_dlssg.dll')), \
                        mock.patch('dlss5tool.frame_generation.PINNED_RUNTIME', __import__('hashlib').sha256(b'').hexdigest()), \
                        mock.patch('dlss5tool.video_export.find_ffmpeg', return_value=str(root/'media/bin/ffmpeg.exe')), \
                        mock.patch('dlss5tool.video_export.find_ffprobe', return_value=str(root/'media/bin/ffprobe.exe')):
                    exec(compile(ast.Module(body=prefix, type_ignores=[]), str(recipe), 'exec'), namespace)
                self.assertEqual(capture.call_count, 1)
                call = capture.call_args
                for source in call.args[0]:
                    self.assertTrue(Path(source).is_file(), source)
                for source, destination in call.kwargs.get('datas', []) + call.kwargs.get('binaries', []):
                    self.assertTrue(Path(source).is_file(), source)
                self.assertIn(str(root), call.kwargs['pathex'])
                if recipe.name == 'DLSS5Tool.spec':
                    self.assertTrue({'dlss5tool.amd_devtest', 'dlss5tool.amd_devtest_ui'} <= set(call.kwargs['excludes']))
                    # The packaged --nvofa-worker entry uses the Torch-free
                    # guidance worker. Only its obsolete top-level shim is excluded.
                    self.assertNotIn('dlss5tool.guidance_worker', call.kwargs['excludes'])
                    self.assertIn('guidance_worker', call.kwargs['excludes'])

    @staticmethod
    def _doc_markdown_files(root=ROOT):
        top_level = [file for file in root.glob('*.md') if file.is_file()]
        docs = [file for file in (root / 'docs').rglob('*.md') if file.is_file()]
        if (root / '.git').exists():
            # Use -z (NUL-separated raw UTF-8 paths) so non-ASCII filenames are
            # never quoted/escaped by core.quotePath while still skipping ignored
            # local-only notes under docs/development/.
            listed = subprocess.run(
                ['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', 'docs'],
                cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=20,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            if listed.returncode == 0:
                docs = [root / entry for entry in listed.stdout.split('\0')
                        if entry and entry.lower().endswith('.md') and (root / entry).is_file()]
        return [*top_level, *docs, root / 'mods/README.md']

    @classmethod
    def _broken_doc_links(cls, root=ROOT):
        broken = []
        for file in cls._doc_markdown_files(root):
            for target in re.findall(r'\]\(([^\s)]+)\)', file.read_text(encoding='utf-8')):
                if re.match(r'^(?:[a-z]+:|#|/)', target, re.I):
                    continue
                relative = target.split('#', 1)[0]
                if not (file.parent / relative).exists():
                    broken.append(f'{file.relative_to(root)}: {target}')
        return broken

    def test_documentation_links_resolve(self):
        self.assertTrue((ROOT / 'mods/README.md').is_file(), 'Required packaging doc mods/README.md is missing')
        files = self._doc_markdown_files()
        self.assertTrue(files)
        self.assertEqual(self._broken_doc_links(), [])

    def test_documentation_link_discovery_handles_non_ascii_filenames_under_quotepath(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'docs/development').mkdir(parents=True)
            (root / 'mods').mkdir()
            (root / '.gitignore').write_text('docs/development/\n', encoding='utf-8')
            (root / 'mods/README.md').write_text('# mods\n', encoding='utf-8')
            tracked = root / 'docs/中文说明.md'
            tracked.write_text('[bad](missing-tracked.md)\n', encoding='utf-8')
            untracked = root / 'docs/新增指南.md'
            untracked.write_text('[bad](missing-untracked.md)\n', encoding='utf-8')
            ignored = root / 'docs/development/本地草稿.md'
            ignored.write_text('[ignored](missing-ignored.md)\n', encoding='utf-8')
            subprocess.run(['git', 'init'], cwd=root, check=True, capture_output=True,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            subprocess.run(['git', 'config', 'core.quotePath', 'true'], cwd=root, check=True,
                           capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            subprocess.run(['git', 'add', 'docs/中文说明.md'], cwd=root, check=True,
                           capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            discovered = self._doc_markdown_files(root)
            self.assertIn(tracked, discovered)
            self.assertIn(untracked, discovered)
            self.assertNotIn(ignored, discovered)
            broken = self._broken_doc_links(root)
            self.assertEqual(len(broken), 2)
            self.assertTrue(any('中文说明.md: missing-tracked.md' in item for item in broken))
            self.assertTrue(any('新增指南.md: missing-untracked.md' in item for item in broken))
            (root / 'mods/README.md').unlink()
            self.assertIn(root / 'mods/README.md', self._doc_markdown_files(root))
            with self.assertRaises(FileNotFoundError):
                self._broken_doc_links(root)
