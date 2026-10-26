"""Archive verified small S2 evidence; leave native assets and media untouched."""
import argparse
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.build_vsr_cuda import safe, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--archive', type=Path, required=True)
    args = parser.parse_args()
    task = safe(args.task, ROOT/'tmp')
    archive = safe(args.archive, ROOT/'docs/development')
    summary = json.loads((task/'verification-summary.json').read_text(encoding='utf-8'))
    if summary.get('status') != 'verified' or 'installed' not in summary:
        raise ValueError('Final installed-component evidence required')
    for name, expected in summary['source_manifest'].items():
        if digest(ROOT/name) != expected:
            raise ValueError('Verified source changed: '+name)
    if shutil.disk_usage(task).free < 15*1024**3:
        raise ValueError('Keep 15 GiB disk safety margin')
    files = []
    for source in task.rglob('*'):
        if not source.is_file():
            continue
        relative = source.relative_to(task)
        root_report = len(relative.parts) == 1 and source.suffix in ('.json', '.xml', '.log', '.md')
        native_log = relative.parts[0] == 'native-logs'
        test_log = relative.parts[0].startswith('pytest-') and source.suffix in ('.json', '.xml', '.log')
        build_record = relative.parts[0] in ('build', 'd3d-build') and source.suffix in ('.bat', '.def', '.exports')
        if root_report or native_log or test_log or build_record:
            files.append(safe(source, task))
    archive.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for source in files:
        relative = source.relative_to(task)
        target = safe(archive/relative, archive)
        target.parent.mkdir(parents=True, exist_ok=True)
        sha256 = digest(source)
        if target.exists() and digest(target) != sha256:
            raise ValueError('Existing archive differs: '+str(target))
        if not target.exists():
            shutil.copy2(source, target)
        if digest(target) != sha256:
            raise ValueError('Archive copy mismatch: '+str(target))
        manifest[relative.as_posix()] = dict(sha256=sha256, bytes=target.stat().st_size)
    (archive/'archive-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps(dict(status='archived', files=len(files),
        bytes=sum(item['bytes'] for item in manifest.values()), path=str(archive))))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
