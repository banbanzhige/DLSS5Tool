"""Verify/download a known NR runtime and optionally process an isolated test frame."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from dlss5tool import dlss_engine, dlss_runtime
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mods-directory', required=True, type=Path)
    parser.add_argument('--profile', choices=('auto', 'bundled', *dlss_runtime.PROFILES), default='auto')
    parser.add_argument('--download-only', action='store_true')
    parser.add_argument('--test', action='store_true')
    args = parser.parse_args()
    selection = '' if args.profile == 'auto' else '__bundled__' if args.profile == 'bundled' else f'__{args.profile}__'
    settings = {'mods_directory': str(args.mods_directory.resolve()), 'dlss_runtime': selection,
                'host_backend': 'v2', 'render_gpu': 'auto', 'guidance_mode': 0}
    adapters = [] if args.download_only else dlss_engine.available_render_adapters()
    info = dlss_runtime.ensure_runtime(settings, adapter=dlss_runtime.selected_adapter(settings, adapters))
    result = {'selection': args.profile, 'runtime': info,
              'sha256': dlss_runtime.file_hash(info['path']), 'adapters': adapters}
    if args.test:
        result['test'] = dlss_runtime.test_runtime(settings)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    dlss_engine.close_adapter_probe()


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    from dlss5tool.adapter_probe import dispatch
    if not dispatch():
        main()
