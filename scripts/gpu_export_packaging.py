"""Validate local candidate inputs without claiming distribution approval."""
import hashlib
import json
from pathlib import Path


def validate_candidate_materials(root, media_directory):
    root = Path(root)
    materials = json.loads((root / 'materials.json').read_text(encoding='utf-8'))
    if (materials.get('schema') != 1 or materials.get('pyav_version') != '18.1.0'
            or materials.get('materials_collected') is not True):
        raise ValueError('GPU export material collection incomplete or wrong version')

    def verify(base, name, record):
        path = base / name
        if not path.resolve().is_relative_to(base.resolve()) or path.is_symlink() or not path.is_file():
            raise ValueError('Missing or unsafe GPU material: ' + str(path))
        with path.open('rb') as source:
            actual = hashlib.file_digest(source, 'sha256').hexdigest()
        if actual != record.get('sha256') or ('bytes' in record and path.stat().st_size != record['bytes']):
            raise ValueError('GPU material hash mismatch: ' + str(path))

    required = {'ffmpeg', 'lame', 'opus', 'dav1d', 'libsvtav1', 'vpx', 'png', 'webp',
                'opencore-amr', 'x264', 'x265', 'nv-codec-headers', 'amf-headers', 'libvpl',
                'pyav', 'libiconv', 'zlib', 'gcc', 'mingw-w64'}
    if set(materials.get('sources', {})) != required or not materials.get('build_sources'):
        raise ValueError('GPU export source inventory incomplete')
    for name, record in materials['sources'].items():
        verify(root / 'sources', record['file'], record)
        if not record.get('notices'):
            raise ValueError('Missing notices: ' + name)
        for notice in record['notices']:
            verify(root / 'notices' / name, notice['file'], notice)
    for name, record in materials['build_sources'].items():
        verify(root / 'build-source', name, record)
    if not (root / 'NVENC-header-LICENSE.txt').is_file():
        raise ValueError('Missing NVENC header notice')
    media_directory = Path(media_directory)
    dlls = materials.get('media_dlls', {})
    if len(dlls) != 25 or set(dlls) != {p.name for p in media_directory.glob('*.dll')}:
        raise ValueError('GPU media DLL inventory differs from material record')
    for name, record in dlls.items():
        if record.get('component') not in required:
            raise ValueError('Unknown media DLL component')
        verify(media_directory, name, record)
    return {'status': 'local-candidate', 'distribution_review_complete': False,
            'unresolved': materials.get('unresolved', []),
            'materials_sha256': hashlib.sha256((root / 'materials.json').read_bytes()).hexdigest()}
