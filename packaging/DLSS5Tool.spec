# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the portable Windows release."""

import os
import shutil
import sys

from PyInstaller.utils.hooks import collect_all


project_root = os.path.dirname(os.path.abspath(SPECPATH))
sys.path.insert(0, project_root)
from dlss5tool.app_version import APP_VERSION


datas = []
datas.append((os.path.join(project_root, 'licenses', 'torchvision-LICENSE.txt'), 'licenses'))
datas.append((os.path.join(project_root, 'licenses', 'NVIDIA-Optical-Flow-Headers-LICENSE.txt'), 'licenses'))
datas.append((os.path.join(project_root, 'scripts', 'rtxmfg_temporal', 'LICENSE.txt'), 'licenses/RTX40MFG-Unlock'))
datas.append((os.path.join(project_root, 'third_party', 'NVIDIA-DLSS', 'LICENSE.txt'), 'licenses/NVIDIA-DLSS'))
binaries = []
hiddenimports = []

for package in ("tkinterdnd2", "imageio_ffmpeg"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

# Verified optional GPU export is additive. Never collect a temporary candidate
# or silently omit a registered component from a build.
from dlss5tool.gpu_export_runtime import load_components, FILES as GPU_EXPORT_FILES
gpu_components = load_components()
if gpu_components:
    import json
    manifest_path = os.path.join(project_root, 'runtime', 'gpu-export', 'manifest.json')
    with open(manifest_path, encoding='utf-8') as gpu_manifest_file:
        gpu_manifest = json.load(gpu_manifest_file)
    if not gpu_manifest.get('packaging_license_review_complete'):
        if os.environ.get('DLSS5_LOCAL_CANDIDATE') != '1':
            raise SystemExit('Distribution review pending. Use build_release.ps1 -LocalCandidate for a validated local-only build.')
        import importlib.metadata
        from scripts.gpu_export_packaging import validate_candidate_materials
        candidate_status = validate_candidate_materials(
            os.path.join(project_root, 'runtime', 'gpu-export', 'licenses'),
            importlib.metadata.distribution('av').locate_file('av.libs'))
        print('LOCAL CANDIDATE: materials verified; distribution review remains pending:', candidate_status['unresolved'])
    import av
    if av.__version__ != '18.1.0':
        raise SystemExit('GPU export requires verified PyAV 18.1.0')
    av_datas, av_binaries, av_imports = collect_all('av')
    datas += av_datas
    binaries += av_binaries
    hiddenimports += av_imports
    gpu_root = os.path.join(project_root, 'runtime', 'gpu-export')
    datas.append((os.path.join(gpu_root, 'manifest.json'), 'gpu-export'))
    for field, filename in GPU_EXPORT_FILES.items():
        collection = binaries if filename.endswith(('.dll', '.exe')) else datas
        collection.append((gpu_components[field], 'gpu-export'))
    av_license = os.path.join(gpu_root, 'licenses')
    if not os.path.isdir(av_license):
        raise SystemExit('GPU export dependency license directory missing')
    datas.append((av_license, 'licenses/GPU-export'))

for filename, required in (
    ("dlssnr_host_v2.dll", True),
    ("nvngx_dlssnr.dll", True),
    ("dlssnr_host.dll", False),
    ("vsr_host.dll", True),
    ("nvngx_vsr.dll", True),
    ("dlssg_video_worker.exe", True),
):
    source = os.path.join(project_root, 'runtime', filename)
    if os.path.isfile(source):
        binaries.append((source, "."))
    elif required:
        raise SystemExit(f"Missing required runtime file: {source}")

# Frame generation uses the pinned official provider; never collect the SDK tree.
import hashlib
from dlss5tool.frame_generation import runtime_files, PINNED_RUNTIME
fg_worker, fg_provider = runtime_files()
with fg_provider.open('rb') as provider_file:
    provider_sha = hashlib.file_digest(provider_file, 'sha256').hexdigest()
if provider_sha != PINNED_RUNTIME:
    raise SystemExit('DLSSG provider does not match the verified release hash')
binaries.append((str(fg_provider), '.'))

# CFR validation needs ffprobe; HDR processing needs the verified full FFmpeg.
from pathlib import Path
from dlss5tool.video_export import find_ffmpeg, find_ffprobe
media_ffmpeg = Path(find_ffmpeg())
media_probe = find_ffprobe(str(media_ffmpeg))
if not media_probe:
    raise SystemExit('Full FFmpeg and ffprobe are required for this release')
binaries += [(str(media_ffmpeg), '.'), (media_probe, '.')]
media_license = media_ffmpeg.parent.parent / 'LICENSE'
media_readme = media_ffmpeg.parent.parent / 'README.txt'
if not media_license.is_file() or not media_readme.is_file():
    raise SystemExit('Verified FFmpeg distribution license/README are required')
datas += [(str(media_license), 'licenses/FFmpeg-full'), (str(media_readme), 'licenses/FFmpeg-full')]

for filename in ("LICENSE", "README.md", "README.en.md", "THIRD_PARTY_NOTICES.md", "docs/guidance/GUIDANCE_PARAMETERS.md"):
    source = os.path.join(project_root, filename)
    if os.path.isfile(source):
        datas.append((source, "."))

for language in ("zh_CN", "en_US"):
    source = os.path.join(project_root, "locales", f"{language}.json")
    if not os.path.isfile(source):
        raise SystemExit(f"Missing localization catalog: {source}")
    datas.append((source, "locales"))

# Guidance ships separately in mods/enhancement, not in the base package.

app_icon = os.path.join(project_root, "assets", "app.ico")
app_icon_png = os.path.join(project_root, "assets", "app.png")
for required_icon in (app_icon, app_icon_png):
    if not os.path.isfile(required_icon):
        raise SystemExit(f"Missing required application icon: {required_icon}")
    datas.append((required_icon, "assets"))

a = Analysis(
    [os.path.join(project_root, "gui.py")],
    pathex=[project_root],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Repository-only tools must never enter the main application's PYZ.
    excludes=["torch", "torchvision", "depth_anything_v2", "guidance_worker",
              "amd_devtest", "amd_devtest_ui", "scripts", "tests",
              "dlss5tool.guidance_worker", "dlss5tool.amd_devtest", "dlss5tool.amd_devtest_ui"],
    noarchive=False,
    optimize=0,
)
# Windows 10/11 provide UCRT as an operating-system component.  Some managed
# endpoints block copying ucrtbase.dll into application folders; relying on the
# supported OS copy also avoids shipping a stale system runtime.
a.binaries = [
    entry for entry in a.binaries
    if os.path.basename(entry[0]).lower() != "ucrtbase.dll"
]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DLSS5Tool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=os.path.join(project_root, "packaging/DLSS5Tool.version.txt"),
    icon=app_icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=f"DLSS5Tool-{APP_VERSION}",
)

# COLLECT puts DATA under _internal in onedir builds. This user-facing directory
# must instead sit beside the executable; copy only our instruction file.
release_mods = os.path.join(coll.name, "mods")
os.makedirs(release_mods, exist_ok=True)
shutil.copy2(os.path.join(project_root, "mods", "README.md"), os.path.join(release_mods, "README.md"))
