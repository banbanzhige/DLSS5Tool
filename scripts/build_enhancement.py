"""Maintainer-only builder. End users extract beside the app; mods is included.

Run with a build environment containing pinned Torch/torchvision, OpenCV, NumPy,
einops and PyInstaller. --depth-source contains the architecture package.
The caller must supply its upstream license; weights are optional and external.
"""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--depth-source', type=Path, required=True)
    parser.add_argument('--depth-license', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--flow-weights', type=Path)
    parser.add_argument('--depth-weights', type=Path)
    parser.add_argument('--encoder', choices=['vits', 'vitb', 'vitl'], default='vitl')
    parser.add_argument('--weights-notice', type=Path,
                        help='Required with weights: provenance, license, allowed use')
    parser.add_argument('--zip', action='store_true')
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error('Output must be a NEW directory; existing packages are never overwritten')
    if not (args.depth_source / 'depth_anything_v2/dpt.py').is_file() or not args.depth_license.is_file():
        parser.error('Complete architecture source and upstream license required')
    if (args.flow_weights or args.depth_weights) and not (args.weights_notice and args.weights_notice.is_file()):
        parser.error('Weights require --weights-notice; do not publish without checking redistribution rights')
    for source in (args.flow_weights, args.depth_weights):
        if source and not source.is_file():
            parser.error(f'Missing weight: {source}')
    import torch
    versions = {name: importlib.metadata.version(name) for name in
                ('torch', 'torchvision', 'numpy', 'opencv-python', 'einops', 'pyinstaller')}
    env = dict(os.environ, DLSS5_DEPTH_SOURCE=str(args.depth_source.resolve()))
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm',
                    '--distpath', str(output / 'mods'), '--workpath', str(output / 'build'),
                    str(ROOT / 'packaging/GuidanceWorker.spec')], cwd=ROOT, env=env, check=True)
    component = output / 'mods/enhancement'
    (component / 'enhancement.json').write_text(json.dumps({
        'id': 'dlss5-guidance', 'protocol': 1,
        'flow_backends': ['raft', 'nvofa'],
        'architectures': ['raft_large', 'depth_anything_v2'],
        'build': 'cuda' if torch.version.cuda else 'cpu',
        'cuda': torch.version.cuda, 'dependencies': versions,
        'default_depth_encoder': args.encoder,
        'device_policy': 'explicit_cpu_only',
    }, indent=2), encoding='utf-8')
    licenses = component / 'licenses'
    licenses.mkdir()
    shutil.copy2(args.depth_license, licenses / 'DepthAnythingV2-LICENSE.txt')
    shutil.copy2(ROOT / 'LICENSE', licenses / 'DLSS5Tool-LICENSE.txt')
    shutil.copy2(ROOT / 'licenses/NVIDIA-Optical-Flow-Headers-LICENSE.txt', licenses / 'NVIDIA-Optical-Flow-Headers-LICENSE.txt')
    if any(path.name.lower() == 'nvofapi64.dll' for path in component.rglob('*.dll')):
        raise RuntimeError('Driver nvofapi64.dll must not be redistributed')
    shutil.copy2(ROOT / 'mods/README.md', component / 'README.md')
    shutil.copy2(ROOT / 'mods/README.md', output / 'mods/README.md')
    if args.weights_notice:
        shutil.copy2(args.weights_notice, licenses / 'MODEL-NOTICES.md')
    models = component / 'models'
    models.mkdir()
    for source, name in ((args.flow_weights, 'raft_large_C_T_SKHT_V2-ff5fadd5.pth'),
                         (args.depth_weights, f'depth_anything_v2_{args.encoder}.pth')):
        if source:
            shutil.copy2(source, models / name)
    if args.zip:
        shutil.make_archive(str(output / 'enhancement'), 'zip', output, 'mods')
    print(f'Extract/copy mods beside DLSS5Tool.exe: {output / "mods"}', flush=True)


if __name__ == '__main__':
    main()
