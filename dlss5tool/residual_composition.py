"""Local vectorized reference for native residual/protection formulas.

Neutral settings bypass conversion. HDR global controls use linear light;
SDR fine controls operate on normalized stored RGB/HSL, as in the native shader.
HDR uses linear luminance/chroma differences and never saturates to SDR white.
"""
from concurrent.futures import ThreadPoolExecutor
import ctypes
import os
import threading
import weakref

import numpy as np
from dlss5tool import appearance
from dlss5tool import paths


COMPOSITION_PARALLEL_MIN_PIXELS = 1_000_000
COMPOSITION_MAX_WORKERS = 4
NATIVE_COMPOSITION_MIN_PIXELS = 1_000_000
_native_state = threading.local()


def _destroy_native_bridge(library, handle, bindings=()):
    for binding in bindings:
        try:
            binding[-1].close()
        except (OSError, RuntimeError, ValueError):
            pass  # Encoder may already have been cancelled/closed.
    if bindings:
        bindings.clear()
    try:
        library.dlss5_quality_destroy(handle)
    except (OSError, ctypes.ArgumentError):
        pass


class _NativeBridge:
    """Own a native allocation even when its thread-local cache disappears.

    The finalizer retains the DLL, never this owner. It runs when a short-lived
    worker exits, with an atexit fallback only for a main-thread bridge.
    """
    def __init__(self, path, library, handle, adapter_luid=None):
        self.path, self.library, self.handle = path, library, handle
        self.adapter_luid = adapter_luid
        self.gpu_bindings = []  # One current consumer, never a growing lease cache.
        self._release = weakref.finalize(self, _destroy_native_bridge, library, handle, self.gpu_bindings)
        # Daemon workers may still be inside the DLL at interpreter shutdown.
        # Never run their cleanup from the main atexit thread while in use.
        self._release.atexit = threading.current_thread() is threading.main_thread()

    def close(self):
        self._release()


def release_native_composer():
    """Release only this thread's idle composer; never another worker's device."""
    bridge = getattr(_native_state, 'bridge', None)
    _native_state.bridge = None
    _native_state.adapter = None
    if bridge is not None:
        bridge.close()


def _native_library_path():
    if os.name != 'nt' or os.environ.get('DLSS5TOOL_DISABLE_NATIVE_COMPOSER'):
        return None
    root = str(paths.runtime_root())
    for path in (os.path.join(root, 'dlss5_quality.dll'),
                 os.path.join(root, 'realtime', 'dlss5_quality.dll')):
        if os.path.isfile(path):
            return path
    return None


def _native_bridge(adapter_luid=None):
    """Return one D3D11 bridge per worker thread, or None when unavailable."""
    path = _native_library_path()
    cached = getattr(_native_state, 'bridge', None)
    if cached is not None:
        if (path is not None and cached.path == path and cached._release.alive
                and cached.adapter_luid == adapter_luid):
            return cached
        release_native_composer()
    if path is None:
        return None
    try:
        library = ctypes.CDLL(path)
        library.dlss5_quality_create.argtypes = []
        library.dlss5_quality_create.restype = ctypes.c_void_p
        library.dlss5_quality_destroy.argtypes = [ctypes.c_void_p]
        library.dlss5_quality_destroy.restype = None
        library.dlss5_quality_compose.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_float), ctypes.c_int,
        ]
        library.dlss5_quality_compose.restype = ctypes.c_int
        if adapter_luid is None:
            handle = library.dlss5_quality_create()
        else:
            # Older optional DLLs cannot honor this contract: use CPU, not
            # their default-adapter entry point.
            library.dlss5_quality_create_on_adapter.argtypes = [ctypes.c_int32, ctypes.c_uint32]
            library.dlss5_quality_create_on_adapter.restype = ctypes.c_void_p
            library.dlss5_quality_adapter.argtypes = [ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_uint32)]
            library.dlss5_quality_adapter.restype = ctypes.c_int
            handle = library.dlss5_quality_create_on_adapter(*adapter_luid)
        if not handle:
            return None
        try:
            if adapter_luid is not None:
                high, low = ctypes.c_int32(), ctypes.c_uint32()
                if (not library.dlss5_quality_adapter(handle, ctypes.byref(high), ctypes.byref(low))
                        or (high.value, low.value) != adapter_luid):
                    _destroy_native_bridge(library, handle)
                    return None
            bridge = _NativeBridge(path, library, handle, adapter_luid)
        except BaseException:
            _destroy_native_bridge(library, handle)
            raise
        _native_state.bridge = bridge
        return bridge
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _composition_adapter(settings):
    """Use NR's actual identity when provided, otherwise its selection policy.

    Cache enumeration only on this worker, not globally across sessions. An
    unavailable explicit choice never turns into an automatic/default choice.
    """
    actual = settings.get('_composition_adapter')
    if actual is not None:
        return int(actual['luid_high']), int(actual['luid_low'])
    from dlss5tool import dlss_engine
    from dlss5tool.adapter_probe import cache_generation
    requested = settings.get('render_gpu', dlss_engine.RENDER_GPU_AUTO)
    generation = cache_generation()
    cached = getattr(_native_state, 'adapter', None)
    if cached is not None and cached[:2] == (requested, generation):
        return cached[2]
    records = dlss_engine.available_render_adapters()
    chosen = (records[0] if requested == dlss_engine.RENDER_GPU_AUTO and records else
              next((r for r in records if r['id'] == requested), None))
    if chosen is None:
        raise ValueError('Composition GPU is unavailable')
    luid = int(chosen['luid_high']), int(chosen['luid_low'])
    # A refresh may have run concurrently with the lookup. Do not mark an old
    # snapshot as belonging to the new generation; resolve again next time.
    _native_state.adapter = ((requested, generation, luid)
                             if generation == cache_generation() else None)
    return luid


def _native_quality_values(values, profile='srgb'):
    q, r = values['nr_quality'], values['nr_residual']
    regions = [float(v) for region in q.get('regions', []) for v in region]
    regions.extend([0.0] * (16 - len(regions)))
    return [
        float(q['total']), float(q['darken']), float(q['brighten']), float(q['color']),
        float(q['luminance']), 1.0 if q.get('protection') else 0.0,
        float(q.get('feather', 12.0)), float(profile != 'srgb'),
        *regions,
        0.0,  # residualInput: local media passes raw NR output, not a residual texture.
        float(r['residualMultiplier']), float(r['residualSaturation']),
        float(r['residualLightness']), float(r['shadowStructureMultiplier']),
        float(r['reflectionGlowMultiplier']), float(profile in ('hdr10_pq', 'hdr10_hlg')), 0.0,
    ]


def _native_compose(original, processed, values, *, order, adapter_luid=None, profile='srgb'):
    """One optional D3D11 shader, same size threshold and fallback for SDR/HDR."""
    types = (np.uint8,) if profile == 'srgb' else (np.float16, np.float32, np.uint16)
    if original.dtype not in types or original.shape[0] * original.shape[1] < NATIVE_COMPOSITION_MIN_PIXELS:
        return None
    bridge = _native_bridge(adapter_luid)
    if bridge is None:
        return None
    try:
        if profile != 'srgb':
            try:
                version = bridge.library.dlss5_quality_version
                version.argtypes, version.restype = [], ctypes.c_int
                if version() < 2:
                    release_native_composer()
                    return None
            except (AttributeError, OSError, TypeError):
                release_native_composer()
                return None
        return _native_compose_frame(original, processed, values, order=order, bridge=bridge, profile=profile)
    except BaseException:
        # A failed preview may retain its exception traceback (and this owner).
        # Release now, rather than relying on collection of that traceback.
        release_native_composer()
        raise


def _native_inputs(original, processed, *, order, profile):
    h, w = original.shape[:2]
    # These immutable snapshots already match the shader's wire contract.
    # Borrow them for the synchronous upload instead of making two full copies.
    if (order == 'rgb' and original.shape[-1] == processed.shape[-1] == 4
            and original.flags.c_contiguous and processed.flags.c_contiguous
            and ((profile == 'srgb' and original.dtype == processed.dtype == np.uint8)
                 or (profile == 'scrgb' and original.dtype == processed.dtype == np.float32))):
        return original, processed, 255 if profile == 'srgb' else 1, None
    if (profile == 'srgb' and original.dtype == processed.dtype == np.uint8
            and original.shape[-1] == processed.shape[-1] == 3):
        import cv2
        conversion = cv2.COLOR_BGR2RGBA if order == 'bgr' else cv2.COLOR_RGB2RGBA
        return cv2.cvtColor(original, conversion), cv2.cvtColor(processed, conversion), 255, None
    # The native shader keeps alpha and always consumes RGBA.  BGR callers are
    # converted to RGB around the dispatch so the luma weights stay correct.
    hdr = profile != 'srgb'
    base = np.empty((h, w, 4), dtype=np.float32 if hdr else np.uint8)
    nr = np.empty_like(base)
    channels = slice(None, None, -1) if order == 'bgr' else slice(None)
    base[..., :3] = original[..., :3][..., channels]
    nr[..., :3] = processed[..., :3][..., channels]
    divisor = np.iinfo(original.dtype).max if original.dtype.kind == 'u' else 1.0
    encode = None
    if hdr:
        base[..., :3] /= divisor
        nr[..., :3] /= divisor
        if profile != 'scrgb':
            from dlss5tool.video_export import _pq_eotf, _pq_oetf, _hlg_eotf, _hlg_oetf
            decode, encode = (_pq_eotf, _pq_oetf) if profile == 'hdr10_pq' else (_hlg_eotf, _hlg_oetf)
            base[..., :3], nr[..., :3] = decode(base[..., :3]), decode(nr[..., :3])
    base[..., 3] = nr[..., 3] = 1 if hdr else 255
    return base, nr, divisor, encode


def _native_compose_frame(original, processed, values, *, order, bridge, profile='srgb'):
    library, handle = bridge.library, bridge.handle
    h, w = original.shape[:2]
    hdr = profile != 'srgb'
    base, nr, divisor, encode = _native_inputs(original, processed, order=order, profile=profile)
    result = np.empty_like(base)
    payload = (ctypes.c_float * 32)(*_native_quality_values(values, profile))
    try:
        ok = library.dlss5_quality_compose(
            handle,
            ctypes.c_void_p(base.ctypes.data), ctypes.c_void_p(nr.ctypes.data),
            ctypes.c_void_p(result.ctypes.data), w, h, 2 if hdr else 0, payload, 32,
        )
    except (OSError, ctypes.ArgumentError, ValueError):
        ok = 0
    if not ok:
        release_native_composer()
        return None
    if not hdr:
        if processed.shape[-1] == 3:
            import cv2
            conversion = cv2.COLOR_RGBA2BGR if order == 'bgr' else cv2.COLOR_RGBA2RGB
            return cv2.cvtColor(result, conversion)
        if order == 'rgb':
            # Native output already owns RGBA8 storage. Preserve the public
            # alpha contract without copying and reassigning all RGB pixels.
            result[..., 3] = original[..., 3] if original.shape[-1] > 3 else processed[..., 3]
            return result
    rgb = result[..., :3]
    if hdr:
        if encode is not None:
            rgb = encode(np.maximum(rgb, 0))
        if profile != 'scrgb':
            rgb = np.clip(rgb, 0, 1)
        if divisor != 1:
            rgb = np.rint(rgb*divisor)
    if order == 'bgr':
        rgb = rgb[..., ::-1]
    output = processed.copy()
    output[..., :3] = rgb
    if output.shape[-1] > 3 and original.shape[-1] > 3:
        output[..., 3] = original[..., 3]
    return output


def compose_to_gpu_writer(original, processed, settings, writer, *,
                          order='rgb', profile='srgb', view=0, mix=1):
    """Optional same-card completed-buffer handoff to the existing encoder.

    False means no encoded frame was submitted; caller retains its normal CPU
    path. Once write_device starts, errors propagate (never retry a frame).
    The encoder's synchronous ACK owns this single slot until consumption ends.
    """
    if (view or mix != 1 or not settings or not appearance.active(settings)
            or profile not in ('srgb', 'hdr10_pq', 'hdr10_hlg')
            or original.shape != processed.shape or original.dtype != processed.dtype
            or original.shape[0]*original.shape[1] < NATIVE_COMPOSITION_MIN_PIXELS
            or original.dtype not in ((np.uint8,) if profile == 'srgb' else (np.float16, np.float32))
            or not hasattr(writer, 'import_shared_output') or not hasattr(writer, 'write_device')
            or (getattr(writer,'width',0),getattr(writer,'height',0)) != (original.shape[1],original.shape[0])
            or _native_library_path() is None):
        return False
    values = appearance.preflight(settings,hdr=profile!='srgb')
    if appearance.source_only(values): return False
    importing = False
    try:
        luid = _composition_adapter(settings)
        combined = ((luid[0] & 0xffffffff)<<32) | luid[1]
        if combined != getattr(writer,'adapter_luid',None): return False
        bridge = _native_bridge(luid)
        if bridge is None: return False
        lib, handle = bridge.library, bridge.handle
        prepare, dispatch = lib.dlss5_quality_gpu_prepare, lib.dlss5_quality_compose_gpu
        prepare.argtypes = [ctypes.c_void_p,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.POINTER(ctypes.c_uint64)]
        prepare.restype = ctypes.c_int
        dispatch.argtypes = [ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_int,ctypes.c_int,
                            ctypes.c_int,ctypes.POINTER(ctypes.c_float),ctypes.c_int]
        dispatch.restype = ctypes.c_int
        hdr = profile != 'srgb'
        # compose_hdr_frame's final-view writer contract is RGBA16F, even
        # when its input happens to be float32.
        format_id = 1 if hdr else 0
        binding = bridge.gpu_bindings[0] if bridge.gpu_bindings else None
        if binding is None or binding[:3] != (writer,original.shape,format_id):
            if binding is not None:
                binding[-1].close()
                bridge.gpu_bindings.clear()
            descriptor = (ctypes.c_uint64*4)()
            if not prepare(handle,original.shape[1],original.shape[0],format_id,descriptor):
                release_native_composer(); return False
            importing = True
            lease = writer.import_shared_output(producer_pid=os.getpid(),handle=int(descriptor[0]),
                allocation=int(descriptor[1]),size=int(descriptor[2]),producer_luid=combined)
            importing = False
            binding = (writer,original.shape,format_id,int(descriptor[3]),lease)
            bridge.gpu_bindings.append(binding)
        base,nr,_,_ = _native_inputs(original,processed,order=order,profile=profile)
        payload = _native_quality_values(values,profile)
        payload[31] = {'srgb':0,'hdr10_pq':1,'hdr10_hlg':2}[profile]
        if hdr and format_id == 1:
            payload[31] += 4  # Explicit nearest-even float16 output quantization.
        if not dispatch(handle,ctypes.c_void_p(base.ctypes.data),ctypes.c_void_p(nr.ctypes.data),
                        original.shape[1],original.shape[0],2 if hdr else 0,(ctypes.c_float*32)(*payload),32):
            release_native_composer(); return False
    except (AttributeError,OSError,RuntimeError,ValueError,ctypes.ArgumentError):
        release_native_composer()
        if importing:
            # The isolated writer may have terminated on an import failure.
            # Do not pretend it can continue encoding via a CPU fallback.
            raise
        return False
    lease = binding[-1]
    writer.write_device(lease.pointer.value,lease.size,'rgba16f' if format_id==1 else 'rgba32f' if hdr else 'rgba',
                        combined,binding[3],strict_hdr=False)
    return True


def protection_mask(shape, q, *, full_shape=None, row_offset=0):
    h, w = shape[:2]
    full_h, full_w = (full_shape or shape)[:2]
    mask = np.zeros((h, w), np.float32)
    if not q['protection']:
        return mask
    x = np.arange(w, dtype=np.float32)[None, :] + .5
    y = np.arange(row_offset, row_offset+h, dtype=np.float32)[:, None] + .5
    for a, b, c, d in q['regions']:
        edge = np.minimum(np.minimum(x-a*full_w, c*full_w-x),
                          np.minimum(y-b*full_h, d*full_h-y))
        if q['feather']:
            t = np.clip(edge/q['feather'], 0, 1)
            edge = t*t*(3-2*t)
        else:
            edge = edge >= 0
        np.maximum(mask, edge, out=mask)
    return mask


def _hsl(rgb):
    hi, lo = rgb.max(-1), rgb.min(-1)
    delta = hi-lo
    safe = np.maximum(delta, 1e-6)
    light = (hi+lo)*.5
    r, g, b = np.moveaxis(rgb, -1, 0)
    hue = np.where(hi == r, (g-b)/safe, np.where(hi == g, (b-r)/safe+2, (r-g)/safe+4))/6
    saturation = delta/np.maximum(1-np.abs(2*light-1), 1e-6)
    return np.stack((np.where(delta > 1e-6, hue % 1, 0),
                     np.where(delta > 1e-6, np.clip(saturation, 0, 1), 0), light), -1)


def _rgb(hsl):
    h, s, l = np.moveaxis(hsl, -1, 0)
    q = np.where(l < .5, l*(1+s), l+s-l*s)
    p = 2*l-q
    def channel(offset):
        t = (h+offset) % 1
        return np.where(t < 1/6, p+(q-p)*6*t,
                        np.where(t < .5, q, np.where(t < 2/3, p+(q-p)*(2/3-t)*6, p)))
    return np.clip(np.stack((channel(1/3), channel(0), channel(-1/3)), -1), 0, 1)


def fine_controls(base, output, r):
    if all(v == 1 for v in r.values()):
        return output
    delta = (output-base)*r['residualMultiplier']
    candidate = np.clip(base+delta, 0, 1)
    def linear(x):
        return np.where(x <= .04045, x/12.92, (np.maximum(x+.055, 0)/1.055)**2.4)
    dy = (linear(candidate)-linear(base)) @ np.array([.2126, .7152, .0722], np.float32)
    direction = np.where(dy < 0, r['shadowStructureMultiplier'],
                         np.where(dy > 0, r['reflectionGlowMultiplier'], 1))
    candidate = np.clip(base+delta*direction[..., None], 0, 1)
    if r['residualSaturation'] != 1 or r['residualLightness'] != 1:
        original, adjusted = _hsl(base), _hsl(candidate)
        adjusted[..., 1:] = np.clip(original[..., 1:] + (adjusted[..., 1:]-original[..., 1:]) *
                                   [r['residualSaturation'], r['residualLightness']], 0, 1)
        candidate = _rgb(adjusted)
    return np.where(np.any(delta != 0, axis=-1, keepdims=True), candidate, base)


def hdr_fine_controls(base, output, r, weights=(.2126, .7152, .0722)):
    if all(v == 1 for v in r.values()):
        return output
    delta = (output-base)*r['residualMultiplier']
    dy = delta @ np.asarray(weights, np.float32)
    direction = np.where(dy < 0, r['shadowStructureMultiplier'],
                         np.where(dy > 0, r['reflectionGlowMultiplier'], 1))
    delta *= direction[..., None]
    luma = (delta @ np.asarray(weights, np.float32))[..., None]
    return base + luma*r['residualLightness'] + (delta-luma)*r['residualSaturation']


def global_controls(base, output, q, weights=(.2126, .7152, .0722), *,
                    full_shape=None, row_offset=0):
    raw = output-base
    delta = np.minimum(raw, 0)*q['darken'] + np.maximum(raw, 0)*q['brighten']
    if q['color'] != 1 or q['luminance'] != 1:
        dy = (delta @ np.array(weights, np.float32))[..., None]
        delta = dy*q['luminance']+(delta-dy)*q['color']
    delta *= q['total']*(1-protection_mask(
        base.shape, q, full_shape=full_shape, row_offset=row_offset)[..., None])
    anchor_delta = np.minimum(raw, 0)
    anchor = base+anchor_delta
    result = base+delta
    guard = (base >= 0) & (anchor > 0) & (delta < anchor_delta)
    result[guard] = anchor[guard]/(1+(anchor_delta[guard]-delta[guard])/anchor[guard])
    return result


def compose(original, processed, settings=None, *, order='rgb', profile='srgb'):
    if not settings or not appearance.active(settings):
        return processed
    hdr = profile != 'srgb'
    values = appearance.preflight(settings, hdr=hdr)
    if original.shape != processed.shape or original.dtype != processed.dtype:
        raise ValueError('NR composition requires matching frames')
    if hdr and profile not in ('hdr10_pq', 'hdr10_hlg', 'scrgb'):
        raise ValueError('Unsupported HDR composition profile')
    if appearance.source_only(values):
        # Identity must preserve even PQ near-black codes and signed/over-white
        # scRGB exactly, without a lossy decode/encode or a native dispatch.
        return original
    if (original.dtype in ((np.uint8,) if not hdr else (np.float16, np.float32, np.uint16))
            and original.shape[0] * original.shape[1] >= NATIVE_COMPOSITION_MIN_PIXELS
            and _native_library_path() is not None):
        try:
            adapter_luid = _composition_adapter(settings)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            adapter_luid = None
        native = (_native_compose(original, processed, values, order=order,
                                  adapter_luid=adapter_luid, profile=profile)
                  if adapter_luid is not None else None)
        if native is not None:
            return native
    divisor = np.iinfo(original.dtype).max if original.dtype.kind == 'u' else 1.0
    base = original[..., :3].astype(np.float32)/divisor
    output = processed[..., :3].astype(np.float32)/divisor
    if order == 'bgr':
        base, output = base[..., ::-1], output[..., ::-1]
    if hdr:
        from dlss5tool.video_export import _pq_eotf, _pq_oetf, _hlg_eotf, _hlg_oetf
        if profile not in ('hdr10_pq', 'hdr10_hlg', 'scrgb'):
            raise ValueError('Unsupported HDR composition profile')
        decode, encode = (_pq_eotf, _pq_oetf) if profile == 'hdr10_pq' else (_hlg_eotf, _hlg_oetf)
        if profile != 'scrgb':
            base, output = decode(base), decode(output)
        weights = (.2627, .6780, .0593) if profile != 'scrgb' else (.2126, .7152, .0722)
        fine = hdr_fine_controls
    else:
        weights = (.2126, .7152, .0722)
        fine = fine_controls

    height, width = base.shape[:2]
    workers = min(COMPOSITION_MAX_WORKERS, max(1, os.cpu_count() or 1))
    if height * width >= COMPOSITION_PARALLEL_MIN_PIXELS and workers > 1 and height >= workers:
        # NumPy releases the GIL for the large array operations used here. A
        # bounded row split keeps the UI/export worker responsive while
        # preserving full-frame protection-region coordinates.
        edges = np.linspace(0, height, workers + 1, dtype=int)

        def process_chunk(bounds):
            start, end = bounds
            chunk_base = base[start:end]
            chunk_output = (fine(chunk_base, output[start:end], values['nr_residual'], weights)
                            if hdr else fine(chunk_base, output[start:end], values['nr_residual']))
            return global_controls(
                chunk_base, chunk_output, values['nr_quality'], weights,
                full_shape=base.shape, row_offset=start,
            )

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='nr-compose') as pool:
            output = np.concatenate(list(pool.map(
                process_chunk, zip(edges[:-1].tolist(), edges[1:].tolist())
            )), axis=0)
    else:
        output = (fine(base, output, values['nr_residual'], weights)
                  if hdr else fine(base, output, values['nr_residual']))
        output = global_controls(base, output, values['nr_quality'], weights)
    if hdr and profile != 'scrgb':
        output = encode(np.maximum(output, 0))
    if profile != 'scrgb':
        output = np.clip(output, 0, 1)
    if order == 'bgr':
        output = output[..., ::-1]
    result = original.copy()  # Preserve source alpha.
    result[..., :3] = (np.rint(output*divisor) if divisor != 1 else output).astype(original.dtype)
    return result
