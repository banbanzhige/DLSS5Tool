"""Source-level packaging contracts and pre-archive isolation checks."""
import ast
from pathlib import Path
import tempfile
import unittest

from dlss5tool import app_version
from scripts.check_release_contents import forbidden_contents


ROOT = Path(__file__).resolve().parents[1]


class ReleasePackagingTests(unittest.TestCase):
    def test_version_resources_match_application(self):
        resource = (ROOT / 'packaging/DLSS5Tool.version.txt').read_text(encoding='utf-8')
        version_tuple = tuple(map(int, app_version.__version__.split('.'))) + (0,)
        self.assertIn(f'filevers={version_tuple}', resource)
        self.assertIn(f'prodvers={version_tuple}', resource)
        for key in ('FileVersion', 'ProductVersion'):
            self.assertIn(f"StringStruct(u'{key}', u'{app_version.__version__}')", resource)

    def test_main_spec_excludes_developer_python_modules(self):
        tree = ast.parse((ROOT / 'packaging/DLSS5Tool.spec').read_text(encoding='utf-8'))
        analysis = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name) and node.func.id == 'Analysis')
        excludes = ast.literal_eval(next(key.value for key in analysis.keywords if key.arg == 'excludes'))
        self.assertTrue({'amd_devtest', 'amd_devtest_ui', 'scripts', 'tests',
                         'guidance_worker', 'torch', 'torchvision', 'depth_anything_v2'} <= set(excludes))

    def test_main_release_allows_runtime_and_user_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('DLSS5Tool.exe', 'README.md', 'CHANGELOG.md',
                         '_internal/GUIDANCE_PARAMETERS.md', '_internal/nvngx_dlssnr.dll',
                         '_internal/base_library.zip', 'mods/README.md'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            self.assertEqual(forbidden_contents(root), [])

    def test_main_release_rejects_amd_experiments_and_user_components(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'DLSS5Tool.exe').touch()
            names = ('AMD-DevTest.exe', '_internal/amd_devtest.py', 'AMD_DEVTEST.md',
                     '_internal/FLOW_QUALITY_TEST.md', 'scripts/flow_quality_probe.py',
                     'tests/test_amd_devtest.py', 'native_amd_probe/amd_probe.cpp',
                     'amd_backend/version.dll', 'output/report.json',
                     'mods/enhancement/enhancement.json')
            for name in names:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            self.assertEqual(forbidden_contents(root), sorted(names))

    def test_release_check_runs_before_compression(self):
        script = (ROOT / 'scripts/build_release.ps1').read_text(encoding='utf-8')
        self.assertLess(script.index('check_release_contents.py'), script.index('Compress-Archive'))
        self.assertIn('Assert-ExternalSuccess "Checking release content isolation"', script)

    def test_release_builds_independent_updater_before_archiving(self):
        script = (ROOT / 'scripts/build_release.ps1').read_text(encoding='utf-8')
        self.assertLess(script.index('build_update_helper.ps1'), script.index('Compress-Archive'))
        helper = (ROOT / 'scripts/build_update_helper.ps1').read_text(encoding='utf-8')
        self.assertIn('--onefile', helper)
        self.assertIn('DLSS5Update', helper)

    def test_release_rejects_update_staging_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'DLSS5Tool.exe').touch()
            (root / '.dlss5-update').mkdir()
            (root / '.dlss5-update/state.json').touch()
            self.assertEqual(forbidden_contents(root), ['.dlss5-update/state.json'])
