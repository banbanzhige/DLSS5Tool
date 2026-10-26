"""Compare HDR analysis LUTs with the frozen pre-upgrade NumPy oracle."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task',type=Path,required=True);parser.add_argument('--label',required=True)
    args=parser.parse_args()
    from scripts.build_vsr_cuda import safe,digest
    task=safe(args.task,ROOT/'tmp');target=task/(args.label+'.json')
    if not (task/'TASK.md').is_file() or not args.label.replace('-','').isalnum() or target.exists():raise ValueError('Fresh registered work required')
    original=subprocess.run(['git','show','HEAD:dlss5tool/guidance_color.py'],cwd=ROOT,check=True,capture_output=True).stdout
    oracle_path=task/'analysis-oracle.py'
    if oracle_path.exists() and oracle_path.read_bytes()!=original:raise ValueError('Oracle changed')
    oracle_path.write_bytes(original)
    spec=importlib.util.spec_from_file_location('hdr_analysis_oracle',oracle_path)
    oracle=importlib.util.module_from_spec(spec);spec.loader.exec_module(oracle)
    import numpy as np
    from dlss5tool import guidance_color as candidate
    from dlss5tool.frame_generation import scene_thumbnail
    from dlss5tool.memory_budget import memory_allocation
    report=dict(accepted=False,oracle_sha256=digest(oracle_path),candidate_sha256=digest(ROOT/'dlss5tool/guidance_color.py'),cases=[])
    try:
        for w,h in ((257,256),(322,182),(1920,1080),(3840,2160)):
          with memory_allocation(w*h*64,stage='hdr_analysis_probe'):
            indices=np.arange(w*h,dtype=np.uint32).reshape(h,w)
            bits=np.empty((h,w,4),np.uint16)
            bits[...,0]=indices%65536;bits[...,1]=(indices*13+17)%65536
            bits[...,2]=(65535-indices)%65536;bits[...,3]=0x3c00
            frame=bits.view(np.float16);frame[~np.isfinite(frame)]=0
            fixtures={'all-finite-half':frame}
            if w>=1920:
                codes=(indices%1024).astype(np.float32)/1023.0
                typical=np.empty_like(frame)
                typical[...,0]=codes;typical[...,1]=codes[::-1];typical[...,2]=codes[:,::-1];typical[...,3]=1
                fixtures={'vsr-10bit-codes':typical}
            for fixture,source in fixtures.items():
                for profile in ('hdr10_pq','hdr10_hlg','scrgb'):
                    for primaries in (('bt709',) if profile=='scrgb' else ('bt709','bt2020')):
                        settings=dict(frame_format='rgba16f',color_profile=profile,color_primaries=primaries)
                        old_times=[];new_times=[]
                        for repeat in range(3):
                            before=time.perf_counter();expected=oracle.analysis_rgba8(source,settings);old_times.append(time.perf_counter()-before)
                            before=time.perf_counter();actual=candidate.analysis_rgba8(source,settings);new_times.append(time.perf_counter()-before)
                            if not np.array_equal(expected,actual):
                                delta=actual.astype(np.int16)-expected.astype(np.int16)
                                raise AssertionError(f'Proxy mismatch {w}x{h} {profile} {primaries}: {np.count_nonzero(delta)}, max {np.abs(delta).max()}')
                        assert np.array_equal(scene_thumbnail(expected),scene_thumbnail(actual))
                        row=dict(size=[w,h],fixture=fixture,profile=profile,primaries=primaries,equal=True,
                            sha256=hashlib.sha256(actual.tobytes()).hexdigest(),old_median_ms=statistics.median(old_times)*1000,
                            new_median_ms=statistics.median(new_times)*1000)
                        report['cases'].append(row);print(json.dumps(row),flush=True)
        report['accepted']=True
    finally:target.write_text(json.dumps(report,indent=2),encoding='utf-8')
    return 0 if report['accepted'] else 1


if __name__=='__main__':raise SystemExit(main())
