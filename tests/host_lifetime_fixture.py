"""Owned, bounded subprocess trees for Windows DLSS lifetime regressions."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_dlss_host_process import FakeLive


def spawn_descendants():
    process = subprocess.Popen(
        [sys.executable, '-B', str(Path(__file__).resolve()), '--branch'],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    try:
        record = json.loads(process.stdout.readline())
    finally:
        process.stdout.close()
    return process, [record['pid'], record['leaf']]


class DescendantLive(FakeLive):
    def __init__(self, width, height, settings):
        super().__init__(width, height, settings)
        self.child, pids = spawn_descendants()
        self.guidance_info = {'fixture_pids': pids}
        record = settings.get('fixture_record')
        if record:
            Path(record).write_text(json.dumps(pids), encoding='utf-8')
        release = settings.get('fixture_release')
        if release:
            deadline = time.monotonic() + 15
            while not Path(release).exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError('fixture observer did not release initialization')
                time.sleep(.01)
        if settings.get('fixture_fail_init'):
            raise RuntimeError('fixture failed after spawning descendants')
        if settings.get('fixture_hang_init'):
            time.sleep(120)

    def close_guidance(self):
        if self.settings.get('fixture_hang_close'):
            time.sleep(120)
        # Deliberately leave both descendants running. The production lifetime
        # owner, rather than fixture cleanup, must retire them.


def vsr_worker(*args):
    """Exercise the production VSR entry with a Python stand-in for its DLL."""
    from dlss5tool import super_resolution as sr
    from types import SimpleNamespace
    child = None

    def initialize(*_):
        nonlocal child
        child, pids = spawn_descendants()
        return {'fixture_pids': pids}

    sr._load_host = lambda *_: SimpleNamespace(vsr_shutdown=lambda: None)
    sr._initialize_host = initialize
    sr._worker_main(*args)


def main():
    if '--controlled' in sys.argv:
        sys.stdin.readline()
        child, pids = spawn_descendants()
        print(json.dumps({'pids': [os.getpid(), *pids]}), flush=True)
        time.sleep(120)
        return
    if '--owner' in sys.argv:
        from dlss5tool.dlss_host_process import ProcessLive
        live = ProcessLive(8, 6, {'host_backend': 'v2'},
                           _live_factory=DescendantLive)
        print(json.dumps({'pids': [live._session._process.pid,
                                   *live.guidance_info['fixture_pids']]}), flush=True)
        sys.stdin.readline()
        os._exit(88)  # No finally/atexit: simulate abrupt GUI owner death.
    if '--branch' in sys.argv:
        leaf = subprocess.Popen(
            [sys.executable, '-B', str(Path(__file__).resolve()), '--leaf'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        try:
            record = json.loads(leaf.stdout.readline())
        finally:
            leaf.stdout.close()
        record = {'pid': os.getpid(), 'leaf': record['pid']}
    else:
        record = {'pid': os.getpid()}
    allocation = bytearray(16 * 1024**2)
    print(json.dumps(record), flush=True)
    time.sleep(120)
    assert allocation[0] == 0


if __name__ == '__main__':
    main()
