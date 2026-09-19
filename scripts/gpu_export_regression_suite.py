"""Run relevant test modules in isolated processes (Tk lifecycle is thread-bound)."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--work',type=Path,required=True)
    parser.add_argument('--label',default='regression');parser.add_argument('--module',action='append')
    args=parser.parse_args();work=args.work.resolve();dest=work/(args.label+'.json')
    if not (work/'TASK.md').is_file() or dest.exists():parser.error('Fresh registered work required')
    env={**os.environ,'TEMP':str(work),'TMP':str(work),'PYTHONDONTWRITEBYTECODE':'1'}
    modules=('test_gpu_export_routing','test_gpu_export_integration','test_cuda_sdr_yuv','test_nvenc_yuv',
        'test_encoding_contract','test_hdr_pipeline','test_video_encoding','test_video_encoder_selection',
        'test_frame_generation','test_render_cache','test_dlss_host_process','test_gpu_adapter_selection',
        'test_guidance_export','test_shared_cache_budget','test_preview_cache_clear','test_export_queue')
    if args.module:
        if any(name not in modules for name in args.module):parser.error('Unknown module')
        modules=tuple(args.module)
    report=dict(scope=__doc__,modules=[])
    for module in modules:
        command=[sys.executable,'-B','-m','pytest','-q','-p','no:cacheprovider','--basetemp',str(work/(args.label+'-pytest-'+module)),
                 str(ROOT/'tests'/(module+'.py'))]
        start=time.monotonic()
        try:
            result=subprocess.run(command,capture_output=True,timeout=90,env=env,cwd=ROOT,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            row=dict(module=module,returncode=result.returncode,seconds=time.monotonic()-start,
                stdout=result.stdout.decode(errors='replace')[-8000:],stderr=result.stderr.decode(errors='replace')[-4000:])
        except subprocess.TimeoutExpired:row=dict(module=module,returncode=-1,error='timeout')
        report['modules'].append(row);print(json.dumps(row),flush=True)
    report['status']='passed' if all(r['returncode']==0 for r in report['modules']) else 'failed'
    with dest.open('x',encoding='utf-8') as out:json.dump(report,out,ensure_ascii=False,indent=2)
    return int(report['status']!='passed')


if __name__=='__main__':raise SystemExit(main())
