"""Exercise geometry negotiation in a frozen RAFT component against fresh sessions.

Reads an explicit component and existing weights; no downloads or installation.
Each resized session must keep its process and match a freshly initialized one.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    import numpy as np
    from dlss5tool.guidance_client import GuidanceSession
    from dlss5tool.guidance_transport import TRANSPORT
    from scripts.verify_p0_stability import checked_path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--component', type=Path, required=True)
    parser.add_argument('--weights', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    component, weights, report = map(checked_path, (args.component, args.weights, args.report))
    if report.exists() or not report.parent.is_dir():
        raise ValueError('Use a new report in a registered task/evidence directory')
    rows = []
    for transport, execution in ((TRANSPORT, 'serial'), (TRANSPORT, 'raft_streams'), ('pipe', 'serial')):
        settings = dict(guidance_mode=1, guidance_transport=transport,
                        guidance_execution=execution, guidance_flow_backend='raft',
                        guidance_device='cuda', guidance_edge=192,
                        guidance_flow_updates=3, guidance_cache_mb=0,
                        mods_directory=str(component.parent), guidance_flow_weights=str(weights))
        session = GuidanceSession(settings, 192, 128)
        try:
            pid = session._process.pid
            if session.info.get('resize_protocol') != 1:
                raise RuntimeError('Frozen component lacks geometry negotiation')
            for index, (width, height) in enumerate(((192, 128), (256, 144), (192, 128))):
                if index and not session.resize(width, height):
                    raise RuntimeError('Component declined resize')
                if session._process.pid != pid or session._process.poll() is not None:
                    raise RuntimeError('Resize replaced or lost the model process')
                y, x = np.indices((height, width))
                image = np.stack(((x*3+y)%256, (y*5+x)%256, (x*7+y*3)%256,
                                  np.full_like(x, 255)), axis=-1).astype(np.uint8)
                frames = (image, np.roll(image, 2, axis=1))
                actual = [session.process(frame, reset=(n == 0)) for n, frame in enumerate(frames)]
                fresh = GuidanceSession(settings, width, height)
                try:
                    expected = [fresh.process(frame, reset=(n == 0)) for n, frame in enumerate(frames)]
                finally:
                    fresh.close()
                for result, reference in zip(actual, expected):
                    if (not np.array_equal(result[0], reference[0])
                            or not np.array_equal(result[1], reference[1])
                            or result[2] != reference[2]):
                        raise RuntimeError('Resized flow/depth/reset differs from a fresh session')
                rows.append(dict(transport=transport, execution=execution, width=width,
                                 height=height, pid=pid, fresh_output_exact=True))
                print(json.dumps(rows[-1]), flush=True)
        finally:
            session.close()
        if session._process is not None:
            raise RuntimeError('Component did not retire')
    result = dict(passed=True, component=str(component),
                  worker_sha256=hashlib.sha256((component/'guidance_worker.exe').read_bytes()).hexdigest(),
                  weights_sha256=hashlib.sha256(weights.read_bytes()).hexdigest(), cases=rows)
    with report.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2)


if __name__ == '__main__':
    main()
