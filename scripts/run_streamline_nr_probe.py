"""Run a no-inference SL capability probe with a hard process timeout.

Usage: python scripts/run_streamline_nr_probe.py EXE PLUGINS MODEL_DIR LOG_DIR
LOG_DIR must not exist. No DLL copies, registry edits or runtime patches.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    if len(sys.argv) != 5:
        raise SystemExit(__doc__)
    exe, plugins, model, logs = (Path(p).resolve() for p in sys.argv[1:])
    files = [exe, *(plugins / n for n in
             ('sl.interposer.dll', 'sl.common.dll', 'sl.dlss_nr.dll')),
             model / 'nvngx_dlssnr.dll']
    identity = {}
    for p in files:
        with p.open('rb') as stream:
            identity[str(p)] = hashlib.file_digest(stream, 'sha256').hexdigest()
    logs.mkdir(parents=True, exist_ok=False)
    report = {'scope': 'capability-only; no NR options/evaluation', 'files': identity,
              'timeout_seconds': 45}
    env = dict(os.environ, TEMP=str(logs), TMP=str(logs))
    try:
        result = subprocess.run([str(exe), str(plugins), str(model), str(logs)],
                                cwd=logs, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=45, creationflags=subprocess.CREATE_NO_WINDOW, env=env)
        output = result.stdout
        report.update(returncode=result.returncode, timed_out=False)
    except subprocess.TimeoutExpired as error:
        output = error.stdout or b''
        report.update(returncode=None, timed_out=True)
    (logs / 'console.log').write_bytes(output)
    (logs / 'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    decoded = output.decode('utf-8', errors='replace')
    for line in decoded.splitlines():
        if line.startswith(('SIGNATURE', 'CONFIG', 'RESULT', 'MODULE', 'LOADED',
                            'REQUIREMENTS', 'ADAPTER', 'VERSION', 'OPTIONS_OWNER',
                            'NO_INFERENCE', 'SHUTDOWN', 'DONE', 'FAIL')):
            print(line)
    print(json.dumps({'log_dir': str(logs), 'returncode': report['returncode'],
                      'timed_out': report['timed_out']}))
    # Successful process exit means a completed query, not NR support.
    # See the RESULT supported-after/loaded-after/find-options records.
    return 124 if report['timed_out'] else (0 if report['returncode'] == 0 else 1)


if __name__ == '__main__':
    raise SystemExit(main())
