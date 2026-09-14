"""Run each public-NGX DLSSG synthetic case in a bounded child process.

This is validation infrastructure, not a production interpolation backend.
No disk DLL patching, game proxy injection, downloads or algorithm fallback.
Explicit --ada-compat permits the pinned research in-process adapter only.
"""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, default=ROOT / 'third_party/NVIDIA-DLSS/lib/Windows_x86_64/rel')
    parser.add_argument('--multipliers', nargs='+', type=int, default=[2, 3, 4])
    parser.add_argument('--formats', nargs='+', choices=['sdr', 'hdr', 'hdr-coded'], default=['sdr', 'hdr', 'hdr-coded'])
    parser.add_argument('--label', default='initial')
    parser.add_argument('--ada-compat', action='store_true')
    parser.add_argument('--preset-b', action='store_true')
    parser.add_argument('--shutdown', choices=['ngx', 'process'], default='ngx')
    args = parser.parse_args()
    task = args.task.resolve(strict=True)
    if not task.is_relative_to(ROOT / 'tmp') or not (task / 'TASK.md').is_file():
        parser.error('Use a registered task under repository tmp/')
    runtime = args.runtime.resolve(strict=True)
    if not re.fullmatch(r'[a-z0-9-]+', args.label):
        parser.error('Use a lowercase alphanumeric run label')
    dll = runtime / 'nvngx_dlssg.dll'
    with dll.open('rb') as handle:
        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
    if args.preset_b and not args.ada_compat:
        parser.error('--preset-b requires --ada-compat')
    if args.ada_compat and digest != '135eaf0733c1e37381a8c28abcf7a862404a54132b81787c04e35d09efc5e36f':
        parser.error('Research Ada route requires the pinned 310.7 DLL')
    report = {'runtime': str(dll), 'runtime_sha256': digest,
              'ada_compat': args.ada_compat, 'preset_b': args.preset_b, 'shutdown': args.shutdown,
              'scope': 'synthetic planar translation; not full video/HDR/product acceptance', 'cases': []}
    env = {**os.environ, 'TEMP': str(task), 'TMP': str(task), 'PYTHONDONTWRITEBYTECODE': '1'}
    for fmt in args.formats:
        for multiplier in args.multipliers:
            case = task / f'{args.label}-{fmt}-1080p-{multiplier}x'
            case.mkdir(exist_ok=False)
            command = [str(task / 'dlssg_probe.exe'), str(runtime), str(case), '1920', '1080', str(multiplier), fmt,
                       'ada-preset-b' if args.preset_b else 'ada-compat' if args.ada_compat else 'stock', args.shutdown]
            started = time.monotonic()
            try:
                result = subprocess.run(command, cwd=case, env=env, timeout=45, capture_output=True,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
                code, stdout, stderr = result.returncode, result.stdout, result.stderr
            except subprocess.TimeoutExpired as exc:
                code, stdout, stderr = 'timeout', exc.stdout or b'', exc.stderr or b''
            (case / 'stdout.log').write_bytes(stdout)
            (case / 'stderr.log').write_bytes(stderr)
            record = {'format': fmt, 'multiplier': multiplier, 'returncode': code,
                      'seconds': round(time.monotonic() - started, 3),
                      'stdout': stdout.decode('utf-8', errors='replace'),
                      'stderr_tail': stderr.decode('utf-8', errors='replace')[-3500:]}
            record['pixel_pass'] = 'synthetic_pixel_probe_pass=1' in record['stdout']
            record['temporal_pass'] = 'synthetic_temporal_probe_pass=1' in record['stdout']
            record['ngx_shutdown_pass'] = 'shutdown=0x00000001' in record['stdout']
            report['cases'].append(record)
            (task / f'{args.label}-report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
            print(json.dumps(record, ensure_ascii=False), flush=True)
    return 0 if all(c['returncode'] == 0 and c['pixel_pass'] and c['temporal_pass']
                    and (args.shutdown == 'process' or c['ngx_shutdown_pass'])
                    for c in report['cases']) else 1


if __name__ == '__main__':
    sys.exit(main())
