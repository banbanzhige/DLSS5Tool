"""Verify real update payload bytes reconstruct the target without copying models."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dlss5tool import delta_update as delta
from scripts.release_updates import verify_upload_updates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packages', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    if args.report.exists():
        raise ValueError('Report already exists')
    assets = args.packages/'github-assets'
    updates = verify_upload_updates(assets)
    policy = json.loads((ROOT/'packaging/update-policy.json').read_text(encoding='utf-8'))
    records = []
    for record in updates['assets']:
        old, new, edition = record['from'], record['to'], record['edition']
        baseline = next(p for p in policy['baselines'] if p['version'] == old)
        old_dir = ROOT/baseline['packages']/f'DLSS5Tool-{old}-win64-{edition}'
        target_inventory = json.loads((args.packages/f'verification/{edition}-files.json').read_text(encoding='utf-8'))
        with zipfile.ZipFile(assets/record['name']) as bundle:
            manifest = delta.validate_manifest(json.loads(bundle.read('manifest.json')), old, new, edition)
            if manifest['after'] != target_inventory:
                raise ValueError('Update target differs from packaged edition')
            changed = {n for n, info in manifest['after'].items() if manifest['before'].get(n) != info}
            if set(bundle.namelist()) != {'manifest.json'} | {'payload/'+n for n in changed}:
                raise ValueError('Unexpected/missing update entries')
            for name, expected in manifest['after'].items():
                with (bundle.open('payload/'+name) if name in changed else (old_dir/name).open('rb')) as stream:
                    size, digest = 0, hashlib.sha256()
                    while data := stream.read(8*1024**2):
                        size += len(data)
                        digest.update(data)
                if {'size': size, 'sha256': digest.hexdigest()} != expected:
                    raise ValueError('Reconstructed bytes mismatch: '+name)
            records.append({'from': old, 'to': new, 'edition': edition,
                            'changed_files': len(changed), 'target_files': len(manifest['after']),
                            'streamed_overlay_exact': True})
            print(records[-1], flush=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({'updates': records, 'actual_install_performed': False}, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
