"""Stage a completed PyInstaller EXE without duplicating same-volume CUDA DLLs.

Consumes the current build's Analysis TOC, not an assumed old dependency set.
Hard links are independent directory entries; no existing files are modified.
"""
import argparse
import ast
import json
import os
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / 'tmp') or output.exists():
        parser.error('Use a new staging directory within this workspace tmp')
    analysis = ast.literal_eval((args.build / 'Analysis-00.toc').read_text(encoding='utf-8'))
    entries = analysis[15] + analysis[18]
    validated = []
    for name, source, kind in entries:
        if Path(name).name.lower() == 'ucrtbase.dll':
            continue  # same exclusion as GuidanceWorker.spec
        destination = (output / '_internal' / name).resolve()
        if not destination.is_relative_to(output / '_internal'):
            raise ValueError('Unsafe build destination')
        source = Path(source).resolve()
        if not source.is_file() or kind not in ('BINARY', 'EXTENSION', 'DATA'):
            raise ValueError((source, kind))
        validated.append((source, destination))
    output.mkdir(parents=True)
    shutil.copy2(args.build / 'guidance_worker.exe', output / 'guidance_worker.exe')
    for source, destination in validated:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            continue
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)
    import torch
    manifest = json.loads((ROOT / 'packaging/enhancement-contract.json').read_text(encoding='utf-8'))
    manifest.update(build='cuda' if torch.version.cuda else 'cpu', cuda=torch.version.cuda,
                    torch=torch.__version__, device_policy='explicit_cpu_only')
    (output / 'enhancement.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(f'Staged {len(validated)} dependency entries: {output}', flush=True)


if __name__ == '__main__':
    main()
