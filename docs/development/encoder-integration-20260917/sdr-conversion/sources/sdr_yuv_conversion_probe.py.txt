"""Exploratory GPU SDR conversion; NOT accepted for production or performance."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dlss5tool.encoding_contract import frame_encoding_contract

# Mathematical BT.601 limited-range coefficients at 15-bit precision. No
# FFmpeg source is embedded. Upstream coefficient/rounding behavior is a
# compatibility reference, not a license-free implementation to transplant.
Y_COEFF = tuple(round(x * 219 / 255 * 32768) for x in (.114, .587, .299))
U_COEFF = tuple(round(x * 224 / 255 * 32768) for x in (.500, -.331, -.169))
V_COEFF = tuple(round(x * 224 / 255 * 32768) for x in (-.081, -.419, .500))


def gpu_box_i420(bgr, method='round'):
    """Bounded hypothesis: fixed point and horizontal/vertical box sampling.

    Torch arithmetic stays on input device. This deliberately does NOT claim
    to reproduce swscale's scaler/filter dispatch, dithering or border policy.
    """
    import torch
    if bgr.device.type != 'cuda' or bgr.dtype != torch.uint8 or bgr.ndim != 3 or bgr.shape[2] != 3:
        raise ValueError('CUDA uint8 HWC BGR required')
    h, w, _ = bgr.shape
    ph, pw = (h+1)//2*2, (w+1)//2*2
    src = torch.zeros((ph, pw, 3), dtype=torch.int32, device=bgr.device)
    src[:h, :w] = bgr
    def matrix(pixels, coefficients):
        return sum(pixels[..., index] * value for index, value in enumerate(coefficients))
    y = (matrix(src,Y_COEFF) + 16*32768 + (16640 if method=='round' else 0)) >> 15
    horizontal = src[:, 0::2] + src[:, 1::2]
    chroma = []
    for coefficients in (U_COEFF,V_COEFF):
        if method=='round':
            row = (matrix(horizontal,coefficients) + 128*65536 + 512) >> 10
            chroma.append((row[0::2] + row[1::2] + 64) >> 7)
        else:
            block_sum = horizontal[0::2] + horizontal[1::2]
            if method=='rgb-floor':
                chroma.append((matrix(block_sum >> 2,coefficients) + 128*32768) >> 15)
            else:
                chroma.append((matrix(block_sum,coefficients) + 128*131072) >> 17)
    return torch.cat([plane.clamp(0,255).to(torch.uint8).reshape(-1) for plane in (y,*chroma)])


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def difference(np, actual, expected, w, h):
    report = {}
    start = 0
    for name, width, height in (('y',w,h),('u',w//2,h//2),('v',w//2,h//2)):
        count = width*height
        a = np.frombuffer(actual[start:start+count],np.uint8).astype(np.int16)
        b = np.frombuffer(expected[start:start+count],np.uint8).astype(np.int16)
        delta = a-b
        indices = np.flatnonzero(delta)
        report[name] = dict(mismatches=len(indices),samples=count,max_abs=int(np.max(np.abs(delta))),
            signed_sum=int(delta.sum()),first=[dict(x=int(i%width),y=int(i//width),actual=int(a[i]),expected=int(b[i]))
                                             for i in indices[:8]])
        start += count
    if start!=len(actual) or len(actual)!=len(expected):
        raise RuntimeError('Wrong plane size')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work',type=Path,required=True)
    parser.add_argument('--label',required=True)
    parser.add_argument('--method',choices=('round','floor','rgb-floor','fused'),default='round')
    args = parser.parse_args()
    work = args.work.resolve()
    output = work/(args.label+'.json')
    if not args.label.replace('-','').isalnum() or not (work/'TASK.md').is_file() or output.exists():
        parser.error('Fresh safe label and registered task required')
    os.environ['TEMP']=os.environ['TMP']=str(work)
    os.environ['CUDA_CACHE_DISABLE']='1'
    import numpy as np
    import torch
    from dlss5tool.video_export import find_ffmpeg
    ffmpeg = find_ffmpeg()
    report = dict(scope=__doc__,argv=sys.argv,script_sha256=digest(Path(__file__).read_bytes()),
                  coefficients=dict(y=Y_COEFF,u=U_COEFF,v=V_COEFF),cases=[])
    def convert(frame, scalar=False):
        h,w,_ = frame.shape
        contract = frame_encoding_contract(w,h,24,(w+1)//2*2,(h+1)//2*2)
        command = [ffmpeg,'-hide_banner','-loglevel','error']
        if scalar:command += ['-cpuflags','0']
        command += [*contract.input_args(),'-frames:v','1','-f','rawvideo','-pix_fmt','yuv420p','pipe:1']
        result = subprocess.run(command,input=frame.tobytes(),capture_output=True,timeout=30,
                                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:raise RuntimeError(result.stderr.decode(errors='replace'))
        return result.stdout
    try:
        for w,h in ((64,48),(320,180),(321,181)):
            rng = np.random.default_rng(20260917)
            yy,xx = np.indices((h,w))
            fixtures = {'black':np.zeros((h,w,3),np.uint8),
                        'white':np.full((h,w,3),255,np.uint8),
                        'ramp':np.stack((xx%256,yy%256,(xx+yy)%256),axis=-1).astype(np.uint8),
                        'random':rng.integers(0,256,(h,w,3),dtype=np.uint8)}
            fixtures['red-row'] = fixtures['black'].copy()
            fixtures['red-row'][h//2,:,2] = 255
            fixtures['red-checker'] = fixtures['black'].copy()
            fixtures['red-checker'][...,2] = ((xx+yy)%2*255).astype(np.uint8)
            for name,frame in fixtures.items():
                expected = convert(frame)
                scalar = convert(frame,True)
                variants=[]
                if args.method=='fused':
                    import ctypes as C
                    from dlss5tool.cuda_sdr_yuv import CudaSdrConverter
                    torch.empty(0,device='cuda')
                    read=C.WinDLL('nvcuda.dll').cuMemcpyDtoH_v2
                    read.argtypes=[C.c_void_p,C.c_uint64,C.c_size_t];read.restype=C.c_int
                    with CudaSdrConverter(work/'sdr-yuv.ptx',w,h) as converter:
                        for layout in ('bgr24','rgb24','bgra','rgba'):
                            rgb=frame if layout in ('bgr24','bgra') else frame[...,::-1]
                            if layout in ('bgra','rgba'):
                                rgb=np.concatenate((rgb,np.full((h,w,1),123,np.uint8)),axis=-1)
                            channels=rgb.shape[2]
                            for padding in (0,17):
                                pitch=w*channels+padding
                                packed=np.full((h,pitch),241,np.uint8)
                                packed[:,:w*channels]=rgb.reshape(h,w*channels)
                                gpu=torch.from_numpy(packed).cuda();torch.cuda.synchronize()
                                lease=converter.convert(gpu.data_ptr(),gpu.numel(),layout,0,pitch)
                                copied=C.create_string_buffer(lease.nbytes)
                                if read(copied,lease.pointer,lease.nbytes):raise RuntimeError('CUDA readback failed')
                                actual=copied.raw
                                variants.append(dict(layout=layout,padding=padding,equal=actual==expected,sha256=digest(actual)))
                    if not all(row['equal'] for row in variants):
                        raise RuntimeError(f'Fused layout/pitch mismatch: {variants}')
                else:
                    actual = gpu_box_i420(torch.from_numpy(frame).cuda(),args.method).cpu().numpy().tobytes()
                pw,ph = (w+1)//2*2,(h+1)//2*2
                row = dict(fixture=name,size=[w,h],input_sha256=digest(frame.tobytes()),
                    reference_sha256=digest(expected),gpu_sha256=digest(actual),
                    scalar_sha256=digest(scalar),gpu_equal=actual==expected,
                    scalar_equal=scalar==expected,gpu_difference=difference(np,actual,expected,pw,ph),
                    scalar_difference=difference(np,scalar,expected,pw,ph))
                if variants:row['variants']=variants
                report['cases'].append(row)
                print(json.dumps(dict(size=[w,h],fixture=name,gpu=row['gpu_equal'],scalar=row['scalar_equal'],
                    delta={k:(v['mismatches'],v['max_abs']) for k,v in row['gpu_difference'].items()})),flush=True)
        report.update(status='completed',accepted=all(row['gpu_equal'] for row in report['cases']))
    except BaseException as exc:
        import traceback
        report.update(status='failed',error=repr(exc),traceback=traceback.format_exc(),accepted=False)
        traceback.print_exc()
    finally:
        with output.open('x',encoding='utf-8') as stream:json.dump(report,stream,indent=2)
        print(f'Report: {output}',flush=True)
    return int(report['status']!='completed')


if __name__=='__main__':
    raise SystemExit(main())
