"""Exact GPU analysis RGB/gray/thumbnail gate, including all 24-bit RGB colors."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from scripts.build_vsr_cuda import safe, digest
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True, type=Path)
    parser.add_argument('--label', required=True)
    parser.add_argument('--worker', action='store_true')
    args = parser.parse_args()
    task = safe(args.task, ROOT/'tmp')
    output = task/(args.label+'.json')
    if not (task/'TASK.md').is_file() or output.exists() or not args.label.replace('-', '').isalnum():
        raise ValueError('Fresh registered evidence required')
    if not args.worker:
        from dlss5tool.process_job import KillOnCloseJob
        from scripts.probe_resident_decode import process_record
        process = subprocess.Popen([sys._base_executable,'-B',str(Path(__file__).resolve()),*sys.argv[1:],'--worker'],
            cwd=ROOT,env=dict(os.environ,PYTHONPATH=str(Path(sys.prefix)/'Lib/site-packages'),PYTHONDONTWRITEBYTECODE='1'),
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
            creationflags=subprocess.CREATE_NO_WINDOW)
        job = KillOnCloseJob(process)
        try:
            stdout,stderr=process.communicate('GO\n',timeout=90)
            record=dict(returncode=process.returncode,stdout=stdout[-2000:],stderr=stderr[-4000:])
        except subprocess.TimeoutExpired:
            record=dict(returncode='timeout')
        finally:
            job.close()
            if process.poll() is None:process.kill();process.wait(timeout=5)
            record['after_retirement']=process_record(process.pid)
        (task/(args.label+'-process.json')).write_text(json.dumps(record,indent=2),encoding='utf-8')
        print(json.dumps(record));return 0 if record['returncode']==0 else 1
    if sys.stdin.readline().strip()!='GO':raise RuntimeError('Owned Job required')
    import cv2
    import numpy as np
    from dlss5tool.cuda_session import CudaSession
    from dlss5tool.cuda_frame import CudaImage,FrameTiming
    from dlss5tool.cuda_analysis import CudaAnalysis
    from dlss5tool.guidance_color import analysis_rgba8
    from dlss5tool.frame_generation import scene_thumbnail
    report=dict(accepted=False,cases=[],tables_sha256=digest(task/'analysis-tables.npz'),
                ptx_sha256=digest(task/'analysis-v2.ptx'),numpy=np.__version__,opencv=cv2.__version__)
    from dlss5tool.analysis_quantization import cpu_fingerprint
    report['analysis_cpu'] = cpu_fingerprint()
    with CudaSession() as cuda:
        def run(pixels,profile,primaries,fixture):
            height,width=pixels.shape[:2]
            with CudaImage(cuda,width,height,layout='rgba8' if profile=='srgb' else 'rgba16f',color_profile=profile) as image:
                analyzer=CudaAnalysis(cuda,width,height,task/'analysis-v2.ptx',task/'analysis-tables.npz',primaries=primaries)
                try:
                    config=dict(frame_format=image.layout,color_profile=profile,color_primaries=primaries)
                    expected=analysis_rgba8(pixels,config)
                    with image.upload(pixels,FrameTiming(0)) as source, analyzer.convert(source) as gray:
                        rgb=analyzer.rgba_snapshot() if profile!='srgb' else pixels
                        actual_gray=analyzer._gray.download(gray)[...,0]
                        expected_gray=cv2.cvtColor(expected[...,:3],cv2.COLOR_RGB2GRAY)
                        expected_gray=cv2.copyMakeBorder(expected_gray,0,analyzer.fh-height,0,analyzer.fw-width,cv2.BORDER_REPLICATE)
                        thumbnail=analyzer.thumbnail(gray)
                    bad=np.argwhere(rgb!=expected)
                    row=dict(size=[width,height],profile=profile,primaries=primaries,fixture=fixture,
                        rgb_equal=not bad.size,rgb_mismatches=int(np.count_nonzero(rgb!=expected)),
                        gray_equal=bool(np.array_equal(actual_gray,expected_gray)),
                        gray_mismatches=int(np.count_nonzero(actual_gray!=expected_gray)),
                        thumbnail_equal=bool(np.array_equal(thumbnail,scene_thumbnail(expected))),
                        rgba_sha256=hashlib.sha256(memoryview(rgb)).hexdigest(),
                        first=[dict(position=p.tolist(),actual=int(rgb[tuple(p)]),expected=int(expected[tuple(p)])) for p in bad[:4]])
                    report['cases'].append(row)
                finally:analyzer.close()
        bits=np.arange(65536,dtype=np.uint16)
        bits=bits[(bits&0x7c00)!=0x7c00]
        indices=np.arange(257*256).reshape(256,257)
        pixels=np.empty((256,257,4),np.float16)
        for channel in range(3):pixels[...,channel]=bits[(indices*(channel*2+1)+channel*77)%len(bits)].view(np.float16)
        pixels[...,3]=np.nan
        for profile in ('hdr10_pq','hdr10_hlg'):
            for primaries in ('bt709','bt2020'):run(pixels,profile,primaries,'finite-half')
        for width,height in ((1920,1080),(3840,2160)):
            rng=np.random.default_rng(20261026)
            pixels=rng.uniform(-.1,1.1,(height,width,4)).astype(np.float16);pixels[...,3]=1
            for profile in ('hdr10_pq','hdr10_hlg'):run(pixels,profile,'bt2020','random')
        for index in range(4):
            codes=np.arange(index*4096*1024,(index+1)*4096*1024,dtype=np.uint32).reshape(1024,4096)
            pixels=np.empty((1024,4096,4),np.uint8)
            for channel in range(3):pixels[...,channel]=(codes>>(8*channel))&255
            pixels[...,3]=255
            run(pixels,'srgb','bt709','all-rgb24-'+str(index))
    report['accepted']=all(row['rgb_equal'] and row['gray_equal'] and row['thumbnail_equal'] for row in report['cases'])
    output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(dict(accepted=report['accepted'],cases=[(row['profile'],row['size'],row['rgb_mismatches'],row['gray_mismatches']) for row in report['cases']])))
    return 0 if report['accepted'] else 1


if __name__=='__main__':raise SystemExit(main())
