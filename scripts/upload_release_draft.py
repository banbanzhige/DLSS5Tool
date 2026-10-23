"""Upload verified release assets to a GitHub draft, preserving existing assets."""

import argparse
import concurrent.futures
import datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time


def plain_path(path):
    path = path.absolute()
    for item in (path, *path.parents):
        info = item.lstat()
        if item.is_symlink() or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f'Reparse point rejected: {item}')
    return path.resolve(strict=True)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def command(*arguments):
    result = subprocess.run(arguments, capture_output=True, text=True, encoding='utf-8',
                            env={**os.environ, 'GH_PROMPT_DISABLED': '1', 'GH_PAGER': 'cat'})
    if result.returncode:
        raise RuntimeError(f'{arguments[0]} failed: {result.stderr.strip()}')
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--target', required=True, help='Existing remote commit SHA; no source push is performed')
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--notes', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    assets, notes = plain_path(args.assets), plain_path(args.notes)
    report_parent = plain_path(args.report.parent)
    report_path = report_parent / args.report.name
    if report_path.exists():
        plain_path(report_path)
    package = json.loads((assets / 'package-report.json').read_text(encoding='utf-8-sig'))
    if package['version'] != args.tag:
        raise ValueError('Package version does not match the draft tag')
    expected = {}
    for details in package['editions'].values():
        if details['volumes']:
            for volume in details['volumes']:
                expected[volume['name']] = {'size': volume['size'], 'sha256': volume['sha256']}
        else:
            expected[details['github_name']] = {'size': details['size'], 'sha256': details['sha256']}
    for details in package['incremental_updates']['assets']:
        expected[details['name']] = {'size': details['size'], 'sha256': details['sha256']}
    for name in ('Join-ReleaseArchive.ps1', 'SHA256SUMS.txt', notes.name):
        path = plain_path(assets / name)
        expected[name] = {'size': path.stat().st_size, 'sha256': digest(path)}
    if (assets / notes.name).read_bytes() != notes.read_bytes():
        raise ValueError('Release notes in the upload directory differ from the source')
    for name, entry in expected.items():
        if Path(name).name != name or '/' in name or '\\' in name:
            raise ValueError(f'Invalid asset name: {name}')
        path = plain_path(assets / name)
        print(f'CHECK {name}', flush=True)
        if path.stat().st_size != entry['size'] or digest(path) != entry['sha256']:
            raise ValueError(f'Local asset mismatch: {name}')

    def api(endpoint):
        return json.loads(command('gh', 'api', endpoint))

    target = api(f'repos/{args.repo}/commits/{args.target}')['sha']
    releases = api(f'repos/{args.repo}/releases?per_page=100')
    matches = [release for release in releases if release['tag_name'] == args.tag]
    if len(matches) > 1:
        raise ValueError('Multiple releases have the requested tag')
    if matches and not matches[0]['draft']:
        raise ValueError('The requested release is already published')
    if not matches:
        command('gh', 'release', 'create', args.tag, '--repo', args.repo, '--draft',
                '--target', target, '--title', args.tag, '--notes-file', str(notes))
        matches = [release for release in api(f'repos/{args.repo}/releases?per_page=100')
                   if release['tag_name'] == args.tag]
    if len(matches) != 1:
        raise ValueError('Could not find the newly created draft')
    release_id = matches[0]['id']

    def draft():
        release = api(f'repos/{args.repo}/releases/{release_id}')
        if not release['draft'] or release['tag_name'] != args.tag:
            raise ValueError('Release is no longer the requested draft')
        return release

    def asset_matches(remote, entry):
        return (remote['state'] == 'uploaded' and remote['size'] == entry['size']
                and remote.get('digest') == 'sha256:' + entry['sha256'])

    existing = {entry['name']: entry for entry in draft()['assets']}
    for name in expected.keys() & existing.keys():
        if not asset_matches(existing[name], expected[name]):
            raise ValueError(f'Existing asset differs and was preserved: {name}')
    local_commit = command('git', 'rev-parse', 'HEAD').strip()
    report = {
        'tag': args.tag, 'repo': args.repo, 'release_id': release_id,
        'started_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'draft': True, 'published': False, 'target_commitish': matches[0]['target_commitish'],
        'local_source_commit': local_commit, 'source_sync_required': target != local_commit,
        'html_url': matches[0]['html_url'], 'status': 'uploading',
        'expected_assets': expected, 'completed': [], 'errors': [],
        'new_local_large_files': False, 'source_pushed': False,
    }

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def upload(name):
        entry = expected[name]
        for attempt in range(1, 4):
            remote = {item['name']: item for item in draft()['assets']}.get(name)
            if remote:
                if asset_matches(remote, entry):
                    return name
                raise ValueError(f'Existing asset differs and was preserved: {name}')
            print(f'UPLOAD {name} ({entry["size"]} bytes), attempt {attempt}', flush=True)
            try:
                command('gh', 'release', 'upload', args.tag, str(assets / name), '--repo', args.repo)
            except RuntimeError as error:
                print(f'RETRY {name}: {error}', flush=True)
                if attempt == 3:
                    raise
                time.sleep(attempt * 2)
                continue
            remote = {item['name']: item for item in draft()['assets']}.get(name)
            if not remote or not asset_matches(remote, entry):
                raise ValueError(f'Uploaded asset digest/size mismatch: {name}')
            return name
        raise RuntimeError(f'Upload attempts exhausted: {name}')

    save()
    print(f'DRAFT {report["html_url"]}; {len(expected)} assets, {sum(x["size"] for x in expected.values())} bytes', flush=True)
    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        pending = {pool.submit(upload, name): name for name in sorted(expected, key=lambda name: expected[name]['size'])}
        while pending:
            ready, _ = concurrent.futures.wait(pending, timeout=15, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in ready:
                name = pending.pop(future)
                try:
                    report['completed'].append(future.result())
                    print(f'VERIFIED {name}', flush=True)
                except Exception as error:
                    report['errors'].append({'asset': name, 'error': str(error)})
                    print(f'ERROR {name}: {error}', flush=True)
                save()
            print(f'PROGRESS {len(report["completed"])}/{len(expected)}, elapsed {int(time.monotonic()-started)}s', flush=True)
    release = draft()
    remote_assets = {item['name']: item for item in release['assets']}
    report['remote_assets'] = [{key: entry.get(key) for key in ('id', 'name', 'size', 'digest', 'state')}
                               for entry in release['assets']]
    report['all_assets_sha256_and_size_verified'] = all(
        name in remote_assets and asset_matches(remote_assets[name], entry) for name, entry in expected.items())
    report['notes_match'] = release['body'].replace('\r\n', '\n').rstrip('\n') == notes.read_text(encoding='utf-8-sig').replace('\r\n', '\n').rstrip('\n')
    report['published_at'] = release['published_at']
    report['status'] = 'complete' if report['all_assets_sha256_and_size_verified'] and report['notes_match'] and not report['errors'] else 'incomplete'
    report['completed_at'] = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
    save()
    if report['status'] != 'complete':
        raise RuntimeError(f'Draft upload incomplete; see {report_path}')
    print(f'DONE {report["html_url"]}; still draft; all {len(expected)} asset digests verified', flush=True)


if __name__ == '__main__':
    main()
