"""Verify old official components can be reused; package only changed notices.

Run after package_editions.py. This is not a delta and never patches binaries or
alters the supported incremental-update versions in update-policy.json.
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
from scripts.build_file_update import inventory

NOTICE_PATHS = {
    'mods/README.md', 'mods/ADDON-INSTALL.txt',
    'mods/enhancement/README.md', 'mods/enhancement/DISTRIBUTION-REVIEW.txt',
}


def reuse_notices(old, lite, full):
    """Only these two old component trees are copied by the user."""
    reused = {n: r for n, r in old.items()
              if n.startswith(('mods/enhancement/', 'mods/models/'))}
    if 'mods/enhancement/guidance_worker.exe' not in reused:
        raise ValueError('Official inference component is missing')
    combined = {**lite, **reused}
    changed = {n for n in combined.keys() | full.keys() if combined.get(n) != full.get(n)}
    if changed - NOTICE_PATHS or any(n not in full for n in changed):
        raise ValueError('Old components are not reusable: ' + ', '.join(sorted(changed - NOTICE_PATHS)))
    if {**combined, **{n: full[n] for n in changed}} != full:
        raise ValueError('Reused component overlay is not exact')
    return changed


def verified_inventory(packages, version, edition):
    expected = delta.read_json(packages / f'verification/{edition}-files.json')
    actual = inventory(packages / f'DLSS5Tool-{version}-win64-{edition}', edition)
    if actual != expected:
        raise ValueError(f'Baseline missing or modified: {version} {edition}')
    return actual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packages', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, action='append', required=True)
    args = parser.parse_args()
    packages = args.packages.resolve()
    report = delta.read_json(packages / 'package-report.json')
    version = delta.version(report['version'])
    if version != 'v2.3.1':
        raise ValueError('Revalidate component reuse instructions for each new release')
    lite = verified_inventory(packages, version, 'lite')
    full = verified_inventory(packages, version, 'full')
    notices, supported = set(), []
    for baseline in args.baseline:
        old_version = delta.read_json(baseline / 'package-report.json')['version']
        if old_version not in ('v2.2.2', 'v2.3.0') or old_version in supported:
            raise ValueError('Unexpected or duplicate component source')
        old = verified_inventory(baseline, old_version, 'full')
        notices.update(reuse_notices(old, lite, full))
        supported.append(old_version)
    if set(supported) != {'v2.2.2', 'v2.3.0'}:
        raise ValueError('Both documented source editions must be verified')
    assets = packages / 'github-assets'
    # -addon blocks selection by older portable updaters if the main ZIP is absent.
    name = f'DLSS5Tool-{version}-addon-component-reuse.zip'
    archive = assets / name
    if archive.exists():
        raise ValueError('Reuse archive already exists')
    target = packages / f'DLSS5Tool-{version}-win64-full'
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as bundle:
        for relative in sorted(notices):
            bundle.write(delta.safe_path(target, relative), relative)
    with zipfile.ZipFile(archive) as bundle:
        if set(bundle.namelist()) != notices or bundle.testzip() is not None:
            raise ValueError('Reuse archive verification failed')
        import hashlib
        for relative in notices:
            data = bundle.read(relative)
            if {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()} != full[relative]:
                raise ValueError('Reuse notice hash mismatch')
    guide = ROOT / f'docs/release/UPGRADE_{version}.md'
    for directory in (packages, assets):
        shutil.copy2(guide, directory / guide.name)
    record = {'target': version, 'source_versions': sorted(supported),
              'asset': name, **delta.file_record(archive), 'notice_paths': sorted(notices),
              'lite_plus_old_components_plus_notices_equals_full': True,
              'binaries_or_models_in_reuse_archive': False}
    delta.write_json(packages / 'verification/component-reuse.json', record)
    for directory, prefix in ((assets, ''), (packages, 'github-assets/')):
        with (directory / 'SHA256SUMS.txt').open('a', encoding='utf-8') as output:
            output.write(f"{record['sha256']}  {prefix}{name}\n")
    with (assets / 'README-UPLOAD.txt').open('a', encoding='utf-8') as output:
        output.write(f'Low-download migration: {name} and {guide.name}. Notices only; NOT a delta or full add-on.\n')
    print(json.dumps(record, indent=2))


if __name__ == '__main__':
    main()
