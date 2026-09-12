"""Create new lite/full/add-on editions from explicit verified inputs.

No runtime pruning, no downloads, no source/deployed component replacement.
Outputs include ZIP64 archives, SHA256 inventories, CRC checks and optional
<2GiB upload volumes. The full edition is exactly lite overlaid with add-on.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dlss5tool.app_version import APP_VERSION
from scripts.check_release_contents import forbidden_contents
from scripts.release_updates import plan_updates, build_updates, verify_upload_updates

# Updated flow-edge component verified in docs/development/PROCESSING_LIMITS_20260911.md.
WORKER_SHA = '2b309510ef6f73ae73dc98d11842a8ef3ba2e012c19a7e1cfe38def9a3b9f450'
MODELS = ('raft_large_C_T_SKHT_V2-ff5fadd5.pth',)


def release_notes_path():
    return ROOT / 'docs/release' / f'RELEASE_NOTES_{APP_VERSION}.md'


def validate_flow_inventory(manifest):
    leaked = [name for name in manifest if Path(name).match('depth_anything_v2_*.pth')]
    if leaked:
        raise ValueError(f'Depth weights leaked into public edition: {leaked}')


def portable_update_name(version=APP_VERSION):
    # Existing in-app updaters only recognize this exact filename.
    return f'DLSS5Tool-{version}-win64.zip'


def link_or_copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def sha(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def copy_tree(source, target, *, flow_only=False):
    # Do not follow symlinks/reparse points into an unrelated user directory.
    for path in source.rglob('*'):
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError(f'Refusing link in package input: {path}')
    shutil.copytree(source, target, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('depth_anything_v2_*.pth') if flow_only else None)


def inventory(directory):
    return {p.relative_to(directory).as_posix(): {'size': p.stat().st_size, 'sha256': sha(p)}
            for p in sorted(directory.rglob('*')) if p.is_file()}


def archive(directory, destination, manifest):
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6, allowZip64=True) as zip:
        for name in manifest:
            zip.write(directory / name, name)
    with zipfile.ZipFile(destination) as zip:
        if set(zip.namelist()) != set(manifest) or zip.testzip() is not None:
            raise RuntimeError(f'Archive verification failed: {destination}')
    return {'name': destination.name, 'size': destination.stat().st_size,
            'sha256': sha(destination), 'unpacked_bytes': sum(v['size'] for v in manifest.values()),
            'file_count': len(manifest), 'crc_verified': True}


def split_archive(path, folder, part_bytes=1900*1024*1024, max_asset_bytes=2*1024**3):
    if path.stat().st_size < max_asset_bytes:
        return []
    folder.mkdir(parents=True, exist_ok=True)
    parts = []
    combined = hashlib.sha256()
    with path.open('rb') as source:
        index = 1
        while source.tell() < path.stat().st_size:
            target = folder / (path.name + f'.{index:03d}')
            remaining = part_bytes
            with target.open('xb') as dest:
                while remaining:
                    data = source.read(min(8*1024*1024, remaining))
                    if not data:
                        break
                    dest.write(data)
                    remaining -= len(data)
            with target.open('rb') as check:
                for data in iter(lambda: check.read(8*1024*1024), b''):
                    combined.update(data)
            parts.append({'name': target.name, 'size': target.stat().st_size, 'sha256': sha(target)})
            index += 1
    if combined.hexdigest() != sha(path):
        raise RuntimeError('Reassembled volume digest differs from ZIP')
    return parts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--component', type=Path, required=True)
    parser.add_argument('--models', type=Path, required=True)
    parser.add_argument('--licenses', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--update-baseline', type=Path, action='append', default=[],
                        help='Relocate a required baseline editions directory; repeat for multiple versions. Defaults come from packaging/update-policy.json')
    parser.add_argument('--initial-update-baseline', action='store_true',
                        help='Explicit first-updater-release exemption only (v2.2.0); forbidden for later releases')
    parser.add_argument('--allow-external-output', action='store_true',
                        help='Explicitly permit a new output directory on another disk; never overwrites existing paths')
    args = parser.parse_args()
    base, component, model_dir, license_dir = [p.resolve() for p in
                                             (args.base, args.component, args.models, args.licenses)]
    output = args.output.resolve()
    if output.exists() or output == Path(output.anchor) or (not args.allow_external_output and not output.is_relative_to(ROOT / 'dist')):
        parser.error('Use a NEW output directory in dist, or explicitly opt into a new external output directory')
    update_plan = plan_updates(APP_VERSION, initial=args.initial_update_baseline, overrides=args.update_baseline)
    if forbidden_contents(base):
        raise ValueError('Base package contains development/user data')
    if not (base / 'DLSS5Update.exe').is_file():
        raise ValueError('Base package lacks the standalone update helper; rebuild it before packaging editions')
    if sha(component / 'guidance_worker.exe') != WORKER_SHA:
        raise ValueError('Component differs from the fully verified optimized candidate')
    for name in MODELS:
        if not (model_dir / name).is_file():
            raise FileNotFoundError(name)
    for name in ('DepthAnythingV2-CODE-LICENSE.txt',):
        if not (license_dir / name).is_file() or (license_dir / name).stat().st_size < 1000:
            raise ValueError(f'Missing original license: {name}')
    if not release_notes_path().is_file():
        raise FileNotFoundError(release_notes_path())
    output.mkdir(parents=True)
    folders = {name: output / f'DLSS5Tool-{APP_VERSION}-win64-{name}' for name in ('lite', 'addon', 'full')}
    print('Staging lite edition...', flush=True)
    copy_tree(base, folders['lite'])
    # Common notices live outside the core DLL directories and contain no user paths.
    notice = ('发行前须知 / Distribution review\n\n'
              '此包已完成本机功能验证，不等于所有显卡、干净环境或第三方分发许可审核通过。\n'
              'NVIDIA DLSS/RTX Video、CUDA/cuDNN、FFmpeg及各依赖仍受各自上游条款约束。\n'
              'Model editions include RAFT-Large only; no Depth Anything V2 weights.\n'
              'The publisher must review redistribution, notices and any source-offer obligations before public release.\n'
              'No files are uploaded or published by this build.\n')
    (folders['lite'] / 'DISTRIBUTION-REVIEW.txt').write_text(notice, encoding='utf-8')
    base_licenses = folders['lite'] / 'licenses'
    base_licenses.mkdir(exist_ok=True)
    shutil.copy2(ROOT / 'third_party/NVIDIA-DLSS/LICENSE.txt', base_licenses / 'NVIDIA-DLSS-SDK-LICENSE.txt')
    print('Staging complete add-on (no dependency pruning)...', flush=True)
    mods = folders['addon'] / 'mods'
    mods.mkdir(parents=True)
    copy_tree(component, mods / 'enhancement', flow_only=True)
    shutil.copy2(ROOT / 'mods/README.md', mods / 'enhancement/README.md')
    shutil.copy2(ROOT / 'mods/README.md', mods / 'README.md')
    weights = mods / 'models'
    weights.mkdir()
    model_records = {}
    for name in MODELS:
        shutil.copy2(model_dir / name, weights / name)
        model_records[name] = {'size': (weights / name).stat().st_size, 'sha256': sha(weights / name)}
    notices = mods / 'enhancement/licenses'
    notices.mkdir(exist_ok=True)
    for path in license_dir.iterdir():
        if path.is_file():
            shutil.copy2(path, notices / path.name)
    shutil.copy2(ROOT / 'LICENSE', notices / 'DLSS5Tool-LICENSE.txt')
    shutil.copy2(ROOT / 'licenses/torchvision-LICENSE.txt', notices / 'torchvision-LICENSE.txt')
    # Preserve packaged licenses and additionally collect installed dependency
    # license/notice files into a visible archive, without copying their code.
    site = ROOT / 'tmp/guidance-cuda-env/Lib/site-packages'
    for dist in sorted(site.glob('*.dist-info')):
        for path in dist.rglob('*'):
            if path.is_file() and any(word in path.name.lower() for word in ('license', 'notice', 'copying', 'copyright')):
                dest = notices / 'dependencies' / dist.name / path.relative_to(dist)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
    (mods / 'ADDON-INSTALL.txt').write_text(
        f'DLSS5Tool {APP_VERSION} 光流附加包\n\n'
        '关闭程序，将本压缩包解压到DLSS5Tool.exe所在目录；mods应与EXE同级。不要解压到mods内部。\n'
        '包含优化推理组件、完整依赖及RAFT-Large；不附深度模型权重；无需系统Python或CUDA Toolkit。\n'
        '完整版本无需重复安装本包。启动后在推理模型页选择模式，检查通过才启用。\n'
        '不包含主程序、用户设置、队列或运行库替换文件。保留完整_internal目录。\n'
        '模型来源和第三方许可详见enhancement/licenses。\n'
        'Close the app; extract beside DLSS5Tool.exe, not inside mods. Keep all files together.\n', encoding='utf-8')
    (notices / 'MODEL-NOTICES.md').write_text(
        '# Models and provenance\n\n'
        '- Depth Anything V2: Lihe Yang, Bingyi Kang, Zilong Huang, Zhen Zhao, Xiaogang Xu, Jiashi Feng, Hengshuang Zhao. '
        'Code: https://github.com/DepthAnything/Depth-Anything-V2 ; Large weights: '
        'https://huggingface.co/depth-anything/Depth-Anything-V2-Large . '
        'Architecture code remains in the component; depth weights are NOT included in this edition.\n'
        '- torchvision RAFT-Large: https://github.com/pytorch/vision/tree/v0.23.0 ; '
        'checkpoint: https://download.pytorch.org/models/raft_large_C_T_SKHT_V2-ff5fadd5.pth . '
        'Original torchvision BSD-3-Clause license is included. Weight bytes were not modified.\n'
        '- DLSS5Tool flow input preparation and depth attention adapters do not change model weights. '
        'No broad commercial-use or redistribution approval is implied.\n\n'
        '```json\n' + json.dumps(model_records, indent=2) + '\n```\n', encoding='utf-8')
    (mods / 'enhancement/DISTRIBUTION-REVIEW.txt').write_text(notice, encoding='utf-8')
    print('Staging full edition as lite + exact add-on overlay...', flush=True)
    copy_tree(folders['lite'], folders['full'])
    copy_tree(folders['addon'], folders['full'])
    manifest_dir = output / 'verification'
    manifest_dir.mkdir()
    manifests = {}
    for name, folder in folders.items():
        manifests[name] = inventory(folder)
        (manifest_dir / f'{name}-files.json').write_text(json.dumps(manifests[name], indent=2), encoding='utf-8')
    for name in ('full', 'addon'):
        validate_flow_inventory(manifests[name])
    if manifests['full'] != {**manifests['lite'], **manifests['addon']}:
        raise RuntimeError('Full edition is not the exact lite + add-on overlay')
    if any(n.startswith(('mods/enhancement/', 'mods/models/')) or n.endswith('.pth') for n in manifests['lite']):
        raise RuntimeError('Inference files leaked into lite')
    if any(not n.startswith('mods/') for n in manifests['addon']):
        raise RuntimeError('Add-on contains files outside mods')
    print('Compressing three ZIP64 editions, then verifying all CRCs...', flush=True)
    records = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = {name: pool.submit(archive, folder, output / (folder.name + '.zip'), manifests[name])
                for name, folder in folders.items()}
        for name, future in jobs.items():
            records[name] = future.result()
            print(name, json.dumps(records[name]), flush=True)
    assets = output / 'github-assets'
    assets.mkdir(parents=True, exist_ok=True)
    for name, record in records.items():
        print('Checking upload volumes:', name, flush=True)
        record['volumes'] = split_archive(output / record['name'], assets)
    # Existing updaters match DLSS5Tool-vX.Y.Z-win64.zip and, if that is
    # missing, prefer the largest remaining ZIP. Never upload unsplit
    # full/add-on archives; publish the lite bytes under the canonical name.
    canonical = portable_update_name(APP_VERSION)
    records['lite']['github_name'] = canonical
    link_or_copy(output / records['lite']['name'], assets / canonical)
    root_checks = [f"{records['lite']['sha256']}  {canonical}"]
    asset_checks = [f"{records['lite']['sha256']}  {canonical}"]
    for name in ('lite', 'addon', 'full'):
        record = records[name]
        root_checks.append(f"{record['sha256']}  {record['name']}")
        for part in record['volumes']:
            root_checks.append(f"{part['sha256']}  github-assets/{part['name']}")
            asset_checks.append(f"{part['sha256']}  {part['name']}")
    incremental = build_updates(update_plan, folders, manifests, output, assets)
    for record in incremental['assets']:
        root_checks.append(f"{record['sha256']}  github-assets/{record['name']}")
        asset_checks.append(f"{record['sha256']}  {record['name']}")
    (output / 'SHA256SUMS.txt').write_text('\n'.join(root_checks) + '\n', encoding='utf-8')
    report = {'version': APP_VERSION, 'built_at': time.strftime('%Y-%m-%d %H:%M:%S'),
              'component_sha256': WORKER_SHA, 'models': model_records, 'editions': records,
              'full_equals_lite_plus_addon': True, 'license_review': 'Publisher review required; RAFT weights only; depth architecture retained',
              'github_portable_asset': canonical, 'incremental_updates': incremental, 'published': False}
    (output / 'package-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    shutil.copy2(output / 'package-report.json', assets / 'package-report.json')
    shutil.copy2(ROOT / 'scripts/Join-ReleaseArchive.ps1', assets / 'Join-ReleaseArchive.ps1')
    shutil.copy2(release_notes_path(), output / f'RELEASE_NOTES_{APP_VERSION}.md')
    shutil.copy2(release_notes_path(), assets / f'RELEASE_NOTES_{APP_VERSION}.md')
    (assets / 'SHA256SUMS.txt').write_text('\n'.join(asset_checks) + '\n', encoding='utf-8')
    verify_upload_updates(assets, report)
    (assets / 'README-UPLOAD.txt').write_text(
        'GitHub Release upload set. Do not publish this folder automatically.\n\n'
        f'Required portable app (in-app updater): {canonical}\n'
        'Do not also upload *-lite.zip; it is the same bytes.\n'
        'Upload full/add-on as .zip.001 .002 .003 only. Unsplit archives exceed 2 GiB.\n'
        + ('First updater baseline: old clients must manually install this release. No delta payloads.\n'
           if incremental['mode'] == 'initial_baseline' else
           'REQUIRED incremental payloads (upload every file):\n' +
           ''.join(f"  {r['name']}\n" for r in incremental['assets'])) +
        'Optional: 30系.zip, 50系.zip, Join-ReleaseArchive.ps1, SHA256SUMS.txt.\n'
        'No depth weights included; publisher must review third-party terms before public upload.\n',
        encoding='utf-8')
    print('DONE', output, flush=True)


if __name__ == '__main__':
    main()
