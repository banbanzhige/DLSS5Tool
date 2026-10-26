"""Run finite S4 real-GPU acceptance cases serially; reuse registered dependencies."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from scripts.build_vsr_cuda import safe
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--group', choices=('correctness', 'throughput', 'lifecycle'), required=True)
    parser.add_argument('--only', action='append', default=[])
    args = parser.parse_args()
    task = safe(args.task, ROOT/'tmp')
    if not (task/'TASK.md').is_file():
        raise ValueError('Registered task required')
    cases = []
    def cached(label, source, *, profile='hdr10_pq', frames=16, width=960, height=540,
               nr=False, fg=1, device=False, audit=True):
        values = ['--task', str(task), '--label', label, '--color-profile', profile,
            '--width', str(width), '--height', str(height), '--frames', str(frames),
            '--case', 'sr-nr' if nr else 'sr', '--nr-total', '1', '--multiplier', str(fg),
            '--cache-mib', '1024', '--private-limit-mib', '6144']
        if source: values += ['--source', str(task/source)]
        if device: values += ['--device-preview']
        if not audit: values += ['--no-frame-hashes']
        cases.append((label, 'profile_cached_export.py', values))
    def resident(label, source, *, profile='hdr10_pq', nr=False, fg=1, audit=True):
        values = ['--task', str(task), '--label', label, '--source', str(task/source),
                  '--color-profile', profile, '--multiplier', str(fg)]
        if nr: values += ['--enhance']
        if audit: values += ['--audit']
        else: values += ['--sample-gpu']
        cases.append((label, 'profile_resident_sr.py', values))
    if args.group == 'correctness':
        for fg in (3, 4):
            cached('control-hlg-fg'+str(fg), 'hlg-mild-audio.mp4', profile='hdr10_hlg', fg=fg)
            resident('resident-hlg-fg'+str(fg), 'hlg-mild-audio.mp4', profile='hdr10_hlg', fg=fg)
        cached('device-hlg-nr-fg-final', 'hlg-mild-audio.mp4', profile='hdr10_hlg', nr=True, fg=2, device=True)
        cached('control-pq-cut-fg2', 'pq-cuts.mp4', fg=2)
        resident('resident-pq-cut-fg2', 'pq-cuts.mp4', fg=2)
        cached('control-pq4k-nr-fg2', 'pq4k-mild.mp4', frames=8, width=1920, height=1080, nr=True, fg=2)
        resident('resident-pq4k-nr-fg2', 'pq4k-mild.mp4', nr=True, fg=2)
    elif args.group == 'throughput':
        cached('control-pq4k-32', None, frames=32, width=1920, height=1080, audit=False)
        resident('resident-pq4k-32', 'synthetic-1920x1080-32-hdr10_pq-vui.mp4', audit=False)
        cached('control-pq-96', 'pq-96.mp4', frames=96, audit=False)
        resident('resident-pq-96', 'pq-96.mp4', audit=False)
        cached('control-sdr-nr-fg96', None, profile='srgb', frames=96, nr=True, fg=2, audit=False)
        resident('resident-sdr-nr-fg96', 'synthetic-960x540-96.mp4', profile='srgb', nr=True, fg=2, audit=False)
    else:
        for name, source, nr in [('cancel-hdr-fg','hlg-mild-audio.mp4',True),
                                  ('cancel-sdr-fg','synthetic-960x540-96.mp4',True)]:
            resident(name,source,profile='hdr10_hlg' if name.startswith('cancel-hdr') else 'srgb',nr=nr,fg=2)
            cases[-1][2].extend(['--cancel-after','2'])
        for label, source, values in [('preview-demand','pq-96.mp4',['--multiplier','2','--demand-start','80']),
                                     ('preview-cancel','hlg-mild-audio.mp4',['--enhance','--multiplier','2','--cancel-after','1'])]:
            cases.append((label,'probe_device_preview.py',['--task',str(task),'--label',label,
                '--source',str(task/source),*values]))
        cases.append(('analysis-pinned','probe_gpu_analysis.py',['--task',str(task),'--label','analysis-pinned']))
    if args.only:
        cases = [row for row in cases if row[0] in args.only]
        if {row[0] for row in cases} != set(args.only):
            raise ValueError('Unknown case')
    rows = []
    for label, script, values in cases:
        command = [sys.executable,'-B',str(ROOT/'scripts'/script),*values]
        path = task/(label+'-invocation.json')
        if path.exists(): raise FileExistsError(path)
        started = time.perf_counter()
        result = subprocess.run(command,cwd=ROOT,capture_output=True,text=True,errors='replace',timeout=180,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        row = dict(label=label,command=command,returncode=result.returncode,
            elapsed_seconds=time.perf_counter()-started,stdout=result.stdout[-4000:],stderr=result.stderr[-6000:])
        path.write_text(json.dumps(row,indent=2),encoding='utf-8')
        rows.append(row)
        print(json.dumps({key:row[key] for key in ('label','returncode','elapsed_seconds','stderr') if row[key]}),flush=True)
    return 0 if all(row['returncode']==0 for row in rows) else 1


if __name__ == '__main__':
    raise SystemExit(main())
