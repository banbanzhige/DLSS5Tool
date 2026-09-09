# Separate onedir component. Build with the desired CPU/CUDA Torch environment.
import os
import shutil
import json
import sys
from pathlib import Path
from PyInstaller.utils.hooks import copy_metadata

root = Path(SPECPATH)
depth_source = Path(os.environ['DLSS5_DEPTH_SOURCE']).resolve()
if not (depth_source / 'depth_anything_v2/dpt.py').is_file():
    raise SystemExit('DLSS5_DEPTH_SOURCE must contain the complete depth_anything_v2 package')
sys.path.insert(0, str(depth_source))
datas = []
for package in ('torch', 'torchvision', 'numpy', 'opencv-python', 'einops'):
    datas += copy_metadata(package)
a = Analysis([str(root / 'guidance_worker.py')], pathex=[str(root), str(depth_source)],
             binaries=[], datas=datas, hiddenimports=['depth_anything_v2.dpt'],
             excludes=['tkinter', 'pytest', 'matplotlib', 'IPython', 'tensorboard'],
             noarchive=False)
a.binaries = [entry for entry in a.binaries if Path(entry[0]).name.lower() != 'ucrtbase.dll']
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='guidance_worker',
          console=True, strip=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name='enhancement', strip=False, upx=False)
import torch
manifest = json.loads((root / 'enhancement-contract.json').read_text(encoding='utf-8'))
manifest.update(build='cuda' if torch.version.cuda else 'cpu', cuda=torch.version.cuda,
                torch=torch.__version__, device_policy='explicit_cpu_only')
(Path(coll.name) / 'enhancement.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
