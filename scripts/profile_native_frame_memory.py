"""Isolate Python/native frame allocations for verify_p0_stability.py.

No native feature or temporal history is reset. Diagnostic GC at checkpoints
distinguishes Python object retention from untraced runtime/driver commit.
"""
import gc
import hashlib
import json
import os
from pathlib import Path
import time
import tracemalloc


def loaded_modules():
    """Read this process's modules; a failed query is not an absent overlay."""
    import ctypes as C
    kernel, psapi = C.WinDLL('kernel32'), C.WinDLL('psapi', use_last_error=True)
    kernel.GetCurrentProcess.restype = C.c_void_p
    process = kernel.GetCurrentProcess()
    psapi.EnumProcessModules.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32, C.POINTER(C.c_uint32)]
    psapi.GetModuleFileNameExW.argtypes = [C.c_void_p, C.c_void_p, C.c_wchar_p, C.c_uint32]
    modules, needed = (C.c_void_p*1024)(), C.c_uint32()
    if not psapi.EnumProcessModules(process, modules, C.sizeof(modules), C.byref(needed)):
        raise C.WinError(C.get_last_error())
    if needed.value > C.sizeof(modules):
        raise RuntimeError('Module inventory exceeded diagnostic capacity')
    loaded = []
    for module in modules[:needed.value//C.sizeof(C.c_void_p)]:
        name = C.create_unicode_buffer(32768)
        if not psapi.GetModuleFileNameExW(process, module, name, len(name)):
            raise C.WinError(C.get_last_error())
        loaded.append(name.value)
    return loaded


class Profile:
    def __init__(self, kind):
        from scripts.profile_thread_activity import load_counter
        self.path = Path(os.environ['TEMP'])/(kind+'-frame-memory.jsonl')
        self.kind = kind
        self.count = 0
        self.thread_counter = load_counter()
        tracemalloc.start(5)
        self.inspector = None
        if os.environ.get('DLSS5TOOL_P0_MEMORY_MAP') == '1':
            from scripts.inspect_native_allocations import Inspector
            self.inspector = Inspector()

    def record(self, *, force=False):
        from scripts.diagnose_composer_lifetime import memory_snapshot
        self.count += 1
        if not force and self.count != 1 and self.count % 512:
            return
        if self.count == 1:
            loaded = loaded_modules()
            (self.path.parent/'native-modules.json').write_text(json.dumps(loaded,indent=2), encoding='utf-8')
            from scripts.windows_process_context import current_context
            context = current_context()
            identity = str(context.get('created_filetime', 'unknown'))
            target = self.path.parent / f'{self.kind}-execution-context-{os.getpid()}-{identity}.json'
            target.write_text(json.dumps(context, indent=2), encoding='utf-8')
        before = memory_snapshot()
        python_before = tracemalloc.get_traced_memory()
        collected = gc.collect()
        after = memory_snapshot()
        current, peak = tracemalloc.get_traced_memory()
        top = tracemalloc.take_snapshot().statistics('lineno')[:6]
        if self.inspector is not None:
            metadata = self.inspector.snapshot()
            with self.path.with_name(self.path.stem+'-regions.jsonl').open('a', encoding='utf-8') as out:
                out.write(json.dumps(metadata)+'\n')
        row = dict(frame=self.count, wall=time.time(), memory_before_gc=before, memory_after_gc=after,
            python_before_gc_bytes=python_before[0], python_after_gc_bytes=current,
            python_peak_bytes=peak, collected=collected,
            largest_python_sites=[dict(site=str(s.traceback), bytes=s.size, count=s.count) for s in top])
        if self.thread_counter is not None:
            from scripts.profile_thread_activity import read_counter
            row['thread_activity'] = read_counter(self.thread_counter)
        library = getattr(self, 'library', None)
        if library is not None and hasattr(library, 'vsr_p0_log_count'):
            import ctypes as C
            library.vsr_p0_log_count.restype = C.c_uint64
            row['candidate_logging_callbacks'] = library.vsr_p0_log_count()
        if self.count > 1 and os.environ.get('DLSS5TOOL_P0_TRIM_HEAPS') == '1':
            # Diagnostic only: documented LFH cache decommit in this process.
            import ctypes as C
            kernel = C.WinDLL('kernel32', use_last_error=True)
            kernel.HeapSetInformation.argtypes = [C.c_void_p, C.c_int, C.c_void_p, C.c_size_t]
            kernel.HeapSetInformation.restype = C.c_int
            info = (C.c_uint32*2)(1, 0)
            ok = kernel.HeapSetInformation(None, 3, info, C.sizeof(info))
            row['native_heap_trim'] = dict(succeeded=bool(ok), error=0 if ok else C.get_last_error(),
                                          memory_after=memory_snapshot())
        with self.path.open('a', encoding='utf-8') as out:
            out.write(json.dumps(row)+'\n')
        if self.count == 1 and os.environ.get('DLSS5TOOL_P0_INJECT_MEMORY_PRESSURE') == '1':
            from dlss5tool import memory_budget
            memory_budget.system_memory = lambda: dict(total_physical_bytes=32*1024**3,
                free_physical_bytes=4*1024**3, free_commit_bytes=20*1024**3)


def vsr_worker(*args):
    from dlss5tool import super_resolution as sr
    if os.environ.get('DLSS5TOOL_P0_VSR_HOST'):
        sr.VSR_HOST_DLL = os.environ['DLSS5TOOL_P0_VSR_HOST']
    real_load = sr._load_host
    profile = Profile('sr')
    def load(*load_args, **load_kwargs):
        library = real_load(*load_args, **load_kwargs)
        profile.library = library
        actual = library.vsr_process
        def process(*parameters):
            result = actual(*parameters)
            profile.record()
            return result
        library.vsr_process = process
        return library
    sr._load_host = load
    try:
        return sr._worker_main(*args)
    finally:
        profile.record(force=True)


class Live:
    def __init__(self, width, height, settings):
        from dlss5tool.dlss_engine import Live as ActualLive
        self.live = ActualLive(width, height, settings)
        self.profile = Profile('nr')

    def __getattr__(self, name):
        return getattr(self.live, name)

    def process(self, *args, **kwargs):
        result = self.live.process(*args, **kwargs)
        self.profile.record()
        return result

    def close(self):
        self.profile.record(force=True)
        return self.live.close()


def start_heap_trace(work, event):
    """Load only the explicitly requested, task-local diagnostic binary."""
    if os.environ.get('DLSS5TOOL_P0_HEAP_TRACE') != '1':
        return None, 0
    import ctypes as C
    from scripts.verify_p0_stability import checked_path
    trace_path = checked_path(work.parent/'native_heap_trace.dll')
    assert (work.parent/'TASK.md').is_file() and trace_path.is_file()
    trace = C.WinDLL(str(trace_path))
    trace.trace_dump.argtypes = [C.c_wchar_p]
    patches = trace.trace_begin()
    assert patches > 0, patches
    event('heap-trace-start', patches=patches, scope='diagnostic process IAT, default heap')
    return trace, patches


def diagnostic_frames(args):
    deadline = time.monotonic()+getattr(args,'run_seconds',0)
    number = 0
    while number < args.frames or time.monotonic() < deadline:
        yield number
        number += 1


def run(args, work, config, event):
    kind = args.case.rsplit('-', 1)[1]
    if kind == 'composer':
        return composer_control(args, work, config, event)
    if kind in ('device', 'device11', 'cuda', 'idle', 'adapters'):
        return device_control(args, work, event, create_device=kind in ('device','device11'),
                              d3d11=kind == 'device11', cuda=kind == 'cuda' or args.device_cuda,
                              enumerate_adapters=kind == 'adapters')
    import numpy as np
    from dlss5tool import super_resolution as sr
    from dlss5tool.dlss_host_process import ProcessLive
    width, height = args.native_width, args.native_width*9//16
    if kind == 'sr':
        sr._worker_main = vsr_worker
        owner = sr.ProcessSuperResolution(width, height, 2)
        pixels = np.zeros((height, width, 4), np.uint8)
    else:
        owner = ProcessLive(width*2, height*2, config, _live_factory=Live)
        pixels = np.zeros((height*2, width*2, 4), np.uint8)
    pixels[..., 3] = 255
    first_digest = None
    try:
        for frame in diagnostic_frames(args):
            if (work/'cancel.flag').exists():
                raise RuntimeError('Supervisor stopped native allocation diagnostic')
            pixels[..., 0] = frame % 256
            result = owner.process(pixels) if kind == 'sr' else owner.process(pixels, reset=frame == 0)
            assert result is not None
            if frame == 0:
                first_digest = hashlib.sha256(result.tobytes()).hexdigest()
                event('first-output', sha256=first_digest)
            if args.simulate_pressure:
                process = owner._process if kind == 'sr' else owner._session._process
                connection = owner._connection if kind == 'sr' else owner._session._connection
                event('simulated-pressure', child_pid=process.pid, real_pressure=False)
                deadline = time.monotonic()+10
                while process.is_alive() and time.monotonic() < deadline:
                    time.sleep(.05)
                assert not process.is_alive(), 'Idle GPU worker ignored simulated memory pressure'
                assert connection.poll(1), 'Worker did not report pressure'
                response = connection.recv()
                assert not response['ok'] and 'MemoryError' in str(response), response
                event('pressure-retired', child_pid=process.pid, response=response)
                return dict(native=kind, frames=1, simulated_pressure=True,
                            pressure_retirement_verified=True)
            if frame == 0 and args.idle_seconds:
                event('idle-start', seconds=args.idle_seconds)
                deadline = time.monotonic()+args.idle_seconds
                while time.monotonic() < deadline:
                    if (work/'cancel.flag').exists():
                        raise RuntimeError('Supervisor stopped idle diagnostic')
                    time.sleep(.1)
                event('idle-end')
            if frame % 128 == 0:
                event('frame', frame=frame+1, total=args.frames)
    finally:
        owner.close()
    return dict(native=kind, frames=frame+1, width=width, height=height,
                diagnostic_gc_interval=512, first_output_sha256=first_digest)


def device_control(args, work, event, *, create_device, d3d11=False, cuda=False, enumerate_adapters=False):
    """D3D12 device only: no SDK, models, frame buffers or command dispatch."""
    import ctypes as C
    import uuid
    device = C.c_void_p()
    context = C.c_void_p()
    library = None
    if cuda:
        library = C.WinDLL('nvcuda', use_last_error=True)
        library.cuInit.argtypes = [C.c_uint32]
        library.cuInit.restype = C.c_int
        code = library.cuInit(0)
        if code:
            raise OSError(f'cuInit failed: {code}')
    if create_device and d3d11:
        library = C.WinDLL('d3d11', use_last_error=True)
        library.D3D11CreateDevice.argtypes = [C.c_void_p, C.c_uint32, C.c_void_p,
            C.c_uint32, C.c_void_p, C.c_uint32, C.c_uint32, C.c_void_p, C.c_void_p, C.c_void_p]
        library.D3D11CreateDevice.restype = C.c_int32
        code = library.D3D11CreateDevice(None, 1, None, 0, None, 0, 7,
                                         C.byref(device), None, C.byref(context))
        if code < 0:
            raise OSError(f'D3D11CreateDevice failed: {code & 0xffffffff:08x}')
    elif create_device:
        library = C.WinDLL('d3d12', use_last_error=True)
        library.D3D12CreateDevice.argtypes = [C.c_void_p, C.c_uint32, C.c_void_p, C.c_void_p]
        library.D3D12CreateDevice.restype = C.c_int32
        iid = C.create_string_buffer(uuid.UUID('189819f1-1db6-4b57-be54-1821339b85f7').bytes_le)
        code = library.D3D12CreateDevice(None, 0xc000, iid, C.byref(device))
        if code < 0:
            raise OSError(f'D3D12CreateDevice failed: {code & 0xffffffff:08x}')
    if enumerate_adapters:
        from dlss5tool.dlss_engine import available_render_adapters
        event('adapters-enumerated', adapters=available_render_adapters(), ngx_initialized=False)
    profile = Profile('adapters' if enumerate_adapters else ('cuda' if cuda else ('device' if create_device else 'idle')))
    duration = args.idle_seconds or 60
    resources = []
    trace, patches = None, 0
    try:
        queries = []
        if args.device_timing:
            methods = C.cast(device, C.POINTER(C.POINTER(C.c_void_p))).contents
            create_query = C.WINFUNCTYPE(C.c_int32, C.c_void_p, C.c_void_p, C.c_void_p)(methods[24])
            for kind in (3, 2, 2):  # D3D11_QUERY_TIMESTAMP_DISJOINT, TIMESTAMP x2
                query = C.c_void_p()
                code = create_query(device, (C.c_uint32*2)(kind, 0), C.byref(query))
                if code < 0:
                    raise OSError(f'CreateQuery failed: {code & 0xffffffff:08x}')
                queries.append(query)
                resources.append(query)
            commands = C.cast(context, C.POINTER(C.POINTER(C.c_void_p))).contents
            C.WINFUNCTYPE(None, C.c_void_p, C.c_void_p)(commands[27])(context, queries[0])
            C.WINFUNCTYPE(None, C.c_void_p, C.c_void_p)(commands[28])(context, queries[1])
        if args.device_compute:
            compiler = C.WinDLL('d3dcompiler_47', use_last_error=True)
            compiler.D3DCompile.argtypes = [C.c_void_p, C.c_size_t, C.c_char_p, C.c_void_p,
                C.c_void_p, C.c_char_p, C.c_char_p, C.c_uint32, C.c_uint32, C.c_void_p, C.c_void_p]
            compiler.D3DCompile.restype = C.c_int32
            blob, errors, shader = C.c_void_p(), C.c_void_p(), C.c_void_p()
            resources.extend((shader, errors, blob))
            source = b'[numthreads(1,1,1)] void main(uint3 id:SV_DispatchThreadID) {}'
            code = compiler.D3DCompile(source, len(source), None, None, None,
                b'main', b'cs_5_0', 0, 0, C.byref(blob), C.byref(errors))
            if code < 0:
                raise OSError(f'D3DCompile failed: {code & 0xffffffff:08x}')
            table = C.cast(blob, C.POINTER(C.POINTER(C.c_void_p))).contents
            data = C.WINFUNCTYPE(C.c_void_p, C.c_void_p)(table[3])(blob)
            size = C.WINFUNCTYPE(C.c_size_t, C.c_void_p)(table[4])(blob)
            table = C.cast(device, C.POINTER(C.POINTER(C.c_void_p))).contents
            create = C.WINFUNCTYPE(C.c_int32, C.c_void_p, C.c_void_p, C.c_size_t,
                                   C.c_void_p, C.c_void_p)
            if create(table[18])(device, data, size, None, C.byref(shader)) < 0:
                raise OSError('CreateComputeShader failed')
            table = C.cast(context, C.POINTER(C.POINTER(C.c_void_p))).contents
            C.WINFUNCTYPE(None, C.c_void_p, C.c_void_p, C.c_void_p, C.c_uint32)(table[69])(
                context, shader, None, 0)
            C.WINFUNCTYPE(None, C.c_void_p, C.c_uint32, C.c_uint32, C.c_uint32)(table[41])(
                context, 1, 1, 1)
            C.WINFUNCTYPE(None, C.c_void_p)(table[111])(context)
            event('minimal-d3d11-compute')
        if args.device_work:
            table = C.cast(device, C.POINTER(C.POINTER(C.c_void_p))).contents
            texture, view = C.c_void_p(), C.c_void_p()
            resources.extend((view, texture))
            # D3D11_TEXTURE2D_DESC: RGBA8, one 32x32 render target, no CPU copy.
            width, height = (1920,1080) if args.device_readback else (32,32)
            desc = (C.c_uint32*11)(width,height,1,1,28,1,0,0,0x20,0,0)
            create = C.WINFUNCTYPE(C.c_int32, C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p)
            if create(table[5])(device, desc, None, C.byref(texture)) < 0:
                raise OSError('CreateTexture2D failed')
            if create(table[9])(device, texture, None, C.byref(view)) < 0:
                raise OSError('CreateRenderTargetView failed')
            commands = C.cast(context, C.POINTER(C.POINTER(C.c_void_p))).contents
            C.WINFUNCTYPE(None, C.c_void_p, C.c_void_p, C.c_void_p)(commands[50])(
                context, view, (C.c_float*4)(0,0,0,1))
            C.WINFUNCTYPE(None, C.c_void_p)(commands[111])(context)
            event('minimal-d3d11-clear', width=width, height=height)
            if args.device_readback:
                staging = C.c_void_p()
                resources.append(staging)
                desc[7], desc[8], desc[9] = 3, 0, 0x20000  # STAGING, no binding, CPU READ
                if create(table[5])(device, desc, None, C.byref(staging)) < 0:
                    raise OSError('Create staging texture failed')
                C.WINFUNCTYPE(None,C.c_void_p,C.c_void_p,C.c_void_p)(commands[47])(
                    context,staging,texture)
                class Mapped(C.Structure):
                    _fields_ = [('data',C.c_void_p),('row_pitch',C.c_uint32),('depth_pitch',C.c_uint32)]
                mapped = Mapped()
                code = C.WINFUNCTYPE(C.c_int32,C.c_void_p,C.c_void_p,C.c_uint32,C.c_uint32,
                    C.c_uint32,C.c_void_p)(commands[14])(context,staging,0,1,0,C.byref(mapped))
                if code < 0:
                    raise OSError(f'Map failed: {code & 0xffffffff:08x}')
                C.WINFUNCTYPE(None,C.c_void_p,C.c_void_p,C.c_uint32)(commands[15])(context,staging,0)
                event('minimal-d3d11-readback', row_pitch=mapped.row_pitch)
        if queries:
            commands = C.cast(context, C.POINTER(C.POINTER(C.c_void_p))).contents
            end = C.WINFUNCTYPE(None, C.c_void_p, C.c_void_p)(commands[28])
            end(context, queries[2])
            end(context, queries[0])
            C.WINFUNCTYPE(None, C.c_void_p)(commands[111])(context)
            get = C.WINFUNCTYPE(C.c_int32, C.c_void_p, C.c_void_p, C.c_void_p,
                               C.c_uint32, C.c_uint32)(commands[29])
            data = [(C.c_uint64*2)(), C.c_uint64(), C.c_uint64()]
            timeout = time.monotonic()+5
            for query, value in zip(queries, data):
                while True:
                    code = get(context, query, C.byref(value), C.sizeof(value), 0)
                    if code == 0:
                        break
                    if code < 0 or time.monotonic() > timeout:
                        raise RuntimeError(f'Timestamp query not ready: {code & 0xffffffff:08x}')
                    time.sleep(.01)
            event('minimal-d3d11-timing', frequency=data[0][0], disjoint=data[0][1],
                  begin=data[1].value, end=data[2].value)
        trace, patches = start_heap_trace(work, event)
        event('idle-start', seconds=duration, device=('d3d11' if d3d11 else 'd3d12') if create_device else None)
        start, checkpoint = time.monotonic(), 0
        while time.monotonic()-start < duration:
            if (work/'cancel.flag').exists():
                raise RuntimeError('Supervisor stopped device control')
            if time.monotonic()-start >= checkpoint:
                profile.record(force=True)
                if trace is not None:
                    assert trace.trace_dump(str(work/f'native-heap-{int(checkpoint)}.jsonl')) == 0
                    event('heap-trace-snapshot', number=int(checkpoint))
                checkpoint += 20
            time.sleep(.1)
        profile.record(force=True)
        if trace is not None:
            assert trace.trace_dump(str(work/'native-heap-final.jsonl')) == 0
            event('heap-trace-snapshot', number='final')
        event('idle-end')
    finally:
        if trace is not None:
            restored = trace.trace_end()
            event('heap-trace-ended', restored=restored, patches=patches)
            assert restored == patches, (restored, patches)
        for interface in (*resources, context, device):
            if interface.value:
                table = C.cast(interface, C.POINTER(C.POINTER(C.c_void_p))).contents
                C.WINFUNCTYPE(C.c_uint32, C.c_void_p)(table[2])(interface)
    return dict(native='adapters' if enumerate_adapters else ('cuda' if cuda else (('device11' if d3d11 else 'device') if create_device else 'idle')),
                cuda_initialized=cuda, d3d_device_created=create_device,
                idle_seconds=duration, minimal_device_work=args.device_work, minimal_compute=args.device_compute,
                minimal_readback=args.device_readback, minimal_timing=args.device_timing)


def composer_control(args, work, config, event):
    import ctypes as C
    import numpy as np
    from dlss5tool import residual_composition as composition
    pixels = np.zeros((1080, 1920, 4), np.uint8)
    pixels[..., 3] = 255
    processed = pixels.copy()
    processed[..., :3] = 110
    profile = Profile('composer')
    counts = (C.c_uint64*3)()
    library = None
    try:
        for frame in diagnostic_frames(args):
            if (work/'cancel.flag').exists():
                raise RuntimeError('Supervisor stopped composer diagnostic')
            if args.composer_resize:
                width, height = ((1920,1080), (1600,896))[frame % 2]
                pixels = np.zeros((height, width, 4), np.uint8)
                pixels[..., 3] = 255
                processed = pixels.copy()
                processed[..., :3] = 110
            result = composition.compose(pixels, processed, config)
            bridge = getattr(composition._native_state, 'bridge', None)
            assert bridge is not None, 'Composer control did not dispatch to the native GPU'
            library = bridge.library
            assert result.shape == pixels.shape
            profile.record(force=(args.composer_resize and not args.run_seconds) or frame == args.frames-1)
            if args.composer_resize and (not args.run_seconds or frame % 512 == 0):
                event('composer-size', frame=frame, width=pixels.shape[1], height=pixels.shape[0])
            if getattr(args,'run_seconds',0) and (frame==0 or (frame+1)%512==0):
                event('native-progress',frames=frame+1,run_seconds=args.run_seconds)
            if frame == 0 and args.idle_seconds:
                trace, patches = start_heap_trace(work, event)
                try:
                    event('idle-start', seconds=args.idle_seconds, scope='one native composer, no SR/NR')
                    deadline = time.monotonic()+args.idle_seconds
                    checkpoint = time.monotonic()+20
                    number = 0
                    while time.monotonic() < deadline:
                        if (work/'cancel.flag').exists():
                            raise RuntimeError('Supervisor stopped composer diagnostic')
                        if (profile.inspector is not None or trace is not None) and time.monotonic() >= checkpoint:
                            profile.record(force=True)
                            if trace is not None:
                                number += 1
                                assert trace.trace_dump(str(work/f'native-heap-{number}.jsonl')) == 0
                                event('heap-trace-snapshot', number=number)
                            checkpoint += 20
                        time.sleep(.1)
                    if profile.inspector is not None:
                        profile.record(force=True)
                    if trace is not None:
                        assert trace.trace_dump(str(work/'native-heap-final.jsonl')) == 0
                        event('heap-trace-snapshot', number='final')
                finally:
                    if trace is not None:
                        restored = trace.trace_end()
                        event('heap-trace-ended', restored=restored, patches=patches)
                        assert restored == patches, (restored, patches)
                event('idle-end')
        profile.record(force=True)
    finally:
        composition.release_native_composer()
        if library is not None and hasattr(library, 'dlss5_quality_lifetime'):
            library.dlss5_quality_lifetime.argtypes = [C.POINTER(C.c_uint64)]
            library.dlss5_quality_lifetime(counts)
    return dict(native='composer', frames=frame+1, created=counts[0], destroyed=counts[1],
                retained_device_refs=counts[2])
