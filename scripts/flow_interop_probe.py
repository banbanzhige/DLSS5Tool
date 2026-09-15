"""Bounded same-process CUDA/D3D12 flow experiment; no product changes.

This isolates CPU-bounce removal. Input decode/upload and final DLSS readback
remain unchanged. Serial CUDA stream synchronization is intentional; no claim
of asynchronous, cross-process, end-to-end zero-copy or GUI/export speed.
"""
import argparse
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class Win32Handle(C.Structure):
    _fields_ = [('handle', C.c_void_p), ('name', C.c_void_p)]


class HandleUnion(C.Union):
    _fields_ = [('fd', C.c_int), ('win32', Win32Handle), ('nvSciBufObject', C.c_void_p)]


class ExternalMemoryDesc(C.Structure):
    _fields_ = [('type', C.c_int), ('handle', HandleUnion), ('size', C.c_uint64),
                ('flags', C.c_uint), ('reserved', C.c_uint * 16)]


class BufferDesc(C.Structure):
    _fields_ = [('offset', C.c_uint64), ('size', C.c_uint64),
                ('flags', C.c_uint), ('reserved', C.c_uint * 16)]


def bind(lib, name, args, result=C.c_int):
    fn = getattr(lib, name)
    fn.argtypes, fn.restype = args, result
    return fn


def check(code, label):
    if code:
        raise RuntimeError(f'{label}: CUDA error {code}')


class Interop:
    def __init__(self, lib, fw, fh, adapter, torch):
        self.torch, self.lib = torch, lib
        self.size = fw * fh * 8
        self.external, self.pointer = C.c_void_p(), C.c_uint64()
        self.cuda = C.WinDLL('nvcuda.dll')
        bind(self.cuda, 'cuCtxGetDevice', [C.POINTER(C.c_int)])
        bind(self.cuda, 'cuDeviceGetLuid', [C.c_void_p, C.POINTER(C.c_uint), C.c_int])
        bind(self.cuda, 'cuImportExternalMemory', [C.POINTER(C.c_void_p), C.POINTER(ExternalMemoryDesc)])
        bind(self.cuda, 'cuExternalMemoryGetMappedBuffer', [C.POINTER(C.c_uint64), C.c_void_p, C.POINTER(BufferDesc)])
        bind(self.cuda, 'cuMemcpyDtoDAsync_v2', [C.c_uint64, C.c_uint64, C.c_size_t, C.c_void_p])
        bind(self.cuda, 'cuStreamSynchronize', [C.c_void_p])
        bind(self.cuda, 'cuMemFree_v2', [C.c_uint64])
        bind(self.cuda, 'cuDestroyExternalMemory', [C.c_void_p])
        bind(lib, 'probe_create', [C.c_uint, C.c_uint, C.POINTER(C.c_void_p), C.POINTER(C.c_uint64)])
        bind(lib, 'probe_close', [], None)
        bind(lib, 'probe_process', [C.c_void_p, C.c_void_p, C.c_int])
        bind(lib, 'probe_motion_bits', [C.c_void_p, C.c_int])
        bind(lib, 'probe_cpu_half', [C.c_void_p, C.c_void_p, C.c_size_t], None)
        device, luid, mask = C.c_int(), C.create_string_buffer(8), C.c_uint()
        check(self.cuda.cuCtxGetDevice(C.byref(device)), 'cuCtxGetDevice')
        check(self.cuda.cuDeviceGetLuid(luid, C.byref(mask), device), 'cuDeviceGetLuid')
        actual = (int.from_bytes(luid.raw[4:], 'little', signed=True), int.from_bytes(luid.raw[:4], 'little'))
        if actual != (adapter['luid_high'], adapter['luid_low']) or mask.value != 1:
            raise RuntimeError('CUDA and D3D12 must use the same single-node physical GPU')
        handle, allocation = C.c_void_p(), C.c_uint64()
        if not lib.probe_create(fw, fh, C.byref(handle), C.byref(allocation)):
            lib.probe_close()
            raise RuntimeError('probe_create failed; inspect task ngx.log')
        try:
            desc = ExternalMemoryDesc()
            desc.type, desc.flags, desc.size = 5, 1, allocation.value  # D3D12_RESOURCE, DEDICATED
            desc.handle.win32.handle = handle.value
            check(self.cuda.cuImportExternalMemory(C.byref(self.external), C.byref(desc)), 'cuImportExternalMemory')
            mapping = BufferDesc(size=self.size)
            check(self.cuda.cuExternalMemoryGetMappedBuffer(C.byref(self.pointer), self.external, C.byref(mapping)),
                  'cuExternalMemoryGetMappedBuffer')
        except BaseException:
            self.close()
            raise

    def send(self, flow):
        if flow.dtype != self.torch.float32 or not flow.is_cuda or not flow.is_contiguous() or flow.numel()*4 != self.size:
            raise ValueError('Expected contiguous FP32 CUDA flow [1,2,fh,fw]')
        stream = self.torch.cuda.current_stream().cuda_stream
        check(self.cuda.cuMemcpyDtoDAsync_v2(self.pointer, flow.data_ptr(), self.size, stream), 'cuMemcpyDtoDAsync')
        # CPU waits for completion, but no flow bytes are read back to CPU.
        # probe_process returns after D3D12 consumption before the next overwrite.
        check(self.cuda.cuStreamSynchronize(stream), 'cuStreamSynchronize')

    def close(self):
        self.torch.cuda.synchronize()
        if self.pointer.value:
            check(self.cuda.cuMemFree_v2(self.pointer), 'cuMemFree')
            self.pointer.value = 0
        if self.external.value:
            check(self.cuda.cuDestroyExternalMemory(self.external), 'cuDestroyExternalMemory')
            self.external.value = None
        self.lib.probe_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--frames', type=int, default=12)
    parser.add_argument('--edge', type=int, default=512)
    parser.add_argument('--rounds', type=int, default=4)
    parser.add_argument('--smoke', action='store_true', help='Synthetic CUDA flow / real image, no RAFT load')
    parser.add_argument('--no-interop', action='store_true', help='Control: only original CPU-bounce path; no shared resource creation')
    parser.add_argument('--shutdown-diagnostic', action='store_true', help='Explicitly test known-hanging NGX shutdown; default uses production isolated-process exit')
    parser.add_argument('--label', default='report')
    parser.add_argument('--start-frame', type=int, default=0)
    args = parser.parse_args()
    args.work = args.work.resolve()
    if not (args.work / 'TASK.md').is_file():
        parser.error('Register TASK.md and check disk space first')
    if not 2 <= args.frames <= 32 or not 1 <= args.rounds <= 8 or not 128 <= args.edge <= 1024 or not 0 <= args.start_frame <= 300:
        parser.error('Bounded experiment: frames 2..32, rounds 1..8, edge 128..1024')
    if not args.label.replace('-', '').replace('_', '').isalnum():
        parser.error('label must be alphanumeric, dash or underscore')
    report_path = args.work / f'{args.label}.json'
    if report_path.exists():
        parser.error('Use a fresh report label; refusing to overwrite evidence')
    os.environ['TMP'] = os.environ['TEMP'] = str(args.work)
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    import cv2
    import numpy as np
    import torch
    from dlss5tool import dlss_engine as engine
    from dlss5tool.guidance_worker import Models
    from dlss5tool.guidance_parameters import analysis_size
    torch.cuda.init()
    torch.empty(0, device='cuda')  # establish current primary context
    cap = cv2.VideoCapture(str(args.source))
    for _ in range(args.start_frame):
        if not cap.grab():
            raise RuntimeError('Source is too short for start-frame')
    frames = []
    for _ in range(args.frames):
        ok, bgr = cap.read()
        if not ok:
            raise RuntimeError('Source video is too short or cannot be decoded')
        frames.append(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA))
    cap.release()
    h, w = frames[0].shape[:2]
    if w*h > 4096*2160:
        raise RuntimeError('Probe input exceeds bounded 4K pixel budget')
    fw, fh = analysis_size(w, h, args.edge)
    engine.HOST_DLL_V2 = str(args.work / 'candidate.dll')
    engine.LOG_PATH = str(args.work / f'{args.label}-ngx.log')
    settings = dict(host_backend='v2', host_auto_fallback=False, host_submission='merged',
                    host_in_flight=1, host_zero_fast_path=False, host_persistent_buffers=True,
                    guidance_mode=0, style=0, intensity=1.0, local_tone=1.0, local_struct=1.0,
                    use_auto_mask=True, skin_struct=1.0)
    # Bypass the component only in this isolated harness. Both paths receive
    # identical flow generated directly using the repository's Models class.
    live = engine.Live(w, h, settings)
    lib = live._lib
    engine._set_options(lib, {**settings, 'guidance_mode': 1})
    bridge = model = None
    report = dict(dimensions=[w,h], flow_dimensions=[fw,fh], source=str(args.source),
                  torch=torch.__version__, cuda=torch.version.cuda, gpu=live.adapter_info,
                  smoke=args.smoke, frames=args.frames, rounds=args.rounds,
                  no_interop=args.no_interop,
                  shutdown_diagnostic=args.shutdown_diagnostic,
                  start_frame=args.start_frame,
                  opencv=cv2.__version__,
                  sources_sha256={name:hashlib.sha256((ROOT/'scripts'/name).read_bytes()).hexdigest()
                                  for name in ('flow_interop_probe.py','flow_interop_host.cpp')},
                  settings={**settings, 'guidance_mode':1, 'flow_updates':6, 'precision':'FP32'},
                  scope='same-process serial flow-to-DLSS only; no IPC, decode, encoding or GUI',
                  shader='D3D12 bilinear resize + production half conversion',
                  runtime_sha256=hashlib.sha256(Path(live.runtime_path).read_bytes()).hexdigest(),
                  candidate_sha256=hashlib.sha256((args.work/'candidate.dll').read_bytes()).hexdigest())
    try:
        if not args.no_interop:
            bridge = Interop(lib, fw, fh, live.adapter_info, torch)
            print('CUDA/D3D12 shared resource import passed on ' + live.adapter_info['name'], flush=True)
        else:
            print('Control: no shared resources; original CPU-bounce path only', flush=True)
        paths = ('cpu',) if args.no_interop else ('cpu','gpu')
        if not args.smoke:
            model = Models(dict(guidance_mode=1, guidance_device='cuda', guidance_flow_edge=args.edge,
                                guidance_flow_updates=6, guidance_cache_mb=0,
                                flow_weights=str(ROOT/'mods/models/raft_large_C_T_SKHT_V2-ff5fadd5.pth')))
        small = [cv2.resize(frame[...,:3], (fw,fh)) for frame in frames]
        inputs = []
        if model:
            for index in range(len(frames)):
                model.prev = small[max(0,index-1)]
                inputs.append(model._flow_input(small[index]))
        zeros = torch.zeros((1,2,fh,fw), device='cuda')
        mv = np.zeros((h,w,2), np.float32)
        output = np.empty_like(frames[0])
        reset_at = len(frames)//2

        def infer(index):
            if index in (0, reset_at):
                return zeros
            if model:
                return model._infer_flow(inputs[index]).contiguous()
            # Synthetic nontrivial deterministic, representable input.
            x = torch.arange(fw, device='cuda', dtype=torch.float32)[None,:].expand(fh,fw)
            y = torch.arange(fh, device='cuda', dtype=torch.float32)[:,None].expand(fh,fw)
            return torch.stack(((x%29-14)/8, (y%31-15)/8))[None].contiguous()

        def cpu_finish(flow):
            values = flow[0].permute(1,2,0).cpu().numpy()
            cv2.resize(values, (w,h), dst=mv, interpolation=cv2.INTER_LINEAR)
            mv[...,0] *= w/fw
            mv[...,1] *= h/fh

        def render(path, index, flow):
            reset = index in (0, reset_at)
            if path == 'cpu':
                cpu_finish(flow)
                ok = lib.dlssnr_process(frames[index].ctypes.data, mv.ctypes.data, None,
                                        output.ctypes.data, int(reset))
            else:
                bridge.send(flow)
                ok = lib.probe_process(frames[index].ctypes.data, output.ctypes.data, int(reset))
            if not ok:
                raise RuntimeError(f'{path} process failed at {index}')

        with torch.inference_mode():
            flows = [infer(index) for index in range(len(frames))]
            torch.cuda.synchronize()
            # Audit exact same model output to remove model-run variability.
            bit_checks = []
            for index, flow in enumerate(flows if bridge else []):
                cpu_finish(flow)
                cpu_bits = np.empty((h,w,2), np.uint16)
                lib.probe_cpu_half(mv.ctypes.data, cpu_bits.ctypes.data, mv.size)
                bridge.send(flow)
                gpu_bits = np.empty_like(cpu_bits)
                if not lib.probe_motion_bits(gpu_bits.ctypes.data, int(index in (0,reset_at))):
                    raise RuntimeError('Motion readback audit failed')
                delta = np.abs(cpu_bits.view(np.float16).astype(np.float32) - gpu_bits.view(np.float16).astype(np.float32))
                bit_checks.append(dict(frame=index, changed=int(np.count_nonzero(cpu_bits != gpu_bits)),
                                       values=mv.size, max_abs=float(delta.max()), mean_abs=float(delta.mean())))
            report['motion_audit'] = bit_checks
            references = []
            for index, flow in enumerate(flows):
                render('cpu',index,flow)
                references.append(output.copy())
            quality = []
            for path in paths:
                checks = []
                for index, flow in enumerate(flows):
                    render(path,index,flow)
                    delta = np.abs(output[...,:3].astype(np.int16)-references[index][...,:3].astype(np.int16))
                    checks.append(dict(frame=index, max_abs=int(delta.max()), mae=float(delta.mean()),
                                       changed=int(np.count_nonzero(delta)), rgb_values=delta.size,
                                       sha256=hashlib.sha256(output).hexdigest()))
                quality.append(dict(path=path, comparisons=checks))
            report['quality'] = quality
            del references
            print('Motion audit max=' + str(max((x['max_abs'] for x in bit_checks),default=0)) +
                  '; DLSS GPU max=' + str(max(x['max_abs'] for x in quality[-1]['comparisons'])), flush=True)
            for path in paths:
                for index, flow in enumerate(flows):
                    render(path,index,flow)
            results = []
            for phase in ('transport_dlss', 'raft_transport_dlss') if model else ('transport_dlss',):
                for repeat in range(args.rounds):
                    for path in (paths if repeat%2==0 else tuple(reversed(paths))):
                        samples=[]
                        for index in range(len(frames)):
                            start=time.perf_counter()
                            flow = infer(index) if phase == 'raft_transport_dlss' else flows[index]
                            render(path,index,flow)
                            samples.append((time.perf_counter()-start)*1000)
                        record=dict(phase=phase, repeat=repeat, path=path, samples_ms=samples,
                                    mean_ms=statistics.mean(samples), fps=1000/statistics.mean(samples))
                        results.append(record)
                        print(json.dumps({k:v for k,v in record.items() if k!='samples_ms'}), flush=True)
            report['timings']=results
            report['summary']={}
            for phase in {r['phase'] for r in results}:
                avg={path:statistics.mean(r['mean_ms'] for r in results if r['phase']==phase and r['path']==path)
                     for path in paths}
                report['summary'][phase]=dict(avg)
                if 'gpu' in avg:
                    report['summary'][phase].update(speedup_percent=(avg['cpu']/avg['gpu']-1)*100,
                                                   saved_ms=avg['cpu']-avg['gpu'])
                steady={path:statistics.mean(value for r in results if r['phase']==phase and r['path']==path
                                              for i,value in enumerate(r['samples_ms']) if i not in (0,reset_at))
                        for path in paths} if len(frames)>2 else {}
                report['summary'][phase]['nonreset_mean_ms']=steady
            report['status']='measurements_completed'
    except BaseException as exc:
        report['status']='failed'
        report['error']=repr(exc)
        raise
    finally:
        report['cleanup']='pending'
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        def cleanup_timeout():
            report['cleanup']='timed_out_after_20_seconds; isolated probe process terminated'
            report['status']='cleanup_failed'
            report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print('Cleanup timed out; terminating ONLY this isolated probe process', flush=True)
            os._exit(3)
        watchdog = threading.Timer(20, cleanup_timeout)
        watchdog.daemon = True
        watchdog.start()
        try:
            if bridge:
                print('Cleanup: shared CUDA resources', flush=True)
                bridge.close()
            if model:
                print('Cleanup: model', flush=True)
                model.close()
            if args.shutdown_diagnostic:
                print('Cleanup: DLSS host explicit shutdown diagnostic', flush=True)
                if hasattr(lib, 'probe_shutdown_diagnostic'):
                    bind(lib, 'probe_shutdown_diagnostic', [], None)()
                else:
                    live.close()
                report['cleanup']='completed'
            else:
                # Same policy as dlss_host_process.py: do not call the known
                # hanging NGX shutdown after Evaluate. This CLI is disposable;
                # shared interop objects and model have already been released.
                report['cleanup']='shared resources and model released; remaining NGX state reclaimed by isolated process exit'
            if report['status']=='measurements_completed':
                report['status']='completed'
        except BaseException as exc:
            report['cleanup']=repr(exc)
            report['status']='failed'
            raise
        finally:
            watchdog.cancel()
            report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report['summary'],ensure_ascii=False),flush=True)
    if not args.shutdown_diagnostic:
        # Also avoids implicit DLL unload ordering during Python finalization.
        os._exit(0)


if __name__ == '__main__':
    main()
