"""Required incremental-package stage and upload-set gate for edition releases."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dlss5tool import delta_update as delta, updater
from scripts import build_file_update

DEFAULT_POLICY = ROOT / 'packaging/update-policy.json'


def plan_updates(target, *, initial=False, overrides=(), policy_path=DEFAULT_POLICY):
    """Run before copying/compressing release inputs. Never guess a baseline."""
    delta.version(target)
    policy = delta.read_json(policy_path)
    if not isinstance(policy, dict) or policy.get('schema') != 1:
        raise delta.DeltaError('Invalid packaging/update-policy.json schema')
    first = delta.version(policy.get('first_updater_version'))
    if initial:
        if target != first or overrides:
            raise delta.DeltaError('--initial-update-baseline is only allowed for the first updater version, without overrides')
        return {'mode': 'initial_baseline', 'target': target, 'first': first, 'baselines': []}
    if not updater.is_newer_version(target, first):
        raise delta.DeltaError('First updater release requires explicit --initial-update-baseline; later releases require baselines')
    configured = policy.get('baselines')
    if not isinstance(configured, list) or not configured:
        raise delta.DeltaError('Update policy must contain at least one required baseline')
    required = {}
    for item in configured:
        if not isinstance(item, dict):
            raise delta.DeltaError('Invalid baseline policy entry')
        old = delta.version(item.get('version'))
        if old in required or not updater.is_newer_version(target, old) or updater.compare_versions(old, first) < 0:
            raise delta.DeltaError('Baseline must be unique, updater-capable and older than target')
        path = item.get('packages')
        if not isinstance(path, str) or not path:
            raise delta.DeltaError('Baseline needs an explicit package directory')
        required[old] = (ROOT / path).absolute()
    for override in overrides:
        # CLI relocation replaces a configured version, never drops another one.
        path = Path(override).absolute()
        report = delta.read_json(delta.safe_path(path, 'package-report.json'))
        old = report.get('version')
        if old not in required:
            raise delta.DeltaError('Override version is not declared in update-policy.json')
        required[old] = path
    baselines = []
    for old, directory in required.items():
        report = delta.read_json(delta.safe_path(directory, 'package-report.json'))
        if report.get('version') != old or report.get('full_equals_lite_plus_addon') is not True:
            raise delta.DeltaError(f'Baseline report mismatch: {old}')
        folders, inventories = {}, {}
        for edition in ('lite', 'full'):
            expected = delta.read_json(delta.safe_path(directory, f'verification/{edition}-files.json'))
            folder = delta.safe_path(directory, f'DLSS5Tool-{old}-win64-{edition}')
            actual = build_file_update.inventory(folder, edition)
            if actual != expected:
                raise delta.DeltaError(f'Baseline files missing or modified: {old} {edition}')
            delta.validate_manifest({'schema': 1, 'from': old, 'to': target, 'edition': edition,
                                     'before': actual, 'after': actual})
            folders[edition], inventories[edition] = folder, actual
        baselines.append({'version': old, 'folders': folders, 'inventories': inventories})
    return {'mode': 'required', 'target': target, 'first': first, 'baselines': baselines}


def build_updates(plan, folders, inventories, output, assets):
    """Generate both editions for every required baseline; errors propagate."""
    for edition in ('lite', 'full'):
        files = inventories.get(edition, {})
        if 'DLSS5Tool.exe' not in files or delta.HELPER not in files:
            raise delta.DeltaError(f'{edition} target is missing the application or update helper')
        if edition == 'full' and 'mods/enhancement/guidance_worker.exe' not in files:
            raise delta.DeltaError('Full target is missing the enhancement worker')
        for name in files:
            delta.valid_path(name, edition)
    records = []
    expected_peak = 0
    for baseline in plan['baselines']:
        for edition in ('lite', 'full'):
            old = baseline['inventories'][edition]
            expected_peak += sum(record['size'] for name, record in inventories[edition].items()
                                 if old.get(name) != record)
    # Account for incompressible payload overhead, reports, and existing full ZIPs.
    expected_peak = int(expected_peak * 1.02) + len(plan['baselines']) * 2 * delta.MAX_MANIFEST
    if plan['baselines'] and shutil.disk_usage(output).free < expected_peak + 15 * 1024**3:
        raise delta.DeltaError('Insufficient space for required incremental payloads + 15 GiB margin')
    for baseline in plan['baselines']:
        for edition in ('lite', 'full'):
            old = baseline['version']
            folder = output / f'update-{old}-{edition}'
            artifact = build_file_update.build(
                baseline['folders'][edition], folders[edition], folder,
                old, plan['target'], edition,
                expected_before=baseline['inventories'][edition], expected_after=inventories[edition],
            )
            record = delta.read_json(folder / 'update-report.json')
            target = assets / artifact.name
            if target.exists():
                raise delta.DeltaError(f'Duplicate update asset: {target.name}')
            # Same volume move: no second multi-GB copy of the payload.
            artifact.rename(target)
            record['name'] = record.pop('asset')
            records.append(record)
    return {'mode': plan['mode'], 'target': plan['target'], 'first_updater_version': plan['first'],
            'required_baselines': [b['version'] for b in plan['baselines']], 'assets': records}


def verify_upload_updates(assets, report=None, *, policy_path=DEFAULT_POLICY):
    """Read-only final gate: catch missing/corrupt/wrong-edition upload payloads."""
    assets = Path(assets)
    report = report or delta.read_json(assets / 'package-report.json')
    updates = report.get('incremental_updates')
    if not isinstance(updates, dict) or updates.get('target') != report.get('version'):
        raise delta.DeltaError('Missing incremental workflow report; upload set is incomplete')
    target = delta.version(report.get('version'))
    first = delta.version(updates.get('first_updater_version'))
    policy = delta.read_json(policy_path)
    if policy.get('schema') != 1 or policy.get('first_updater_version') != first:
        raise delta.DeltaError('Upload report does not match the committed update policy')
    required = updates.get('required_baselines')
    if not isinstance(required, list) or len(set(required)) != len(required):
        raise delta.DeltaError('Invalid required baseline list')
    for old in required:
        delta.version(old)
        if not updater.is_newer_version(target, old) or updater.compare_versions(old, first) < 0:
            raise delta.DeltaError('Invalid baseline version in report')
    if updates.get('mode') == 'initial_baseline':
        if target != first or required:
            raise delta.DeltaError('Invalid initial baseline exemption')
    elif updates.get('mode') != 'required' or not required:
        raise delta.DeltaError('Later releases cannot skip incremental payloads')
    else:
        configured = policy.get('baselines')
        if not isinstance(configured, list):
            raise delta.DeltaError('Invalid baseline policy')
        # The target itself may be registered after validation for the next release.
        expected_sources = [item['version'] for item in configured if item['version'] != target]
        if sorted(expected_sources) != sorted(required):
            raise delta.DeltaError('Required baseline coverage differs from update policy')
    expected = {delta.asset_name(old, target, e): (old, e) for old in required for e in ('lite', 'full')}
    records = updates.get('assets')
    if not isinstance(records, list) or len(records) != len(expected):
        raise delta.DeltaError('Missing or duplicate incremental payload records')
    if {r['name'] for r in records} != set(expected):
        raise delta.DeltaError('Incremental payload record names do not match required pairs')
    if {p.name for p in assets.glob('*.dlssupdate')} != set(expected):
        raise delta.DeltaError('Missing or unexpected incremental payloads in github-assets')
    checks = {}
    for line in (assets / 'SHA256SUMS.txt').read_text(encoding='utf-8').splitlines():
        digest, name = line.split(maxsplit=1)
        checks[name.strip()] = digest
    for record in records:
        name = record['name']
        old, edition = expected[name]
        path = delta.safe_path(assets, name)
        actual = delta.file_record(path)
        if (record.get('from') != old or record.get('to') != target or record.get('edition') != edition
                or actual != {k: record.get(k) for k in ('size', 'sha256')}
                or checks.get(name) != actual['sha256'] or actual['size'] >= 2 * 1024**3):
            raise delta.DeltaError(f'Incremental payload digest/metadata mismatch: {name}')
        with zipfile.ZipFile(path) as bundle:
            if bundle.getinfo('manifest.json').file_size > delta.MAX_MANIFEST:
                raise delta.DeltaError('Oversized update manifest')
            delta.validate_manifest(json.loads(bundle.read('manifest.json')), old, target, edition)
    return updates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check-upload', type=Path)
    mode.add_argument('--preflight-version', help='Read-only baseline validation before the main app build')
    parser.add_argument('--update-baseline', type=Path, action='append', default=[])
    parser.add_argument('--initial-update-baseline', action='store_true')
    args = parser.parse_args()
    if args.preflight_version:
        plan = plan_updates(args.preflight_version, initial=args.initial_update_baseline,
                            overrides=args.update_baseline)
        print(f"Baseline preflight passed: {plan['mode']}, {len(plan['baselines'])} required versions")
        return
    if args.update_baseline or args.initial_update_baseline:
        parser.error('Baseline options apply only to --preflight-version')
    result = verify_upload_updates(args.check_upload)
    print(f"Incremental upload gate passed: {result['mode']}, {len(result['assets'])} payloads")


if __name__ == '__main__':
    main()
