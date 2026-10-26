"""Freeze exact half EOTF and linear-to-byte boundaries for GPU analysis."""
import argparse
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from scripts.build_vsr_cuda import safe, digest
    from dlss5tool.guidance_color import _half_hdr_linear_table, _BT2020_TO_709
    from dlss5tool.analysis_quantization import thresholds, cpu_fingerprint
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True, type=Path)
    args = parser.parse_args()
    task = safe(args.task, ROOT/'tmp')
    target = task/'analysis-tables.npz'
    if not (task/'TASK.md').is_file() or target.exists(): raise ValueError('Fresh registered tables required')
    t32, k32, v32 = thresholds(np.float32)
    t64, k64, v64 = thresholds(np.float64)
    values = dict(pq=_half_hdr_linear_table('hdr10_pq'), hlg=_half_hdr_linear_table('hdr10_hlg'),
                  threshold32=t32, threshold64=t64, correction32_keys=k32, correction32_values=v32,
                  correction64_keys=k64, correction64_values=v64, matrix=_BT2020_TO_709)
    with target.open('xb') as output: np.savez(output, **values)
    report = dict(numpy=np.__version__, sha256=digest(target), bytes=target.stat().st_size,
        analysis_cpu=cpu_fingerprint(),
        fields={key:dict(shape=list(value.shape), dtype=str(value.dtype)) for key,value in values.items()},
        source_sha256={name:digest(ROOT/name) for name in ('dlss5tool/guidance_color.py',
            'dlss5tool/analysis_quantization.py', 'scripts/build_analysis_tables.py')})
    (task/'analysis-tables-build.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__': main()
