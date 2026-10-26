"""Exact HDR GPU operator comparison with the frozen original C++ CPU helpers."""
import argparse
import ctypes as C
import hashlib
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
    parser.add_argument('--task',required=True,type=Path)
    parser.add_argument('--label',required=True)
    parser.add_argument('--worker',action='store_true')
    args=parser.parse_args()
    from scripts.build_vsr_cuda import safe,digest
    task=safe(args.task,ROOT/'tmp');target=task/(args.label+'.json')
    if not (task/'TASK.md').is_file() or not args.label.replace('-','').isalnum() or target.exists():
        raise ValueError('Fresh registered work required')
    if not args.worker:
        from dlss5tool.process_job import KillOnCloseJob,confirm_retired,process_record
        from dlss5tool.dlss_engine import available_render_adapters
        adapter=available_render_adapters()[0]
        environment=dict(os.environ,TEMP=str(task),TMP=str(task),PYTHONDONTWRITEBYTECODE='1',
            PYTHONPATH=str(Path(sys.prefix)/'Lib/site-packages'),HDR_PROBE_ADAPTER=json.dumps(adapter))
        process=subprocess.Popen([sys._base_executable,'-B',str(Path(__file__).resolve()),*sys.argv[1:],'--worker'],
            cwd=ROOT,env=environment,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            text=True,creationflags=subprocess.CREATE_NO_WINDOW)
        job=KillOnCloseJob(process)
        try:
            stdout,stderr=process.communicate('GO\n',timeout=60)
            record=dict(returncode=process.returncode,stdout=stdout[-5000:],stderr=stderr[-5000:])
        except subprocess.TimeoutExpired:
            record=dict(returncode='timeout')
        finally:
            job.close()
            if process.poll() is None:process.kill();process.wait(timeout=5)
            confirm_retired(process)
        record.update(pid=process.pid,after_retirement=process_record(process.pid))
        (task/(args.label+'-process.json')).write_text(json.dumps(record,indent=2),encoding='utf-8')
        print(json.dumps(record));return 0 if record['returncode']==0 else 1
    if sys.stdin.readline().strip()!='GO':raise RuntimeError('Native work requires parent Job assignment')
    import numpy as np
    from dlss5tool.cuda_session import bind
    from dlss5tool.memory_budget import memory_allocation
    from scripts.profile_cached_export import private_bytes
    adapter=json.loads(os.environ['HDR_PROBE_ADAPTER'])
    library=C.WinDLL(str(safe(task/'build/hdr_operators.dll',task)),winmode=0x1100)
    initialize=bind(library,'hdr_probe_init',[C.c_uint,C.c_uint,C.c_int32,C.c_uint32])
    run=bind(library,'hdr_probe_run',[C.c_int,C.c_void_p,C.c_void_p,C.c_void_p])
    close=bind(library,'hdr_probe_close',[],None)
    error=bind(library,'hdr_probe_error',[],C.c_char_p)
    pack=bind(library,'hdr_reference_pack',[C.c_void_p,C.c_void_p,C.c_uint64],None)
    unpack=bind(library,'hdr_reference_unpack',[C.c_void_p,C.c_void_p,C.c_uint64],None)
    report=dict(status='running',accepted=False,adapter=adapter,library_sha256=digest(task/'build/hdr_operators.dll'),cases=[])
    try:
        for width,height in ((257,256),(127,17),(1920,1080),(3840,2160)):
          with memory_allocation(width*height*96,stage='hdr_operator_probe'):
            n=width*height
            if not initialize(width,height,adapter['luid_high'],adapter['luid_low']):
                raise RuntimeError('GPU operator init: '+str(error()))
            try:
                indices=np.arange(n,dtype=np.uint32).reshape(height,width)
                bits=np.empty((height,width,4),np.uint16)
                bits[...,0]=indices%65536;bits[...,1]=(indices*13+17)%65536
                bits[...,2]=(65535-indices)%65536;bits[...,3]=(indices*317)%65536
                # Test every finite half encoding, signed zeros, subnormals and
                # finite out-of-range clamp values. NaN/Inf are outside the app contract.
                values=bits.view(np.float16)
                values[~np.isfinite(values)]=0
                source=np.ascontiguousarray(values)
                packed=np.empty((height,width),np.uint32);expected_pack=np.empty_like(packed)
                output=np.empty_like(source);expected_output=np.empty_like(source)
                pack(source.ctypes.data,expected_pack.ctypes.data,n)
                unpack(expected_pack.ctypes.data,expected_output.ctypes.data,n)
                if not run(0,source.ctypes.data,output.ctypes.data,packed.ctypes.data):raise RuntimeError('Pack operator failed')
                pack_equal=np.array_equal(packed,expected_pack)
                roundtrip_equal=np.array_equal(output.view(np.uint16),expected_output.view(np.uint16))
                # Every 10-bit channel code and arbitrary alpha bits.
                words=np.arange(n,dtype=np.uint32).reshape(height,width)
                words=(words%1024)|(((words*13)%1024)<<10)|(((words*317)%1024)<<20)|((words%4)<<30)
                unpack(words.ctypes.data,expected_output.ctypes.data,n)
                if not run(1,words.ctypes.data,output.ctypes.data,None):raise RuntimeError('Unpack operator failed')
                unpack_equal=np.array_equal(output.view(np.uint16),expected_output.view(np.uint16))
                gpu_ms=[];cpu_ms=[]
                for _ in range(8):
                    before=time.perf_counter();pack(source.ctypes.data,expected_pack.ctypes.data,n);unpack(expected_pack.ctypes.data,expected_output.ctypes.data,n)
                    cpu_ms.append((time.perf_counter()-before)*1000)
                    before=time.perf_counter()
                    if not run(0,source.ctypes.data,output.ctypes.data,packed.ctypes.data):raise RuntimeError('Repeated conversion failed')
                    gpu_ms.append((time.perf_counter()-before)*1000)
                    if (private_bytes(os.getpid()) or 0)>4*1024**3:raise MemoryError('Probe private-memory guard')
                row=dict(size=[width,height],pack_equal=pack_equal,roundtrip_equal=roundtrip_equal,unpack_equal=unpack_equal,
                    packed_sha256=hashlib.sha256(packed.tobytes()).hexdigest(),
                    cpu_median_ms=statistics.median(cpu_ms[2:]),gpu_including_transfers_median_ms=statistics.median(gpu_ms[2:]))
                report['cases'].append(row);print(json.dumps(row),flush=True)
                if not (pack_equal and roundtrip_equal and unpack_equal):raise RuntimeError('Exact half/packed conversion mismatch')
            finally:close()
        report.update(status='complete',accepted=True)
    except BaseException as exc:
        import traceback
        traceback.print_exc();report.update(status='failed',error=repr(exc))
    finally:target.write_text(json.dumps(report,indent=2),encoding='utf-8')
    return 0 if report['accepted'] else 1


if __name__=='__main__':raise SystemExit(main())
