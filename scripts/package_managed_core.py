"""Experimental core-update assets from an explicitly managed frozen build.

Supplements the required package_editions.py workflow. Never installs, migrates,
uploads or overwrites a release. Full-channel components stay outside current/.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dlss5tool import delta_update as delta
from dlss5tool.managed_update import SDK_VERSION, manifest_name, package_inventory, validate_target_manifest
from dlss5tool.mod_paths import GUIDANCE_PROTOCOL
from dlss5tool.portable_layout import LAYOUT_FILE, LayoutError, PortableLayout, validate_layout_config


def write_manifest(package, version, edition, package_id, destination):
    files = package_inventory(package)
    if 'lib/app/DLSS5Tool.exe' not in files:
        raise ValueError('Managed core is missing DLSS5Tool.exe')
    if any(name.startswith('lib/app/mods/') for name in files):
        raise ValueError('Models and user components must remain outside current/')
    result = {'schema': 1, 'provider': 'velopack', 'sdk_version': SDK_VERSION,
              'version': delta.version(version), 'edition': edition, 'channel': 'win-' + edition,
              'package_id': package_id, 'guidance_protocol': GUIDANCE_PROTOCOL,
              'full_asset': package.name, 'full_record': delta.file_record(package), 'files': files}
    from types import SimpleNamespace
    validate_target_manifest(result, SimpleNamespace(tag=version),
        PortableLayout(destination.parent, destination.parent / 'current', package_id, edition, 'win-' + edition))
    raw = json.dumps(result, sort_keys=True).encode('utf-8')
    if len(raw) > delta.MAX_MANIFEST:
        raise ValueError('Managed target manifest exceeds client limit')
    delta.safe_path(destination.parent, destination.name)
    with destination.open('xb') as target:
        target.write(raw)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', required=True, type=Path)
    parser.add_argument('--vpk', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--version', required=True)
    parser.add_argument('--experimental-task', action='store_true')
    args = parser.parse_args()
    delta.version(args.version)
    base, output, tool = (path.absolute() for path in (args.base, args.output, args.vpk))
    for path in (base, output, tool):
        delta.safe_path(path.parent, path.name)
    if output.exists() or output.is_relative_to(base):
        parser.error('Use a new output directory outside the input build')
    if args.experimental_task:
        if not output.is_relative_to(ROOT / 'tmp') or not any(
                (p / 'TASK.md').is_file() for p in output.parents if p.is_relative_to(ROOT / 'tmp')):
            parser.error('Use a registered tmp task')
    elif not output.is_relative_to(Path('E:/dist/DLSS5Tool') / args.version):
        parser.error('Deliverables must stay under E:/dist/DLSS5Tool/<version>/')
    try:
        config = validate_layout_config(delta.read_json(delta.safe_path(base, '_internal/' + LAYOUT_FILE)))
    except (LayoutError, OSError, ValueError):
        parser.error('Input needs explicit managed portable layout metadata')
    for path in base.rglob('*'):
        delta.safe_path(base, path.relative_to(base))
        if path.is_file():
            delta.valid_path(path.relative_to(base).as_posix(), 'lite')
    if (base / 'mods').exists():
        parser.error('Build core without mods; components are distributed separately')
    required = sum(p.stat().st_size for p in base.rglob('*') if p.is_file()) * 4
    if not output.parent.is_dir() or shutil.disk_usage(output.parent).free < required + 15 * 1024**3:
        parser.error('Need an existing parent and package peak + 15 GiB free space')
    output.mkdir()
    command = ['dotnet', str(tool), 'pack', '--packId', config['package_id'], '--packVersion', args.version.lstrip('v'),
               '--packDir', str(base), '--mainExe', 'DLSS5Tool.exe', '--outputDir', str(output),
               '--channel', config['channel'], '--runtime', 'win-x64', '--noInst', '--icon', str(ROOT / 'assets/app.ico')]
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace',
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=900)
    (output / 'pack.log').write_text(result.stdout + '\n' + result.stderr, encoding='utf-8')
    if result.returncode:
        raise RuntimeError('Managed packaging failed; see pack.log')
    full, = output.glob('*-full.nupkg')
    manifest = write_manifest(full, args.version, config['edition'], config['package_id'],
                              output / manifest_name(args.version, config['edition']))
    delta.write_json(output / 'managed-report.json', {
        'version': args.version, 'edition': config['edition'], 'experimental': True,
        'core_only': True, 'existing_release_workflow_replaced': False, 'uploaded': False,
        'manifest': manifest_name(args.version, config['edition']), 'full': manifest['full_record'],
        'sdk_version': SDK_VERSION,
        'limitations': ['External component provisioning and legacy bridge need independent acceptance',
                        'Production signed delivery has not been certified']})
    print(json.dumps({'core_package': str(full), 'experimental': True}))


if __name__ == '__main__':
    main()
