import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.gpu_export_packaging import validate_candidate_materials


class CandidateMaterialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / 'dlls'
        self.media.mkdir()
        names = ('ffmpeg lame opus dav1d libsvtav1 vpx png webp opencore-amr x264 x265 '
                 'nv-codec-headers amf-headers libvpl pyav libiconv zlib gcc mingw-w64').split()
        self.data = dict(schema=1, pyav_version='18.1.0', materials_collected=True,
                         sources={}, build_sources={}, media_dlls={}, unresolved=['pending review'])
        def write(name):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'fixture')
            return {'file': path.name, 'sha256': hashlib.sha256(b'fixture').hexdigest()}
        for name in names:
            record = write('sources/' + name + '.tar.gz')
            record['notices'] = [write('notices/' + name + '/LICENSE')]
            self.data['sources'][name] = record
        self.data['build_sources']['scripts/build.py'] = write('build-source/scripts/build.py')
        write('NVENC-header-LICENSE.txt')
        for index in range(25):
            record = write(f'dlls/media{index}.dll')
            record['component'] = 'ffmpeg'
            self.data['media_dlls'][record['file']] = record
        self.save()

    def save(self):
        (self.root / 'materials.json').write_text(json.dumps(self.data), encoding='utf-8')

    def test_candidate_preserves_pending_status(self):
        result = validate_candidate_materials(self.root, self.media)
        self.assertFalse(result['distribution_review_complete'])
        self.assertEqual(result['unresolved'], ['pending review'])

    def test_changed_binary_is_rejected(self):
        (self.media / 'media0.dll').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            validate_candidate_materials(self.root, self.media)

    def test_incomplete_sources_are_rejected(self):
        del self.data['sources']['zlib']
        self.save()
        with self.assertRaisesRegex(ValueError, 'inventory incomplete'):
            validate_candidate_materials(self.root, self.media)

    def test_missing_notice_is_rejected(self):
        (self.root / 'notices/zlib/LICENSE').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing or unsafe'):
            validate_candidate_materials(self.root, self.media)

    def test_spec_requires_explicit_local_candidate(self):
        repo = Path(__file__).resolve().parents[1]
        spec = (repo / 'packaging/DLSS5Tool.spec').read_text(encoding='utf-8')
        self.assertIn("os.environ.get('DLSS5_LOCAL_CANDIDATE') != '1'", spec)
        self.assertIn('validate_candidate_materials(', spec)
        script = (repo / 'scripts/build_release.ps1').read_text(encoding='utf-8')
        self.assertIn('[switch]$LocalCandidate', script)
        self.assertIn('$env:DLSS5_LOCAL_CANDIDATE = $previousCandidateMode', script)
