"""Tiny frozen app for isolated Velopack compatibility tests, never production."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import velopack


def main():
    # Updates must never be applied implicitly by a background/export worker.
    velopack.App().set_auto_apply_on_startup(False).run()
    parser = argparse.ArgumentParser()
    parser.add_argument('--sandbox', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--write-markers', action='store_true')
    parser.add_argument('--stable-data', action='store_true')
    parser.add_argument('--feed')
    parser.add_argument('--download', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--wrapped', action='store_true', help='Exercise the production managed-update adapter with a local synthetic feed')
    args = parser.parse_args()

    from dlss5tool import paths
    from dlss5tool.delta_update import safe_path
    from dlss5tool.portable_layout import load_portable_layout

    sandbox = args.sandbox.absolute()
    binary_root = paths.app_root()
    if not binary_root.is_relative_to(sandbox):
        raise RuntimeError('Probe must run inside the registered sandbox')
    report_path = args.report.absolute()
    if not report_path.is_relative_to(sandbox):
        raise RuntimeError('Probe report escaped the sandbox')
    safe_path(sandbox, report_path.relative_to(sandbox))
    data_root = binary_root
    layout = load_portable_layout(binary_root, paths.resource_root(), Path(sys.executable).name)
    if args.stable_data:
        data_root = paths.portable_root()
    safe_path(sandbox, data_root.relative_to(sandbox))
    markers = {
        'dlss5_settings.json': b'{"user_setting":"keep"}',
        'dlss5_queue.json': b'{"jobs":["keep"]}',
        'mods/nvngx_dlssnr.dll': b'custom runtime marker, never executed',
        'mods/models/custom.pth': b'custom model marker, never loaded',
        'mods/enhancement/custom.txt': b'custom component marker',
    }
    if args.write_markers:
        for name, content in markers.items():
            path = safe_path(data_root, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as output:
                output.write(content)
    record = {
        'sdk_version': velopack.__version__,
        'executable': str(Path(sys.executable)),
        'binary_root': str(binary_root),
        'data_root': str(data_root),
        'runtime_root': str(paths.runtime_root()),
        'state_root': str(paths.state_path('dlss5_settings.json').parent),
        'version_marker': (paths.resource_root() / 'probe-version.txt').read_text().strip(),
        'markers': {name: {'exists': (data_root / name).is_file(),
                           'matches': (data_root / name).is_file()
                           and (data_root / name).read_bytes() == content}
                    for name, content in markers.items()},
    }
    options = None if layout is None else velopack.UpdateOptions(False, 10, layout.channel)
    manager = velopack.UpdateManager(args.feed, options=options)
    record.update(current_version=manager.get_current_version(), app_id=manager.get_app_id(),
                  portable=manager.get_is_portable())
    if args.feed:
        update = manager.check_for_updates()
        record['update'] = None if update is None else {
            'target_version': update.TargetFullRelease.Version,
            'full_size': update.TargetFullRelease.Size,
            'full_name': update.TargetFullRelease.FileName,
            'deltas': [{'name': item.FileName, 'size': item.Size}
                       for item in update.DeltasToTarget],
            'downgrade': update.IsDowngrade,
        }
        if args.download:
            if update is None:
                raise RuntimeError('Expected an available update')
            progress = []
            if args.wrapped:
                from types import SimpleNamespace
                from dlss5tool import managed_update, updater
                target_version = 'v' + update.TargetFullRelease.Version
                feed = args.feed if isinstance(args.feed, Path) else Path(args.feed)
                metadata_name = managed_update.manifest_name(target_version, layout.edition)
                metadata_source = feed / metadata_name
                target_source = feed / update.TargetFullRelease.FileName
                assets = []
                for path in (metadata_source, target_source):
                    proof = managed_update.delta.file_record(path)
                    assets.append(updater.ReleaseAsset(path.name,
                        f'https://github.com/{updater.GITHUB_REPOSITORY}/releases/download/{target_version}/{path.name}',
                        proof['size'], 'sha256:' + proof['sha256']))
                release = updater.ReleaseInfo(target_version, updater.RELEASES_URL, '', tuple(assets))
                # Local fixtures substitute only the transport. Production uses
                # the official GitHub source and API-bound asset digests.
                sdk = SimpleNamespace(UpdateOptions=velopack.UpdateOptions,
                                      GithubSource=lambda _: str(feed), UpdateManager=velopack.UpdateManager)
                session = managed_update.Session(release, layout=layout, sdk=sdk)
                session.directory.mkdir(exist_ok=True)
                destination = session.directory / metadata_name
                if not destination.exists():
                    destination.write_bytes(metadata_source.read_bytes())
                ready_proof = session.prepare(progress=lambda value: progress.append(value))
                record['adapter_ready_proof'] = ready_proof
                # A fresh Session must recover the same ready package from disk.
                recovered = managed_update.Session(release, layout=layout, sdk=sdk)
                recovered.ready()
                record['adapter_ready_restored'] = True
            else:
                manager.download_updates(update, lambda value: progress.append(value))
            record['download_progress'] = progress
            pending = manager.get_update_pending_restart()
            record['pending_version'] = None if pending is None else pending.Version
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(record), flush=True)
    if args.apply:
        if not args.download:
            raise RuntimeError('Probe only applies freshly downloaded updates')
        # Native updater waits for this process to exit; no installer or app restart.
        if args.wrapped:
            recovered.apply_on_exit(restart=False)
        else:
            manager.wait_exit_then_apply_updates(update, silent=True, restart=False)


if __name__ == '__main__':
    main()
