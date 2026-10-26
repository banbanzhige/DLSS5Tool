"""Compare HDR analysis LUTs with the frozen pre-upgrade NumPy oracle."""
import argparse
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

# Bind the last accepted pre-LUT source by immutable commit and exact bytes.
ORACLE_COMMIT = '22ee79c2368b6beb8d9eee01e9c7b4ee1e7ef711'
ORACLE_SOURCE = 'dlss5tool/guidance_color.py'
ORACLE_SHA256 = '3c80060a8edeb404be8f5703c1586a620240831142957cf0c5e6e400c03de6a5'
ORACLE_EOTF_SHA256 = {
    '_pq_eotf': '17f88e9f56fc3904c4c624b6207e55044ca06e72b109ba0b2a81809684dbd184',
    '_hlg_eotf': '108f43df1a8107708376794b4a1cbc18d6bdf5e280ef11ac25a30c786f20a9f9',
}
PROBE_SIZES = ((257,256),(322,182),(1920,1080),(3840,2160))


def validate_eotf_dependencies():
    # The historical module imports these two functions from video_export.
    # Reject a changed shared dependency instead of comparing it to itself.
    from dlss5tool import video_export
    for name, expected in ORACLE_EOTF_SHA256.items():
        try:
            source = inspect.getsource(getattr(video_export,name)).replace('\r\n','\n')
        except (OSError,TypeError) as error:
            raise ValueError('HDR oracle EOTF dependency source unavailable: '+name) from error
        if hashlib.sha256(source.encode('utf-8')).hexdigest() != expected:
            raise ValueError('HDR oracle EOTF dependency changed: '+name)


def load_oracle_source(candidate_path):
    """Return only the pinned pre-upgrade source, independent of the candidate."""
    original = subprocess.run(['git','show',f'{ORACLE_COMMIT}:{ORACLE_SOURCE}'],
        cwd=ROOT,check=True,capture_output=True).stdout
    if hashlib.sha256(original).hexdigest() != ORACLE_SHA256:
        raise ValueError('Pinned HDR analysis oracle hash mismatch')
    validate_eotf_dependencies()
    candidate = Path(candidate_path).read_text(encoding='utf-8-sig')
    if original.decode('utf-8-sig').splitlines() == candidate.splitlines():
        raise ValueError('HDR oracle matches candidate; independent pre-upgrade source required')
    return original


def validate_analysis_evidence(report, *, candidate_path=None):
    """Require accepted evidence bound to the known reference and current source."""
    if (report.get('accepted') is not True or report.get('oracle_commit') != ORACLE_COMMIT
            or report.get('oracle_source') != ORACLE_SOURCE or report.get('oracle_sha256') != ORACLE_SHA256
            or report.get('oracle_eotf_sha256') != ORACLE_EOTF_SHA256):
        raise ValueError('HDR analysis evidence requires the pinned independent oracle identity')
    candidate_path = Path(candidate_path) if candidate_path is not None else ROOT/ORACLE_SOURCE
    if report.get('candidate_sha256') != hashlib.sha256(candidate_path.read_bytes()).hexdigest():
        raise ValueError('HDR analysis candidate changed after verification')
    load_oracle_source(candidate_path)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task',type=Path,required=True);parser.add_argument('--label',required=True)
    args=parser.parse_args()
    from scripts.build_vsr_cuda import safe,digest
    task=safe(args.task,ROOT/'tmp');target=task/(args.label+'.json')
    if not (task/'TASK.md').is_file() or not args.label.replace('-','').isalnum() or target.exists():raise ValueError('Fresh registered work required')
    original=load_oracle_source(ROOT/ORACLE_SOURCE)
    oracle_path=safe(task/'analysis-oracle.py',task)
    if oracle_path.exists() and oracle_path.read_bytes()!=original:raise ValueError('Oracle changed')
    oracle_path.write_bytes(original)
    spec=importlib.util.spec_from_file_location('hdr_analysis_oracle',oracle_path)
    oracle=importlib.util.module_from_spec(spec);spec.loader.exec_module(oracle)
    import numpy as np
    from dlss5tool import guidance_color as candidate
    from dlss5tool.frame_generation import scene_thumbnail
    from dlss5tool.memory_budget import memory_allocation
    report=dict(accepted=False,oracle_commit=ORACLE_COMMIT,oracle_source=ORACLE_SOURCE,
        oracle_sha256=digest(oracle_path),oracle_eotf_sha256=dict(ORACLE_EOTF_SHA256),
        candidate_sha256=digest(ROOT/ORACLE_SOURCE),cases=[])
    try:
        for w,h in PROBE_SIZES:
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
