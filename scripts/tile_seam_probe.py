"""Compare a chosen native host on a still image; never replaces DLLs or inputs."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run(args):
    import numpy as np
    from PIL import Image
    from dlss5tool import app_settings
    from dlss5tool import dlss_engine as engine
    from dlss5tool.gui import _large_image_host_settings
    from dlss5tool.super_resolution import ProcessSuperResolution

    args.output.mkdir(parents=True, exist_ok=False)
    engine.HOST_DLL_V2 = str(args.dll.resolve())
    engine.LOG_PATH = str((args.output / 'ngx.log').resolve())
    source = Image.open(args.source).convert('RGBA')
    if args.max_edge:
        source.thumbnail((args.max_edge,args.max_edge))
    frame = np.array(source)
    h, w = frame.shape[:2]
    if args.scale > 1:
        with ProcessSuperResolution(w, h, args.scale) as sr:
            frame = sr.process(frame)
    h, w = frame.shape[:2]
    settings = _large_image_host_settings(w, h, {
        **app_settings.DEFAULTS, 'preset': 1, 'guidance_mode': 0,
        'host_backend': 'v2', 'host_auto_fallback': False, 'host_in_flight': 1,
        'local_tone': 1.0, 'local_struct': 1.0,
        'host_persistent_buffers': not args.transient,
    })
    if args.force_tile:
        settings.update(host_tiled_mode=True, host_tile_width=args.force_tile,
                        host_tile_height=args.force_tile)
    if args.hdr:
        # Synthetic scRGB checks transport/half blending, not HDR visual quality.
        frame = frame.astype(np.float16) / np.float16(255)
        settings.update(frame_format='rgba16f', color_profile='scrgb')
    started = time.perf_counter()
    live = engine.Live(w, h, settings)
    try:
        result = live.process(frame, reset=True)
        if result is None:
            raise RuntimeError('No DLSS output')
        result = result.copy()
        print(f'First DLSS frame: {time.perf_counter()-started:.2f}s',flush=True)
        if args.repeat:
            repeated = live.process(frame, reset=True)
            if repeated is None or not np.array_equal(result, repeated):
                raise RuntimeError('Repeated still output differs')
    finally:
        # Probe runs in its own disposable process, matching the application's
        # NGX isolation. Some runtime builds spin during explicit Shutdown1.
        live.close_guidance()
    report = {'source': str(args.source), 'dll': str(args.dll), 'size': [w, h],
              'scale': args.scale, 'seconds': time.perf_counter() - started,
              'settings': settings, 'finite': bool(np.isfinite(result).all())}
    if args.save_array:
        np.save(args.output / 'output.npy', result)
    if args.hdr:
        np.save(args.output / 'output.npy', result)
    else:
        Image.fromarray(result).convert('RGB').save(args.output / 'output.jpg', quality=96)
        # Save exact lossless crops through OLD seams for before/after comparison.
        for axis, positions in [('x',range(6000,w,6000)),('y',range(3000,h,3000))]:
            for p in positions:
                box = (p-240,max(0,h//2-400),p+240,min(h,h//2+400)) if axis == 'x' else (
                    max(0,w//2-400),p-240,min(w,w//2+400),p+240)
                for name, array in [('source',frame),('output',result)]:
                    Image.fromarray(array).convert('RGB').crop(box).save(args.output / f'{axis}{p}-{name}.png')
        # Scan every row/column: a seam moving with a new grid is NOT a fix.
        for axis, dim in [('x',1),('y',0)]:
            metrics = []
            for p in range(1, w if dim else h):
                if dim:
                    left = result[:,p-1,:3].astype(np.float32)-frame[:,p-1,:3]
                    right = result[:,p,:3].astype(np.float32)-frame[:,p,:3]
                else:
                    left = result[p-1,:,:3].astype(np.float32)-frame[p-1,:,:3]
                    right = result[p,:,:3].astype(np.float32)-frame[p,:,:3]
                metrics.append(float(np.abs(right-left).mean()))
            np.save(args.output / f'{axis}-residual-jumps.npy', np.array(metrics))
            report[axis+'_largest_jumps'] = sorted(enumerate(metrics,1),key=lambda pair:pair[1],reverse=True)[:10]
        thumb = Image.fromarray(result).convert('RGB')
        thumb.thumbnail((1400,1400))
        thumb.save(args.output / 'overview.jpg',quality=95)
    (args.output / 'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k != 'settings'},ensure_ascii=False),flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--dll',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--scale',type=int,choices=[1,2,4],default=1)
    parser.add_argument('--force-tile',type=int,default=0)
    parser.add_argument('--transient',action='store_true')
    parser.add_argument('--hdr',action='store_true')
    parser.add_argument('--repeat',action='store_true')
    parser.add_argument('--max-edge',type=int,default=0)
    parser.add_argument('--save-array',action='store_true')
    run(parser.parse_args())
