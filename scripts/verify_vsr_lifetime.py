"""Verify real VSR job retirement with small synthetic frames on Windows."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True,
                        help='Small JSON report in an existing registered task directory')
    args = parser.parse_args()
    if not args.output.parent.is_dir() or args.output.exists():
        parser.error('--output needs an existing parent and must not overwrite a file')

    import numpy as np
    from multiprocessing import shared_memory
    from dlss5tool.super_resolution import ProcessSuperResolution
    from dlss5tool.memory_budget import fresh_system_memory
    records = []
    source = np.empty((360, 640, 4), np.uint8)
    source[..., 0] = np.arange(640)[None, :] % 256
    source[..., 1] = np.arange(360)[:, None] % 256
    source[..., 2] = 96
    source[..., 3] = 255
    before = fresh_system_memory()
    for cycle in range(3):
        live = ProcessSuperResolution(640, 360, 2)
        names = [live._input_memory.name, live._output_memory.name]
        record = dict(cycle=cycle, pid=live._process.pid,
                      job_armed=live._job is not None, adapter=live.adapter_info)
        try:
            for _ in range(3):
                output = live.process(source)
                assert output.shape == (720, 1280, 4) and output.dtype == np.uint8
            record['output_sha256'] = hashlib.sha256(memoryview(output)).hexdigest()
        finally:
            live.close()
        assert live._job is live._process is None
        for name in names:
            try:
                unexpected = shared_memory.SharedMemory(name=name)
            except FileNotFoundError:
                continue
            unexpected.close()
            raise AssertionError('Shared frame still exists after retirement')
        record['retired_and_shared_frames_released'] = True
        records.append(record)
    result = dict(passed=True, cycles=3, frames=9, records=records,
                  memory_before=before, memory_after=fresh_system_memory())
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
