"""Exercise the GUI's real three-process ProcessLive lifecycle with GPU flow."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

def candidate_factory(width,height,settings):
    from dlss5tool import dlss_engine
    dlss_engine.HOST_DLL_V2=os.environ['FLOW_GPU_PROBE_HOST']
    return dlss_engine.Live(width,height,settings)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--work',type=Path,required=True)
    p.add_argument('--label',required=True)
    a=p.parse_args();a.work=a.work.resolve()
    report_path=a.work/(a.label+'.json')
    if not (a.work/'TASK.md').is_file() or report_path.exists() or not a.label.replace('-','').isalnum():
        p.error('Registered task and new label required')
    os.environ['TMP']=os.environ['TEMP']=str(a.work)
    os.environ['PYTHONDONTWRITEBYTECODE']='1'
    os.environ['DLSS5TOOL_GUIDANCE_PYTHON']=str(ROOT/'tmp/guidance-cuda-env/Scripts/python.exe')
    os.environ['FLOW_GPU_PROBE_HOST']=str(a.work/'candidate.dll')
    import numpy as np
    from dlss5tool.dlss_host_process import ProcessLive
    settings=dict(host_backend='v2',host_auto_fallback=False,host_zero_fast_path=False,
                  host_submission='merged',host_in_flight=3,host_persistent_buffers=True,
                  guidance_mode=1,guidance_device='cuda',guidance_flow_edge=128,guidance_cache_mb=16,
                  guidance_gpu_transport='off')
    live=ProcessLive(256,256,settings,_live_factory=candidate_factory)
    results=[]
    try:
        for width,height in ((256,256),(320,192),(256,256)):
            live.resize(width,height)
            rng=np.random.default_rng(6)
            original=rng.integers(0,256,(height,width,4),dtype=np.uint8);original[...,3]=255
            frames=[np.ascontiguousarray(np.roll(original,i,axis=1)) for i in range(10)]
            baseline=None
            for mode in ('off','auto'):
                live.update({'guidance_gpu_transport':mode})
                outputs=[]
                for index,frame in enumerate(frames):
                    # Mix synchronous frames and queue use at the public boundary.
                    result=live.process(frame,reset=index in (0,5))
                    if result is None:raise RuntimeError('ProcessLive returned no frame')
                    outputs.append(result.copy())
                if baseline is None:baseline=outputs
                different=sum(int(np.count_nonzero(x!=y)) for x,y in zip(baseline,outputs))
                if different:raise RuntimeError('Resize lifecycle output mismatch')
                if mode=='auto' and not live.guidance_info.get('gpu_flow_transport'):
                    raise RuntimeError('GPU flow missing after resize')
                # Preview must return a CPU visualization without tearing down the GPU import.
                live.guidance_preview(frames[0])
                if live.process(frames[1],reset=True) is None:raise RuntimeError('Resume failed')
                results.append(dict(size=[width,height],mode=mode,changed=different,info=live.guidance_info))
                print(json.dumps(results[-1]),flush=True)
            # Fill/reject/drain queue: rejection must not advance guidance state.
            for frame in frames[:live.max_in_flight]:
                if not live.enqueue(frame,reset=True):raise RuntimeError('Queue unexpectedly full')
            if live.enqueue(frames[4]):raise RuntimeError('Full queue accepted extra frame')
            while live.pending:
                if live.dequeue() is None:raise RuntimeError('Drain failed')
        report=dict(status='completed',results=results,main_imported_torch='torch' in sys.modules)
    except BaseException as exc:
        report=dict(status='failed',results=results,error=repr(exc))
        raise
    finally:
        started=time.perf_counter();live.close()
        report['close_seconds']=time.perf_counter()-started
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__':main()
