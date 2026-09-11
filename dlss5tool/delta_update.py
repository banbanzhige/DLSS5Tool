"""Version-pair file updates. No third-party dependencies; shared by the helper.

Only explicitly allowlisted release files are owned by the updater. A baseline
hash mismatch fails closed, including unchanged dependencies. The updater never
executes anything from a downloaded archive.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import zipfile

from dlss5tool import updater

TRANSACTION = '.dlss5-update'
HELPER = 'DLSS5Update.exe'
MAX_MANIFEST = 4 * 1024 * 1024
MAX_FILES = 20000
MAX_UNPACKED = 12 * 1024**3
SPACE_MARGIN = 256 * 1024**2
ROOT_FILES = {
    'dlss5tool.exe', 'dlss5update.exe', 'readme.md', 'readme.en.md',
    'changelog.md', 'license', 'third_party_notices.md',
    'nvidia_rtx_video_sdk_license.pdf', 'distribution-review.txt',
}


class DeltaError(updater.UpdateError):
    pass


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', value):
        raise DeltaError('Invalid update version')
    return value


def asset_name(old, new, edition):
    version(old)
    version(new)
    if edition not in ('lite', 'full'):
        raise DeltaError('Invalid update edition')
    return f'DLSS5Tool-{old}-to-{new}-win64-{edition}.dlssupdate'


def installed_edition(root, settings=None):
    """External/custom component locations require manual compatibility review."""
    root = Path(root).absolute()
    settings = settings or {}
    configured = Path(str(settings.get('mods_directory') or 'mods'))
    configured = configured if configured.is_absolute() else root / configured
    if configured.resolve() != (root / 'mods').resolve():
        raise DeltaError('Custom mods directory: use a matching full package and review component compatibility')
    return 'full' if safe_path(root, 'mods/enhancement').exists() else 'lite'


def select_asset(release, current, edition):
    expected = asset_name(current, release.tag, edition)
    matches = [a for a in release.assets if a.name == expected]
    if len(matches) != 1:
        return None
    asset = matches[0]
    # A required publisher-provided GitHub digest binds the entire archive,
    # including its manifest. A self-declared hash inside the ZIP is not trust.
    if not updater._expected_sha256(asset.digest) or not 0 < asset.size < 2 * 1024**3:
        return None
    from urllib.parse import urlsplit, unquote
    parsed = urlsplit(asset.download_url)
    expected_path = f'/{updater.GITHUB_REPOSITORY}/releases/download/{release.tag}/{expected}'
    if (parsed.scheme != 'https' or parsed.netloc != 'github.com'
            or unquote(parsed.path) != expected_path or parsed.query or parsed.fragment):
        return None
    return asset


def valid_path(name, edition='full'):
    if not isinstance(name, str) or len(name) > 220 or not name:
        raise DeltaError('Invalid release path')
    parts = name.split('/')
    for part in parts:
        if (not part or part in ('.', '..') or part[-1] in ' .'
                or re.search(r'[<>:"\\|?*\x00-\x1f]', part)
                or part.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
                    *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))}):
            raise DeltaError(f'Unsafe release path: {name}')
    lower = name.lower()
    allowed = (lower in ROOT_FILES or lower.startswith(('_internal/', 'licenses/'))
               or lower == 'mods/readme.md')
    if edition == 'full':
        allowed |= (lower.startswith(('mods/enhancement/', 'mods/models/'))
                    or lower == 'mods/addon-install.txt')
    if not allowed:
        raise DeltaError(f'Not an updater-owned path: {name}')
    return name


def safe_path(root, relative):
    """Internal paths too: reject links/junctions in root and every ancestor."""
    root = Path(root).absolute()
    path = root / relative
    if (not path.is_relative_to(root) or '..' in Path(relative).parts or Path(relative).is_absolute()
            or ':' in str(relative)):
        raise DeltaError('Path escapes update root')
    for candidate in (*reversed(path.parents), path):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise DeltaError(f'Link/reparse point is not supported: {candidate}')
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise DeltaError(f'Hard-linked file is not supported: {candidate}')
        if candidate != path and not stat.S_ISDIR(info.st_mode):
            raise DeltaError(f'Parent is not a directory: {candidate}')
    return path


def file_record(path):
    with Path(path).open('rb') as source:
        return {'size': os.fstat(source.fileno()).st_size,
                'sha256': hashlib.file_digest(source, 'sha256').hexdigest()}


def read_json(path):
    with Path(path).open('rb') as source:
        raw = source.read(MAX_MANIFEST + 1)
    if len(raw) > MAX_MANIFEST:
        raise DeltaError('Update metadata is too large')
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError) as ex:
        raise DeltaError('Invalid update metadata') from ex


def write_json(path, value):
    path = Path(path)
    temporary = safe_path(path.parent, path.name + '.new')
    with temporary.open('w', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=True, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def validate_manifest(manifest, old=None, new=None, edition=None):
    if (not isinstance(manifest, dict) or type(manifest.get('schema')) is not int
            or manifest.get('schema') != 1):
        raise DeltaError('Unsupported update schema')
    source, target = version(manifest.get('from')), version(manifest.get('to'))
    if not updater.is_newer_version(target, source):
        raise DeltaError('Update must move to a newer version')
    if old is not None and source != old or new is not None and target != new:
        raise DeltaError('Update version mismatch')
    kind = manifest.get('edition')
    if kind not in ('lite', 'full') or edition is not None and kind != edition:
        raise DeltaError('Update edition mismatch')
    for key in ('before', 'after'):
        files = manifest.get(key)
        if not isinstance(files, dict) or not 0 < len(files) <= MAX_FILES:
            raise DeltaError('Invalid file inventory')
        seen, total = set(), 0
        for name, record in files.items():
            valid_path(name, kind)
            if name.lower() in seen or not isinstance(record, dict):
                raise DeltaError('Duplicate path or invalid file record')
            seen.add(name.lower())
            if (set(record) != {'size', 'sha256'} or type(record['size']) is not int
                    or not 0 <= record['size'] <= MAX_UNPACKED
                    or not isinstance(record['sha256'], str)
                    or not re.fullmatch('[0-9a-f]{64}', record['sha256'])):
                raise DeltaError('Invalid file size or SHA-256')
            total += record['size']
        if total > MAX_UNPACKED or 'DLSS5Tool.exe' not in files or HELPER not in files:
            raise DeltaError('Oversized or incomplete release inventory')
        if kind == 'full' and 'mods/enhancement/guidance_worker.exe' not in files:
            raise DeltaError('Full edition is missing its worker')
    # Reject case-only renames and file/directory transitions on Windows.
    union = set(manifest['before']) | set(manifest['after'])
    lower = {p.lower() for p in union}
    if len(union) != len(lower):
        raise DeltaError('Case-only path changes are not supported')
    if any('/'.join(p.split('/')[:i]) in lower for p in lower for i in range(1, len(p.split('/')))):
        raise DeltaError('File/directory path conflict')
    return manifest


def changes(manifest):
    before, after = manifest['before'], manifest['after']
    return sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))


def check_baseline(root, manifest, cancelled=None):
    if installed_edition(root) != manifest['edition']:
        raise DeltaError('Installed components changed; update edition no longer matches')
    for name, expected in manifest['before'].items():
        if cancelled and cancelled():
            raise updater.DownloadCancelled('Update cancelled')
        path = safe_path(root, name)
        if not path.is_file() or file_record(path) != expected:
            raise DeltaError(f'Installed file missing or modified; no files replaced: {name}')
    for name in manifest['after'].keys() - manifest['before'].keys():
        if safe_path(root, name).exists():
            raise DeltaError(f'New release file conflicts with a local file: {name}')


def check_space(root, required):
    if shutil.disk_usage(root).free < required + SPACE_MARGIN:
        raise DeltaError('Insufficient disk space for download, staging and rollback backup')


def transaction_path(root):
    return safe_path(root, TRANSACTION)


@contextmanager
def transaction_lock(root):
    path = safe_path(transaction_path(root), 'lock')
    with path.open('a+b') as handle:
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as ex:
                raise DeltaError('Another update helper is running') from ex
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def status(root):
    path = safe_path(transaction_path(root), 'state.json')
    if not path.exists():
        return None
    value = read_json(path)
    if not isinstance(value, dict) or value.get('phase') not in (
            'failed', 'ready', 'waiting', 'applying', 'complete', 'rolled_back', 'recovery_needed'):
        raise DeltaError('Invalid update state; keep the update directory for manual recovery')
    return value


def set_state(root, phase, **extra):
    write_json(safe_path(transaction_path(root), 'state.json'), {'phase': phase, **extra})


def discard_transaction(root):
    """Call ONLY after explicit user confirmation. Never discard recovery data."""
    directory = transaction_path(root)
    with transaction_lock(root):
        state = status(root)
        if not state or state.get('phase') not in ('complete', 'rolled_back', 'ready', 'failed'):
            raise DeltaError('Unfinished update needs recovery; backup cannot be removed')
        for path in directory.rglob('*'):
            safe_path(directory, path.relative_to(directory))
            if path.is_file() and path.stat().st_nlink > 1:
                raise DeltaError('Refusing to remove a hard-linked update artifact')
    # Lock handle must be closed on Windows. Called before a new helper starts.
    shutil.rmtree(directory)


def prepare(root, asset, current, target, edition, progress=None, cancelled=None, opener=None):
    root = Path(root).absolute()
    if asset.name != asset_name(current, target, edition) or not updater._expected_sha256(asset.digest):
        raise DeltaError('Missing trusted update digest or wrong update asset')
    directory = transaction_path(root)
    if directory.exists():
        raise DeltaError('Previous update/backup exists; review it before starting another update')
    check_space(root, asset.size * 2)
    directory.mkdir()
    set_state(root, 'failed', error='Download/preparation did not finish; application is unchanged')
    with transaction_lock(root):
        archive = safe_path(directory, 'package.dlssupdate')
        updater.download_asset(asset, str(archive), progress, cancelled, opener=opener)
        with zipfile.ZipFile(archive) as bundle:
            infos = bundle.infolist()
            if len(infos) > MAX_FILES + 1 or len({i.filename for i in infos}) != len(infos):
                raise DeltaError('Duplicate or excessive archive entries')
            info = bundle.getinfo('manifest.json')
            if info.file_size > MAX_MANIFEST:
                raise DeltaError('Update manifest too large')
            manifest = validate_manifest(json.loads(bundle.read(info)), current, target, edition)
            delta = changes(manifest)
            payload = {p for p in delta if p in manifest['after']}
            if {i.filename for i in infos} != {'manifest.json'} | {'payload/' + p for p in payload}:
                raise DeltaError('Archive payload does not match its manifest')
            unpacked = sum(manifest['after'][p]['size'] for p in payload)
            backup = sum(manifest['before'][p]['size'] for p in delta if p in manifest['before'])
            check_space(root, unpacked + backup + max((manifest['after'][p]['size'] for p in payload), default=0))
            check_baseline(root, manifest, cancelled)
            for name in payload:
                info = bundle.getinfo('payload/' + name)
                if (info.file_size != manifest['after'][name]['size'] or info.flag_bits & 1
                        or stat.S_ISLNK(info.external_attr >> 16)):
                    raise DeltaError('Invalid payload entry')
                path = safe_path(directory, 'payload/' + name)
                path.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, path.open('xb') as output:
                    copied = 0
                    while chunk := source.read(1024 * 1024):
                        if cancelled and cancelled():
                            raise updater.DownloadCancelled('Update cancelled')
                        copied += len(chunk)
                        if copied > info.file_size:
                            raise DeltaError('Expanded payload exceeds declared size')
                        output.write(chunk)
                if file_record(path) != manifest['after'][name]:
                    raise DeltaError(f'Payload hash mismatch: {name}')
        write_json(directory / 'manifest.json', manifest)
        # Use the running release's helper, never a downloaded script/executable.
        helper = safe_path(root, HELPER)
        if file_record(helper) != manifest['before'][HELPER]:
            raise DeltaError('Installed update helper was modified')
        shutil.copyfile(helper, directory / HELPER)
        if cancelled and cancelled():
            raise updater.DownloadCancelled('Update cancelled')
        set_state(root, 'ready', target=target)
        return manifest


def _replace_copy(source, destination, scratch):
    """Same-volume scratch avoids a partially written destination."""
    shutil.copyfile(source, scratch)
    with scratch.open('r+b') as handle:
        os.fsync(handle.fileno())
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(scratch, destination)


def rollback(root, manifest, touched):
    directory = transaction_path(root)
    errors = []
    for name in reversed(touched):
        try:
            destination = safe_path(root, name)
            if name in manifest['before']:
                backup = safe_path(directory, 'backup/' + name)
                if file_record(backup) != manifest['before'][name]:
                    raise DeltaError('Rollback backup failed integrity check')
                if destination.is_file() and file_record(destination) == manifest['before'][name]:
                    continue  # Failed replacement may have left the old, locked file intact.
                _replace_copy(backup, destination, safe_path(directory, 'scratch'))
            elif destination.exists():
                # Never delete a file another process/user changed after apply.
                if file_record(destination) != manifest['after'][name]:
                    raise DeltaError('New file changed after installation; manual recovery required')
                destination.unlink()
        except Exception as ex:
            errors.append(f'{name}: {ex}')
    if errors:
        set_state(root, 'recovery_needed', error='; '.join(errors))
        raise DeltaError('Rollback incomplete. Keep backup and run helper --recover: ' + '; '.join(errors))
    set_state(root, 'rolled_back')


def apply_transaction(root, *, recover=False):
    """Caller must hold transaction_lock and wait for the application to exit."""
    directory = transaction_path(root)
    manifest = validate_manifest(read_json(safe_path(directory, 'manifest.json')))
    state = status(root) or {}
    if recover:
        if state.get('phase') == 'waiting':
            # A helper killed while waiting has not touched the installation.
            set_state(root, 'ready', target=manifest['to'])
            return
        if state.get('phase') not in ('applying', 'recovery_needed'):
            raise DeltaError('No interrupted transaction to recover')
        journal = read_json(safe_path(directory, 'journal.json'))
        touched = journal.get('touched')
        if (not isinstance(touched, list) or len(set(touched)) != len(touched)
                or any(p not in changes(manifest) for p in touched)):
            raise DeltaError('Invalid recovery journal')
        rollback(root, manifest, touched)
        return
    if state.get('phase') not in ('ready', 'waiting'):
        raise DeltaError('Update is not ready to apply')
    touched = []
    try:
        check_baseline(root, manifest)
        delta = changes(manifest)
        check_space(root, sum(manifest['before'][p]['size'] for p in delta if p in manifest['before'])
                    + max((manifest['after'][p]['size'] for p in delta if p in manifest['after']), default=0))
        for name in delta:
            if name in manifest['after']:
                staged = safe_path(directory, 'payload/' + name)
                if file_record(staged) != manifest['after'][name]:
                    raise DeltaError(f'Staged payload modified: {name}')
            if name in manifest['before']:
                backup = safe_path(directory, 'backup/' + name)
                backup.parent.mkdir(parents=True, exist_ok=True)
                with safe_path(root, name).open('rb') as source, backup.open('xb') as output:
                    shutil.copyfileobj(source, output)
                    output.flush()
                    os.fsync(output.fileno())
                if file_record(backup) != manifest['before'][name]:
                    raise DeltaError(f'Backup verification failed: {name}')
        write_json(directory / 'journal.json', {'touched': []})
        set_state(root, 'applying')
        for name in delta:
            # Persist intent before each mutation, so a killed helper is recoverable.
            touched.append(name)
            write_json(directory / 'journal.json', {'touched': touched})
            destination = safe_path(root, name)
            if name in manifest['after']:
                _replace_copy(safe_path(directory, 'payload/' + name), destination,
                              safe_path(directory, 'scratch'))
            else:
                destination.unlink()
        for name, expected in manifest['after'].items():
            if file_record(safe_path(root, name)) != expected:
                raise DeltaError(f'Installed file verification failed: {name}')
        set_state(root, 'complete', target=manifest['to'])
    except Exception as ex:
        if touched:
            rollback(root, manifest, touched)
        else:
            set_state(root, 'failed', error=str(ex))
        raise
