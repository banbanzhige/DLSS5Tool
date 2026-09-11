"""Build a version-pair file payload from two clean, verified release folders.

The .dlssupdate suffix deliberately keeps this ZIP out of legacy ZIP selectors.
No upload, dependency copy or environment build is performed.
"""
import argparse
import json
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dlss5tool import delta_update as delta


def inventory(root, edition):
    root = Path(root).absolute()
    files = {}
    for path in sorted(root.rglob('*')):
        delta.safe_path(root, path.relative_to(root))
        if path.is_file():
            name = path.relative_to(root).as_posix()
            delta.valid_path(name, edition)
            files[name] = delta.file_record(path)
    return files


def build(before, after, output, old, new, edition):
    before, after, output = (Path(p).absolute() for p in (before, after, output))
    if (output.exists() or output.is_relative_to(before) or output.is_relative_to(after)
            or before == after):
        raise delta.DeltaError('Use distinct release inputs and a new output directory outside them')
    manifest = delta.validate_manifest({
        'schema': 1, 'from': old, 'to': new, 'edition': edition,
        'before': inventory(before, edition), 'after': inventory(after, edition),
    })
    changed = [p for p in delta.changes(manifest) if p in manifest['after']]
    payload_bytes = sum(manifest['after'][p]['size'] for p in changed)
    # Conservative bound for ZIP + metadata, no staging copy is made.
    peak = int(payload_bytes * 1.02) + delta.MAX_MANIFEST
    free = shutil.disk_usage(output.parent).free
    print(json.dumps({'estimated_peak_bytes': peak, 'free_bytes': free, 'changed_files': len(changed)}))
    if free < peak + 15 * 1024**3:
        raise delta.DeltaError('Build requires estimated peak + 15 GiB free space')
    if peak > 5 * 1024**3:
        raise delta.DeltaError('Estimated output exceeds 5 GiB; use a full release after reviewing space budget')
    output.mkdir()
    destination = output / delta.asset_name(old, new, edition)
    raw = json.dumps(manifest, ensure_ascii=True, sort_keys=True).encode('utf-8')
    if len(raw) > delta.MAX_MANIFEST:
        raise delta.DeltaError('Manifest exceeds client size limit')
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        bundle.writestr('manifest.json', raw)
        for name in changed:
            bundle.write(delta.safe_path(after, name), 'payload/' + name)
    # Validate actual bytes, not just ZIP CRC. Inputs must remain immutable.
    with zipfile.ZipFile(destination) as bundle:
        for name in changed:
            import hashlib
            with bundle.open('payload/' + name) as source:
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
            if digest != manifest['after'][name]['sha256']:
                raise delta.DeltaError(f'Release input changed during build: {name}')
        if bundle.testzip() is not None:
            raise delta.DeltaError('Payload CRC check failed')
    record = delta.file_record(destination)
    if record['size'] >= 2 * 1024**3:
        raise delta.DeltaError('Payload exceeds supported release asset size; publish full-package fallback only')
    delta.write_json(output / 'update-report.json', {
        'from': old, 'to': new, 'edition': edition, 'asset': destination.name, **record,
        'changed_files': len(changed), 'removed_files': len(manifest['before'].keys() - manifest['after'].keys()),
        'payload_bytes': payload_bytes, 'published': False,
    })
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before', required=True, type=Path)
    parser.add_argument('--after', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--from-version', required=True)
    parser.add_argument('--to-version', required=True)
    parser.add_argument('--edition', required=True, choices=('lite', 'full'))
    args = parser.parse_args()
    print(build(args.before, args.after, args.output, args.from_version, args.to_version, args.edition))


if __name__ == '__main__':
    main()
