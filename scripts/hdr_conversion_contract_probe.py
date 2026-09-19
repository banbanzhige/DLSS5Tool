"""Bounded HDR production conversion characterization; no default changes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from dlss5tool.encoding_contract import frame_encoding_contract


def convert(ffmpeg,frame,transfer='smpte2084',pixel_format='p010le',verbose=False):
    import numpy as np
    h,w,_=frame.shape
    contract=frame_encoding_contract(w,h,24,w,h,False,
        dict(color_transfer=transfer,color_primaries='bt2020',color_space='bt2020nc'))
    args=contract.input_args()
    args[-1]=args[-1].replace('format=p010le','format='+pixel_format)
    wire=np.rint(np.clip(frame.astype(np.float32),0,1)*65535).astype('<u2').tobytes()
    command=[ffmpeg,'-hide_banner','-loglevel','verbose' if verbose else 'error',*args,
        '-frames:v','1','-pix_fmt',pixel_format,'-f','rawvideo','pipe:1']
    result=subprocess.run(command,input=wire,capture_output=True,timeout=30,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:raise RuntimeError(result.stderr.decode(errors='replace'))
    return result.stdout,result.stderr.decode(errors='replace')


def matrix(frame):
    import numpy as np
    rgb=np.rint(np.clip(frame.astype(np.float32),0,1)*65535).astype(np.float32)[...,:3]*np.float32(1/65535)
    coeff=np.array([[.2627,.678,.0593],[-.2627/(2*(1-.0593)),-.678/(2*(1-.0593)),.5],
        [.5,-.678/(2*(1-.2627)),-.0593/(2*(1-.2627))]],np.float32)
    output=[]
    for row in coeff:
        value=rgb[...,0]*row[0]
        for index in (1,2):value=(rgb[...,index].astype(np.float64)*float(row[index])+value.astype(np.float64)).astype(np.float32)
        output.append(value)
    return output


def fixed_matrix(frame):
    import numpy as np
    # Invert the documented fixed-point BT2020 YUV->RGB matrix, expressed in
    # code-value units. Independent numeric derivation; no upstream code copied.
    inverse=np.array([[int(65536*255/219),0,110013],
        [int(65536*255/219),-12277,-42626],
        [int(65536*255/219),140363,0]],np.float64)/65536
    coefficients=np.rint(np.linalg.inv(inverse)*32768).astype(np.int64)
    rgb=np.rint(np.clip(frame.astype(np.float32),0,1)*65535).astype(np.int64)[...,:3]
    planes=[(sum(rgb[...,i]*int(c) for i,c in enumerate(row))+offset*256*32768+16384)>>15
            for row,offset in zip(coefficients,(16,128,128))]
    return planes,coefficients.tolist()


def cubic_weights(size,precision):
    import numpy as np
    table=np.zeros((size//2,size),np.int64)
    for output in range(size//2):
        weights=np.zeros(size,np.float64)
        for offset in range(-3,5):
            distance=abs((offset-.5)/2)
            weight=((1.4*distance**3-2.4*distance**2+1) if distance<1 else
                    (-.6*distance**3+3*distance**2-4.8*distance+2.4))/2
            weights[min(max(2*output+offset,0),size-1)]+=weight
        # Preserve unity gain with error distributed across coefficients.
        cumulative=np.rint(np.cumsum(weights)*precision).astype(np.int64)
        table[output]=np.diff(np.concatenate(([0],cumulative)))
    return table


def fixed_chroma(planes):
    import numpy as np
    h,w=planes[0].shape
    horizontal=cubic_weights(w,16384);vertical=cubic_weights(h,4096)
    outputs=[]
    for plane in planes[1:]:
        rows=np.minimum(((plane&65535)@horizontal.T)>>15,32767)
        outputs.append(np.clip((vertical@rows+65536)>>17,0,1023))
    return np.stack(outputs,axis=-1)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--work',type=Path,required=True)
    parser.add_argument('--label',required=True);args=parser.parse_args();work=args.work.resolve()
    dest=work/(args.label+'.json')
    if not (work/'TASK.md').is_file() or dest.exists() or not args.label.replace('-','').isalnum():parser.error('Fresh registered work required')
    os.environ['TEMP']=os.environ['TMP']=str(work)
    import numpy as np
    from dlss5tool.video_export import find_ffmpeg
    ffmpeg=find_ffmpeg();w,h=32,16
    frame=np.zeros((h,w,4),np.float16);frame[...,3]=1
    frames=dict(black=frame.copy(),white=np.ones_like(frame),random=np.random.default_rng(916).random((h,w,4)).astype(np.float16))
    for x,y in ((0,0),(6,6),(7,7)):
        image=frame.copy();image[y,x,0]=1;frames[f'impulse-{x}-{y}']=image
    report=dict(scope=__doc__,source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),cases=[])
    for name,image in frames.items():
        expected,log=convert(ffmpeg,image,verbose=True)
        p010=np.frombuffer(expected,'<u2')>>6
        y=p010[:w*h].reshape(h,w);uv=p010[w*h:].reshape(h//2,w//2,2)
        floating=matrix(image)
        fixed,coefficients=fixed_matrix(image)
        candidate_y=np.rint((floating[0].astype(np.float64)*876+64).astype(np.float32)).astype(np.int32)
        output444,_=convert(ffmpeg,image,pixel_format='yuv444p10le')
        planes444=np.frombuffer(output444,'<u2').reshape(3,h,w)
        candidates=np.stack([np.rint((plane.astype(np.float64)*scale+offset).astype(np.float32))
            for plane,scale,offset in zip(floating,(876,896,896),(64,512,512))]).astype(np.int32)
        box=np.stack([np.rint((plane.reshape(h//2,2,w//2,2).mean(axis=(1,3)).astype(np.float64)*896+512).astype(np.float32))
            for plane in floating[1:]],axis=-1).astype(np.int32)
        row=dict(name=name,sha256=hashlib.sha256(expected).hexdigest(),
            fixed_coefficients=coefficients,fixed_y_mismatch=int(np.count_nonzero(y-((fixed[0]//2+16)>>5))),
            fixed_uv_mismatch=int(np.count_nonzero(uv-fixed_chroma(fixed))),
            y_mismatch=int(np.count_nonzero(y.astype(np.int32)-candidate_y)),
            planes444_mismatch=[int(np.count_nonzero(planes444[i]-candidates[i])) for i in range(3)],
            box_mismatch=int(np.count_nonzero(uv.astype(np.int32)-box)))
        if name.startswith('impulse'):
            row['chroma_impulse']=[dict(x=int(x),y=int(y),u=int(uv[y,x,0]),v=int(uv[y,x,1]))
                for y,x in np.argwhere(np.any(uv!=512,axis=-1))]
        if name=='random':row['log']=log;row['y_examples']=[[int(y.flat[i]),int(candidate_y.flat[i])] for i in range(20)]
        report['cases'].append(row);print(json.dumps({k:v for k,v in row.items() if k!='log'}),flush=True)
    with dest.open('x',encoding='utf-8') as output:json.dump(report,output,indent=2)


if __name__=='__main__':main()
