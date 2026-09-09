"""Torch-free external worker for real Windows pipe/shared-memory tests."""
import argparse
import json
from multiprocessing.connection import Client
import threading

import numpy as np

import guidance_worker


class FakeModels:
    device = 'cuda'
    device_name = 'Fixture GPU'
    np = np
    last_metrics = {'inference_ms': 0}

    def __init__(self, settings):
        self.settings = settings

    def process(self, rgba, reset, outputs=None):
        if self.settings.get('fixture_error'):
            raise RuntimeError('fixture inference failure')
        h, w = rgba.shape[:2]
        mv, dp = outputs if outputs is not None else (np.empty((h, w, 2), np.float32), np.empty((h, w), np.float32))
        mv[..., 0] = rgba[..., 0] * 0.5
        mv[..., 1] = -rgba[..., 1].astype(np.float32)
        dp[:] = rgba[..., 2] / np.float32(255)
        if reset:
            mv.fill(0)
        if self.settings.get('fixture_nonfinite'):
            dp[0, 0] = np.nan
        return mv, dp, bool(reset)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--address')
    parser.add_argument('--token')
    parser.add_argument('--parent', type=int)
    parser.add_argument('--legacy', action='store_true')
    args = parser.parse_args()
    threading.Thread(target=guidance_worker.watch_parent, args=(args.parent,), daemon=True).start()
    conn = Client(args.address, family='AF_PIPE', authkey=bytes.fromhex(args.token))
    try:
        settings = json.loads(conn.recv_bytes())
        if args.legacy:
            # Original v1 behavior: ignore descriptor, do not acknowledge transport.
            model = FakeModels(settings)
            conn.send_bytes(json.dumps({'ok': True, 'protocol': 1, 'device': 'cuda'}).encode())
            while True:
                request = json.loads(conn.recv_bytes())
                frame = np.frombuffer(conn.recv_bytes(), np.uint8).reshape(settings['height'], settings['width'], 4)
                mv, dp, reset = model.process(frame, request['reset'])
                conn.send_bytes(json.dumps({'ok': True, 'reset': reset}).encode())
                conn.send_bytes(mv.tobytes())
                conn.send_bytes(dp.tobytes())
        else:
            guidance_worker.serve(conn, settings, FakeModels)
    except (EOFError, BrokenPipeError):
        pass
    except Exception as exc:
        conn.send_bytes(json.dumps({'ok': False, 'error': str(exc)}).encode())
    finally:
        conn.close()


if __name__ == '__main__':
    main()
