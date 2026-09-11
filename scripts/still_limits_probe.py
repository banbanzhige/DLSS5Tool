"""Exercise the application's still-image policy with flow left enabled."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    import numpy as np
    from dlss5tool.gui import App, _read_image_bgr
    from dlss5tool.dlss_host_process import ProcessLive
    from dlss5tool.super_resolution import query_gpu_memory
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    frame = _read_image_bgr(str(args.source))
    if frame is None:
        raise ValueError('Cannot read probe source')
    query_gpu_memory()
    app = App.__new__(App)
    app._live_lock = threading.RLock()
    logs, plans, lives = [], [], []
    app.logln = logs.append
    def ensure(width, height, settings):
        plans.append(dict(settings))
        live = ProcessLive(width, height, settings)
        lives.append(live)
        return live
    app._ensure_live = ensure
    settings = {'guidance_mode': 1, 'host_backend': 'v2', 'super_resolution_scale': 1,
                'host_auto_fallback': False, 'host_in_flight': 1, 'host_submission': 'merged',
                'host_zero_fast_path': True, 'local_tone': 1.0, 'local_struct': 1.0}
    start = time.perf_counter()
    try:
        result = app._process_still_image(frame, settings)
        assert result is not None and result.shape == frame.shape and result.dtype == frame.dtype
        assert plans[-1]['guidance_mode'] == 0 and settings['guidance_mode'] == 1
        assert not lives[-1].guidance_info
        first_hash = hashlib.sha256(memoryview(np.ascontiguousarray(result))).hexdigest()
        # Release app-side full output before repeating, keeping this probe bounded.
        del result
        lives[-1].close()
        repeated = app._process_still_image(frame, settings)
        repeat_hash = hashlib.sha256(memoryview(np.ascontiguousarray(repeated))).hexdigest()
        assert first_hash == repeat_hash
        report = {'ok': True, 'size': list(frame.shape[:2][::-1]), 'settings_preserved': True,
                  'no_guidance_worker': True, 'repeated_equal': True, 'hash': first_hash,
                  'plan': plans[-1], 'logs': logs, 'seconds': time.perf_counter() - start}
        (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False))
    finally:
        for live in lives:
            live.close()


if __name__ == '__main__':
    main()
