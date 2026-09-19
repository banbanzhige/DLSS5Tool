"""Collect pinned GPU-export dependency materials; never changes runtime binaries.

Downloads are source/notice artifacts only. Upstream Python is parsed as AST,
never executed. Existing files are reused only with matching SHA-256.
"""
import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import shutil
import sys
import tarfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
VENDOR = 'https://raw.githubusercontent.com/PyAV-Org/pyav-ffmpeg/8.1.2-1/'
REQUIRED = {'ffmpeg', 'lame', 'opus', 'dav1d', 'libsvtav1', 'vpx', 'png', 'webp',
            'opencore-amr', 'x264', 'x265', 'nv-codec-headers', 'amf-headers', 'libvpl'}


def sha(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def fetch(url, target, expected=None):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        request = urllib.request.Request(url, headers={'User-Agent': 'DLSS5Tool-release-materials/1.0'})
        partial = target.with_suffix(target.suffix + '.partial')
        if partial.exists():
            raise RuntimeError('Previous incomplete download: ' + str(partial))
        with urllib.request.urlopen(request, timeout=20) as response, partial.open('xb') as dest:
            count = 0
            deadline = time.monotonic() + 120
            while data := response.read(1024 * 1024):
                count += len(data)
                if count > 180 * 1024**2 or time.monotonic() > deadline:
                    raise RuntimeError('Source download exceeds time/size budget: ' + url)
                dest.write(data)
        if expected and sha(partial) != expected:
            raise RuntimeError('Source SHA mismatch: ' + str(partial))
        partial.rename(target)
    actual = sha(target)
    if expected and actual != expected:
        raise RuntimeError(f'Source SHA mismatch: {target.name}: {actual} != {expected}')
    return dict(url=url, file=target.name, sha256=actual, bytes=target.stat().st_size)


def pinned_packages(text):
    packages = {}
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != 'Package':
            continue
        fields = {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords
                  if kw.arg in ('name', 'source_url', 'sha256')}
        if fields.get('name') in REQUIRED:
            packages[fields['name']] = fields
    if set(packages) != REQUIRED:
        raise RuntimeError('Incomplete upstream dependency lock')
    return packages


def extract_notices(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    found = []
    with tarfile.open(archive, 'r:*') as source:
        for member in source:
            name = Path(member.name).name
            if (not member.isfile() or member.size > 512 * 1024
                    or not re.match(r'(?i)^(COPYING|LICENSE|LICENCE|NOTICE|COPYRIGHT|AUTHORS)([._-]|$)', name)):
                continue
            # Preserve every matching notice without trusting archive paths.
            key = hashlib.sha256(member.name.encode()).hexdigest()[:10] + '-' + name
            target = destination / key
            payload = source.extractfile(member).read()
            if target.exists() and target.read_bytes() != payload:
                raise RuntimeError('Existing notice mismatch: ' + str(target))
            target.write_bytes(payload)
            found.append(dict(source_path=member.name, file=target.name, sha256=sha(target)))
    if not found:
        raise RuntimeError('No license notices in ' + str(archive))
    return found


def collect(work, destination):
    if importlib.metadata.version('av') != '18.1.0':
        raise RuntimeError('Requires verified PyAV 18.1.0')
    if not (work / 'TASK.md').is_file():
        raise RuntimeError('Task must be registered')
    if shutil.disk_usage(work).free < 16 * 1024**3:
        raise RuntimeError('Insufficient free space')
    destination.mkdir(parents=True, exist_ok=True)
    upstream = destination / 'build-source'
    build_records = {}
    for name in ('scripts/pkg.py', 'scripts/build-ffmpeg.py', 'scripts/cibuildpkg.py',
                 'scripts/grab.py', 'scripts/cache.py', 'pyproject.toml', 'setup.py',
                 '.github/workflows/build-ffmpeg.yml', 'patches/ffmpeg.patch',
                 'patches/amf-headers.patch', 'patches/gmp.patch', 'patches/lame.patch', 'patches/vpx.patch'):
        build_records[name] = fetch(VENDOR + name, upstream / name)
    packages = pinned_packages((upstream / 'scripts/pkg.py').read_text(encoding='utf-8'))
    packages.update({
        'pyav': dict(name='pyav', source_url='https://github.com/PyAV-Org/PyAV/archive/refs/tags/v18.1.0.tar.gz'),
        'libiconv': dict(name='libiconv', source_url='https://ftp.gnu.org/gnu/libiconv/libiconv-1.19.tar.gz'),
        'zlib': dict(name='zlib', source_url='https://zlib.net/zlib-1.3.2.tar.gz'),
        'gcc': dict(name='gcc', source_url='https://ftp.gnu.org/gnu/gcc/gcc-16.1.0/gcc-16.1.0.tar.xz'),
        'mingw-w64': dict(name='mingw-w64', source_url='https://github.com/mingw-w64/mingw-w64/archive/refs/tags/v13.0.0.tar.gz'),
    })
    def download(item):
        name, package = item
        url = package['source_url'].replace('http://deb.debian.org/', 'https://deb.debian.org/')
        suffix = '.tar.bz2' if url.endswith('.bz2') else '.tar.xz' if url.endswith('.xz') else '.tar.gz'
        target = destination / 'sources' / (name + suffix)
        record = fetch(url, target, package.get('sha256'))
        record['notices'] = extract_notices(target, destination / 'notices' / name)
        print(json.dumps(dict(component=name, bytes=record['bytes'], status='collected')), flush=True)
        return name, record
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = dict(pool.map(download, packages.items()))
    dist = importlib.metadata.distribution('av')
    installed = Path(dist.locate_file('av.libs'))
    mapping = {'avcodec': 'ffmpeg', 'avdevice': 'ffmpeg', 'avfilter': 'ffmpeg', 'avformat': 'ffmpeg',
        'avutil': 'ffmpeg', 'swresample': 'ffmpeg', 'swscale': 'ffmpeg', 'libdav1d': 'dav1d',
        'libgcc_s_seh': 'gcc', 'libstdc++': 'gcc', 'libiconv': 'libiconv', 'libmp3lame': 'lame',
        'libopencore-amrnb': 'opencore-amr', 'libopencore-amrwb': 'opencore-amr', 'libopus': 'opus',
        'libsharpyuv': 'webp', 'libSvtAv1Enc': 'libsvtav1', 'libvpl': 'libvpl', 'libvpx': 'vpx',
        'libwebp': 'webp', 'libwebpmux': 'webp', 'libwinpthread': 'mingw-w64',
        'libx264': 'x264', 'libx265': 'x265', 'zlib1': 'zlib'}
    binaries = {}
    for dll in sorted(installed.glob('*.dll')):
        matches = [prefix for prefix in mapping if dll.name.startswith(prefix + '-')]
        if not matches:
            raise RuntimeError('Unclassified bundled DLL: ' + dll.name)
        prefix = max(matches, key=len)
        binaries[dll.name] = dict(sha256=sha(dll), component=mapping[prefix])
    if len(binaries) != 25:
        raise RuntimeError('Unexpected media DLL set')
    nv_header = ROOT / 'tmp/encode-acceptance-20260916/nvEncodeAPI.h'
    notice = nv_header.read_text(encoding='utf-8').split('*/', 1)[0] + '*/\n'
    (destination / 'NVENC-header-LICENSE.txt').write_text(notice, encoding='utf-8')
    # Materials are collected, not declared legally approved by a Boolean switch.
    manifest = dict(schema=1, pyav_version='18.1.0', vendor_ref='8.1.2-1',
        sources=records, build_sources=build_records, media_dlls=binaries,
        review_notes=[
            'Upstream FFmpeg configure patch reclassifies x264/x265; DLL LGPL label is not a complete license inventory.',
            'x264/x265 GPL terms are retained; do not describe the entire wheel as BSD or LGPL only.',
            'GCC runtime carries GPL plus GCC Runtime Library Exception; preserve both notices.',
            'mingw-w64 v13 notices are reference material; exact bundled winpthreads build provenance still needs confirmation.',
        ], materials_collected=True, unresolved=[
            'FFmpeg/x264/x265 licensing classification and linked-distribution obligations require publisher review',
            'winpthreads exact source provenance'])
    (destination / 'materials.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    collect(args.work.resolve(), args.destination.resolve())
