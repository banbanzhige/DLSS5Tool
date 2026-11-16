"""Optional Velopack portable updates; existing ZIP editions keep schema 1.

No SDK import or update mutation occurs for an ordinary portable installation.
Release and per-file metadata are pinned to official GitHub asset SHA256s before
an SDK-prepared package can be installed. User components live outside current/.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import shutil
import sys
import zipfile

from dlss5tool import delta_update as delta, paths, updater
from dlss5tool.portable_layout import load_portable_layout

STATE_DIRECTORY = '.dlss5-managed'
MAX_PACKAGE_BYTES = 2 * 1024**3
SDK_VERSION = '1.2.161'


def current_layout():
    if not getattr(sys, 'frozen', False):
        return None
    return load_portable_layout(Path(sys.executable).absolute().parent,
                                paths.resource_root(), Path(sys.executable).name)


def startup():
    """Only opt-in packages load the SDK; worker dispatch never auto-applies."""
    layout = current_layout()
    if layout is None:
        return
    sdk = importlib.import_module('velopack')
    sdk.App().set_auto_apply_on_startup(False).run()


def manifest_name(tag, edition):
    delta.version(tag)
    if edition not in ('lite', 'full'):
        raise updater.UpdateError('Invalid managed update edition')
    return f'DLSS5Tool-{tag}-win-{edition}.manifest.json'


def trusted_asset(release, name):
    matches = [asset for asset in release.assets if asset.name == name]
    expected = f'https://github.com/{updater.GITHUB_REPOSITORY}/releases/download/{release.tag}/{name}'
    if (len(matches) != 1 or matches[0].download_url != expected
            or not updater._expected_sha256(matches[0].digest)
            or not 0 < matches[0].size < MAX_PACKAGE_BYTES):
        raise updater.UpdateError(f'Missing trusted managed update asset: {name}')
    return matches[0]


def package_inventory(path):
    """Do not trust recompression to preserve the complete ZIP container hash."""
    files = {}
    seen = set()
    with zipfile.ZipFile(path) as bundle:
        if len(bundle.infolist()) > delta.MAX_FILES + 100:
            raise updater.UpdateError('Managed package has too many entries')
        for item in bundle.infolist():
            if item.is_dir():
                continue
            name = item.filename
            # NUPKG package metadata and app payload use this fixed layout.
            delta.safe_path(Path(path).parent / 'inventory-only', name)
            if name.casefold() in seen or item.flag_bits & 1:
                raise updater.UpdateError('Duplicate or encrypted managed package entry')
            seen.add(name.casefold())
            import stat
            if stat.S_ISLNK(item.external_attr >> 16):
                raise updater.UpdateError('Managed package contains a symbolic link')
            if item.file_size > delta.MAX_UNPACKED:
                raise updater.UpdateError('Managed package entry is too large')
            import hashlib
            with bundle.open(item) as source:
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
            files[name] = {'size': item.file_size, 'sha256': digest}
        if sum(record['size'] for record in files.values()) > delta.MAX_UNPACKED:
            raise updater.UpdateError('Managed package expands beyond the supported limit')
    return files


def validate_target_manifest(value, release, layout):
    if (not isinstance(value, dict) or value.get('schema') != 1
            or type(value.get('schema')) is not int or value.get('provider') != 'velopack'
            or value.get('sdk_version') != SDK_VERSION
            or value.get('version') != release.tag or value.get('package_id') != layout.package_id
            or value.get('edition') != layout.edition or value.get('channel') != layout.channel):
        raise updater.UpdateError('Managed update manifest identity/protocol mismatch')
    files = value.get('files')
    if not isinstance(files, dict) or not 0 < len(files) <= delta.MAX_FILES:
        raise updater.UpdateError('Managed update target inventory is invalid')
    seen, total = set(), 0
    for name, record in files.items():
        if not isinstance(name, str):
            raise updater.UpdateError('Invalid package path')
        delta.safe_path(layout.root / 'inventory-only', name)
        if (name.casefold() in seen or not isinstance(record, dict)
                or set(record) != {'size', 'sha256'} or type(record['size']) is not int
                or not 0 <= record['size'] <= delta.MAX_UNPACKED
                or not updater._expected_sha256('sha256:' + str(record['sha256']))):
            raise updater.UpdateError('Invalid managed update file record')
        seen.add(name.casefold())
        total += record['size']
    if total > delta.MAX_UNPACKED or 'lib/app/DLSS5Tool.exe' not in files:
        raise updater.UpdateError('Incomplete or oversized managed update target')
    # Package contents cannot become a remote arbitrary user-data write list.
    for name in files:
        if name.startswith('lib/app/'):
            relative = name[len('lib/app/'):]
            if relative not in {'sq.version', 'Squirrel.exe', value['package_id'] + '_ExecutionStub.exe'}:
                delta.valid_path(relative, 'lite')
            if relative.startswith('mods/'):
                raise updater.UpdateError('User components must remain outside the core package')
    return value


class Session:
    """Pinned target, cancellable preparation boundary, persistent ready proof."""
    def __init__(self, release, *, layout=None, sdk=None):
        self.layout = layout or current_layout()
        if self.layout is None:
            raise updater.UpdateError('This installation uses the existing portable updater')
        self.release = release
        self.sdk = sdk or importlib.import_module('velopack')
        cache = delta.safe_path(self.layout.root, 'packages')
        if cache.exists():
            for entry in cache.rglob('*'):
                delta.safe_path(cache, entry.relative_to(cache))
        options = self.sdk.UpdateOptions(False, 10, self.layout.channel)
        self.manager = self.sdk.UpdateManager(
            self.sdk.GithubSource(f'https://github.com/{updater.GITHUB_REPOSITORY}'), options=options)
        if (self.manager.get_app_id() != self.layout.package_id or not self.manager.get_is_portable()
                or not updater.is_newer_version(release.tag, self.manager.get_current_version())):
            raise updater.UpdateError('Managed update does not match this portable installation')
        self.directory = delta.safe_path(self.layout.root, STATE_DIRECTORY)
        self.metadata_asset = trusted_asset(release, manifest_name(release.tag, self.layout.edition))
        if self.metadata_asset.size > delta.MAX_MANIFEST:
            raise updater.UpdateError('Managed update manifest is too large')

    def prepare(self, progress=None, cancelled=None):
        self.directory.mkdir(exist_ok=True)
        metadata_path = delta.safe_path(self.directory, self.metadata_asset.name)
        if not metadata_path.is_file() or delta.file_record(metadata_path) != {
                'size': self.metadata_asset.size,
                'sha256': updater._expected_sha256(self.metadata_asset.digest)}:
            updater.download_asset(self.metadata_asset, str(metadata_path), cancelled=cancelled)
        value = validate_target_manifest(delta.read_json(metadata_path), self.release, self.layout)
        if self.layout.edition == 'full':
            component = delta.safe_path(self.layout.root, 'mods/enhancement/enhancement.json')
            contract = delta.read_json(component)
            if (contract.get('id') != 'dlss5-guidance'
                    or type(contract.get('protocol')) is not int
                    or contract['protocol'] != value.get('guidance_protocol')):
                raise updater.UpdateError('The external full component requires an update before this app version')
        full_asset = trusted_asset(self.release, value.get('full_asset'))
        delta_asset_record = {'size': full_asset.size, 'sha256': updater._expected_sha256(full_asset.digest)}
        if value.get('full_record') != delta_asset_record:
            raise updater.UpdateError('Target package differs from official release digest')
        required = full_asset.size * 2 + sum(r['size'] for r in value['files'].values())
        if shutil.disk_usage(self.layout.root).free < required + 15 * 1024**3:
            raise updater.UpdateError('Insufficient space for update, candidate and rollback')
        update = self.manager.check_for_updates()
        target = None if update is None else update.TargetFullRelease
        if (target is None or update.IsDowngrade or target.PackageId != self.layout.package_id
                or target.FileName != full_asset.name or target.Version != self.release.tag.lstrip('v')
                or target.Size != full_asset.size or target.SHA256.lower() != delta_asset_record['sha256']):
            raise updater.UpdateError('Update target changed while preparing; check again')
        if cancelled and cancelled():
            raise updater.DownloadCancelled('Update preparation cancelled')
        self.manager.download_updates(update, progress_callback=progress)
        # The SDK lacks a transport cancellation token. Never schedule an install
        # after a user cancels; already completed cache bytes may remain reusable.
        if cancelled and cancelled():
            raise updater.DownloadCancelled('Update preparation cancelled')
        package = delta.safe_path(self.layout.root / 'packages', full_asset.name)
        if package_inventory(package) != value['files']:
            raise updater.UpdateError('Reconstructed target files failed verification')
        ready = {'schema': 1, 'phase': 'ready', 'target': self.release.tag,
                 'edition': self.layout.edition, 'package_id': self.layout.package_id,
                 'package': package.name, 'cached_record': delta.file_record(package),
                 'metadata': self.metadata_asset.name,
                 'metadata_record': delta.file_record(metadata_path),
                 'source_version': self.manager.get_current_version(),
                 'planned_deltas': [asset.FileName for asset in update.DeltasToTarget]}
        delta.write_json(delta.safe_path(self.directory, 'ready.json'), ready)
        self._update = update
        return ready

    def ready(self):
        record = delta.read_json(delta.safe_path(self.directory, 'ready.json'))
        if (record.get('phase') != 'ready' or record.get('target') != self.release.tag
                or record.get('edition') != self.layout.edition
                or record.get('package_id') != self.layout.package_id):
            raise updater.UpdateError('Prepared update identity does not match this installation')
        metadata_path = delta.safe_path(self.directory, self.metadata_asset.name)
        if delta.file_record(metadata_path) != {'size': self.metadata_asset.size,
                                               'sha256': updater._expected_sha256(self.metadata_asset.digest)}:
            raise updater.UpdateError('Prepared release manifest changed')
        manifest = validate_target_manifest(delta.read_json(metadata_path), self.release, self.layout)
        package = delta.safe_path(self.layout.root / 'packages', manifest['full_asset'])
        if package.name != record['package'] or delta.file_record(package) != record['cached_record']:
            raise updater.UpdateError('Prepared update package changed')
        if package_inventory(package) != manifest['files']:
            raise updater.UpdateError('Prepared target content changed')
        pending = self.manager.get_update_pending_restart()
        if pending is None or pending.Version != self.release.tag.lstrip('v') or pending.FileName != package.name:
            raise updater.UpdateError('Native updater has no matching prepared update')
        return pending

    def apply_on_exit(self, *, restart):
        pending = self.ready()
        self.manager.wait_exit_then_apply_updates(pending, silent=True, restart=restart)
