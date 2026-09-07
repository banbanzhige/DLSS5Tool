# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the portable Windows release."""

import os
import sys

from PyInstaller.utils.hooks import collect_all


project_root = os.path.abspath(SPECPATH)
sys.path.insert(0, project_root)
from app_version import APP_VERSION


datas = []
binaries = []
hiddenimports = []

for package in ("tkinterdnd2", "imageio_ffmpeg"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

for filename, required in (
    ("dlssnr_host_v2.dll", True),
    ("nvngx_dlssnr.dll", True),
    ("dlssnr_host.dll", False),
    ("vsr_host.dll", True),
    ("nvngx_vsr.dll", True),
):
    source = os.path.join(project_root, filename)
    if os.path.isfile(source):
        binaries.append((source, "."))
    elif required:
        raise SystemExit(f"Missing required runtime file: {source}")

for filename in ("LICENSE", "README.md", "THIRD_PARTY_NOTICES.md"):
    source = os.path.join(project_root, filename)
    if os.path.isfile(source):
        datas.append((source, "."))

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
    excludes=[],
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
    version=os.path.join(project_root, "DLSS5Tool.version.txt"),
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
