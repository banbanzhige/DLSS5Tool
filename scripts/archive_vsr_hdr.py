"""Verify the installed S4a host and archive small, reproducible HDR evidence."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    from scripts.build_vsr_cuda import safe, digest
    from dlss5tool.resident_sr_runtime import load_components

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True, type=Path)
    parser.add_argument('--destination', required=True, type=Path)
    args = parser.parse_args()
    task = safe(args.task, ROOT / 'tmp')
    destination = safe(args.destination, ROOT / 'docs/development')
    require(task != ROOT / 'tmp' and (task / 'TASK.md').is_file(), 'Registered task required')
    require(destination != ROOT / 'docs/development' and not destination.exists(), 'Fresh archive required')
    require(shutil.disk_usage(task).free >= 15 * 1024**3 + 16 * 1024**2, 'Disk reserve required')

    def read(name):
        return json.loads(safe(task / (name + '.json'), task).read_text(encoding='utf-8-sig'))

    plan = read('install-plan')
    proof_path = safe(plan['proof'], task)
    require(digest(proof_path) == plan['proof_sha256'], 'Promotion proof changed')
    proof = json.loads(proof_path.read_text(encoding='utf-8'))
    require(proof['status'] == 'verified', 'Promotion was not accepted')
    for relative, checksum in proof['source_manifest'].items():
        require(digest(safe(ROOT / relative, ROOT)) == checksum, 'Source changed: ' + relative)

    host = safe(ROOT / 'runtime/vsr_host.dll', ROOT / 'runtime')
    provider = safe(ROOT / 'runtime/nvngx_vsr.dll', ROOT / 'runtime')
    require(digest(host) == plan['candidate_sha256'] == proof['candidate_host_sha256'], 'Installed host mismatch')
    require(digest(provider) == plan['provider_sha256'] == proof['provider_sha256'], 'Provider changed')
    rollback_paths = [safe(task / relative, task) for relative in
                      ('rollback/vsr_host.dll', 'install-staging/pre-replace-vsr_host.dll')]
    for path in rollback_paths:
        require(digest(path) == proof['previous_host_sha256'], 'Rollback mismatch')

    smoke = read('installed-hdr4k-verified')
    process = read('installed-hdr4k-verified-process')
    require(smoke['status'] == 'complete' and smoke['frames'] == 8, 'Installed smoke incomplete')
    require(smoke['actual_adapter']['hdr_conversion'] == 'gpu-rgb10a2', 'Installed GPU conversion inactive')
    require(smoke['frame_hashes'] == read('cpu-hdr4k')['frame_hashes'][:8], 'Installed pixels changed')
    require(process['returncode'] == 0 and not process['worker_still_alive'], 'Installed worker did not retire')
    require(process['pool_after_retirement']['reserved_processing_bytes'] == 0, 'Reserved processing memory remains')
    require(load_components() is not None, 'S3 resident component pairing changed')

    identities = set()
    for case in ET.parse(safe(task / 'targeted-core.xml', task)).getroot().iter('testcase'):
        require(all(case.find(tag) is None for tag in ('failure', 'error', 'skipped')), 'Targeted test did not pass')
        identities.add((case.get('classname'), case.get('name')))
    require(len(identities) == proof['tests'] == 141, 'Targeted test identity mismatch')

    final_path = safe(task / 'installation-verification.json', task)
    final = dict(status='installed-smoke-verified', proof_sha256=digest(proof_path),
                 host_sha256=digest(host), provider_sha256=digest(provider), smoke_frames=8,
                 pixels_equal=True, resident_sdr_pairing_valid=True,
                 rollback_files=[dict(path=str(p), sha256=digest(p)) for p in rollback_paths],
                 targeted_tests=len(identities), test_identities=sorted(identities),
                 smoke_report='installed-hdr4k-verified.json',
                 initial_install_note='install-attempt-1.json failed with an empty backup path; '
                                    'installed-hdr4k.json still used the old CPU host. '
                                    'Neither is positive installation evidence.',
                 new_s4_source_committed=False, packaged=False, published=False)
    with final_path.open('x', encoding='utf-8') as output:
        json.dump(final, output, indent=2)

    reports = []
    # Inspect every child before walking into it, including Windows junctions.
    for current, directories, files in os.walk(task, topdown=True, followlinks=False):
        for name in directories + files:
            safe(Path(current) / name, task)
        directories[:] = [name for name in directories if not name.startswith('pytest-')]
        for name in sorted(files):
            path = Path(current) / name
            if path.suffix.lower() in ('.json', '.jsonl', '.csv', '.xml', '.log', '.md', '.h'):
                require(path.stat().st_size <= 8 * 1024**2, 'Unexpected large report: ' + str(path))
                reports.append(path)
    for name in ('analysis-oracle.py', 'analysis-22ee79c2368b.py'):
        reports.append(safe(task / name, task))
    require(sum(path.stat().st_size for path in reports) < 16 * 1024**2, 'Archive exceeds budget')

    def git(*arguments):
        return subprocess.run(['git', *arguments], cwd=ROOT, check=True, capture_output=True, text=True).stdout

    changed = git('diff', '--name-only').splitlines()
    added = git('ls-files', '--others', '--exclude-standard').splitlines()
    sources = sorted({name for name in changed + added
                      if name.startswith(('dlss5tool/', 'native/', 'scripts/', 'tests/'))
                      and Path(name).suffix.lower() in ('.py', '.cpp', '.h', '.ps1', '.bat')})
    for relative in sources:
        safe(ROOT / relative, ROOT)
    destination.mkdir(parents=True)
    records = []

    def copy(source, relative):
        source = safe(source, ROOT)
        target = safe(destination / relative, destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open('rb') as incoming, target.open('xb') as outgoing:
            outgoing.write(incoming.read())
        checksum = digest(source)
        require(checksum == digest(target), 'Archive copy mismatch')
        records.append(dict(path=target.relative_to(destination).as_posix(),
                            bytes=target.stat().st_size, sha256=checksum))

    for source in sorted(reports):
        copy(source, source.relative_to(task))
    for relative in sources:
        copy(ROOT / relative, 'source/' + relative)
    for name in ('SR_HDR_IMPLEMENTATION_20261026.md', 'SR_UPGRADE_ROADMAP_20261024.md',
                 'SR_UPGRADE_CHECKLIST_20261024.md', 'SR_UPGRADE_RECONCILIATION_20261026.md'):
        copy(ROOT / 'docs/development' / name, 'notes/' + name)

    state = dict(base_commit=git('rev-parse', 'HEAD').strip(), source_files=sources,
                 source_sha256={name: digest(ROOT / name) for name in sources},
                 installed_host_sha256=digest(host), tests=len(identities),
                 source_s4_committed=False, new_development_docs_ignored=True,
                 post_install_smoke_verified=True, packaged=False, published=False)
    state_path = destination / 'source-state.json'
    state_path.write_text(json.dumps(state, indent=2), encoding='utf-8')
    records.append(dict(path='source-state.json', bytes=state_path.stat().st_size, sha256=digest(state_path)))
    manifest = dict(schema=1, files=records, total_bytes=sum(row['bytes'] for row in records))
    (destination / 'archive-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    for row in records:
        require(digest(safe(destination / row['path'], destination)) == row['sha256'], 'Archive verification failed')
    print(json.dumps(dict(status=final['status'], destination=str(destination), files=len(records),
                          bytes=manifest['total_bytes'], tests=len(identities))))


if __name__ == '__main__':
    main()
