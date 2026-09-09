# -*- mode: python ; coding: utf-8 -*-
"""A separate developer handoff build; never bundles user DLSS-NR components."""
import hashlib
import json
import os

project_root = os.path.dirname(os.path.abspath(SPECPATH))
probe = os.path.join(project_root, "build", "amd_probe", "amd_probe.exe")
fsr = os.path.join(project_root, "third_party", "FidelityFX-1.1.4", "PrebuiltSignedDLL", "amd_fidelityfx_dx12.dll")
fsr_license = os.path.join(project_root, "third_party", "FidelityFX-1.1.4", "LICENSE.txt")
for path in (probe, fsr, fsr_license):
    if not os.path.isfile(path):
        raise SystemExit("Missing developer test component: " + path)
manifest_path = os.path.join(project_root, "build", "amd_probe", "native-manifest.json")
with open(manifest_path, "w", encoding="utf-8") as handle:
    manifest = {}
    for path in (probe, fsr):
        with open(path, "rb") as binary:
            manifest[os.path.basename(path)] = hashlib.sha256(binary.read()).hexdigest()
    json.dump(manifest, handle, indent=2)

a = Analysis(
    [os.path.join(project_root, "dlss5tool/amd_devtest.py")], pathex=[project_root],
    # Keep native images as data: they are explicitly started/loaded by the
    # disposable child. This avoids PyInstaller treating them as Python DLLs.
    datas=[(probe, "native"), (fsr, "native"), (fsr_license, "licenses/fidelityfx"),
           (manifest_path, "."), (os.path.join(project_root, "assets", "app.ico"), "assets")],
    binaries=[], hiddenimports=["dlss5tool.amd_devtest_ui"],
    excludes=["cv2", "imageio_ffmpeg", "tkinterdnd2", "pytest", "torch", "scipy", "pandas"],
    noarchive=False, optimize=0,
)
a.binaries = [entry for entry in a.binaries if os.path.basename(entry[0]).lower() != "ucrtbase.dll"]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="AMD-DevTest", debug=False,
          strip=False, upx=False, console=False, disable_windowed_traceback=False,
          icon=os.path.join(project_root, "assets", "app.ico"))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="DLSS5Tool-AMD-dev1")
