"""Verify actual delta bytes against both release inventories without copying runtimes."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dlss5tool import delta_update as delta
from scripts.build_file_update import inventory


def verify(before, after, asset, edition):
    with zipfile.ZipFile(asset) as bundle:
        manifest = json.loads(bundle.read('manifest.json'))
        delta.validate_manifest(manifest, edition=edition)
        old, new = inventory(before, edition), inventory(after, edition)
        if old != manifest['before'] or new != manifest['after']:
            raise ValueError('Release files differ from update endpoints')
        changed = {name for name, record in new.items() if old.get(name) != record}
        expected = {'manifest.json'} | {'payload/' + name for name in changed}
        if len(bundle.namelist()) != len(expected) or set(bundle.namelist()) != expected:
            raise ValueError('Unexpected or missing update payload')
        reconstructed = {name: record for name, record in old.items() if name in new and name not in changed}
        for name in changed:
            info = bundle.getinfo('payload/' + name)
            with bundle.open(info) as source:
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
            reconstructed[name] = {'size': info.file_size, 'sha256': digest}
        if reconstructed != new:
            raise ValueError('Streamed update does not reconstruct target')
        return {'edition': edition, 'from': manifest['from'], 'to': manifest['to'],
                'asset_sha256': delta.file_record(asset)['sha256'],
                'changed_files': len(changed), 'overlay_verified': True,
                'method': 'streamed actual payload plus unchanged baseline bytes; no install mutation'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before', type=Path, required=True)
    parser.add_argument('--after', type=Path, required=True)
    parser.add_argument('--asset', type=Path, required=True)
    parser.add_argument('--edition', choices=('lite', 'full'), required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists():
        parser.error('Use a new report path')
    result = verify(args.before, args.after, args.asset, args.edition)
    with args.report.open('x', encoding='utf-8') as out:
        json.dump(result, out, indent=2)
    print(json.dumps(result))
