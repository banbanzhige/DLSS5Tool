"""Bounded profiling of the real NR-only Tk preview or GUI export path.

Keep sources read only and product settings isolated inside a registered task.
Stage wall times overlap and do not represent GPU kernel timings.
"""
import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class Profile:
    def __init__(self):
        self.lock = threading.Lock()
        self.times = defaultdict(list)
        self.encoded = []

    def wrap(self, name, function):
        def invoke(*args, **kwargs):
            start = time.perf_counter()
            try:
                result = function(*args, **kwargs)
                if name == 'FFmpegVideoWriter.write':
                    self.encoded.append(time.perf_counter())
                return result
            finally:
                with self.lock:
                    self.times[name].append(time.perf_counter() - start)
        return invoke

    def summary(self):
        with self.lock:
            return {name: {'calls': len(values), 'total_seconds': sum(values),
                'mean_ms': statistics.mean(values) * 1000,
                'warm_mean_ms': statistics.mean(values[4:] or values) * 1000,
                'p95_ms': sorted(values)[min(len(values)-1, int(len(values)*.95))] * 1000}
                for name, values in self.times.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--mode', choices=('preview', 'export'), default='preview')
    parser.add_argument('--frames', type=int, default=64, help='Preview prefix, 16..96 frames')
    parser.add_argument('--poll-ms', type=int, choices=(4, 8, 16), help='Diagnostic preview scheduler interval')
    parser.add_argument('--neutral-composition', action='store_true', help='A/B only; leave NR inference controls unchanged')
    parser.add_argument('--gpu-composition', action='store_true', help='A/B only; use installed same-card GPU composition sink')
    parser.add_argument('--timeout', type=float, default=120)
    args = parser.parse_args()
    task = args.task.absolute()
    if (task != task.resolve() or not task.is_relative_to(ROOT / 'tmp')
            or any(p.is_symlink() or p.is_junction() for p in (task, *task.parents))):
        raise ValueError('Task must be inside repository tmp without links')
    if not (task / 'TASK.md').is_file() or not args.label.replace('-', '').isalnum():
        raise ValueError('Registered task and plain label required')
    if not 16 <= args.frames <= 96:
        raise ValueError('Preview prefix must be 16..96 frames')
    report_path = task / (args.label + '.json')
    if report_path.exists():
        raise FileExistsError(report_path)
    source = args.source.resolve(strict=True)
    settings = json.loads((ROOT / 'var/dlss5_settings.json').read_text(encoding='utf-8'))
    settings.update(super_resolution_scale=1, frame_generation_multiplier=1,
        preview_super_resolution=False, preview_frame_generation=False,
        preview_auto_prerender=args.mode == 'preview', preview_cache_mb=2048,
        preview_detached=False, guidance_mode=0, workbench_mode='local')
    if args.neutral_composition:
        from dlss5tool import appearance
        settings.update(appearance.settings({}))
    settings_path = task / (args.label + '-settings.json')
    with settings_path.open('x', encoding='utf-8') as handle:
        json.dump(settings, handle)
    for key, name in (('SETTINGS', args.label + '-settings.json'),
                      ('QUEUE', args.label + '-queue.json'),
                      ('EXPORT_HISTORY', args.label + '-history.json')):
        os.environ[f'DLSS5TOOL_{key}_PATH'] = str(task / name)
    os.environ['TEMP'] = os.environ['TMP'] = str(task)

    import tkinter as tk
    from dlss5tool import gui, gpu_decode, dlss_host_process, video_export, residual_composition
    from dlss5tool.memory_budget import memory_events
    if args.gpu_composition:
        from dlss5tool.gpu_export_runtime import create_video_writer, load_components
        components = load_components()
        if components is None:
            raise RuntimeError('Verified GPU writer components required')
        gui.FFmpegVideoWriter = lambda *a, **kw: create_video_writer(*a, gpu_options=components, **kw)
    if args.poll_ms is not None:
        gui.PREVIEW_BACKGROUND_TICK_MS = args.poll_ms

    profile = Profile()
    for module, field in ((gpu_decode.GPUVideoCapture, 'read'),
                          (dlss_host_process.ProcessLive, 'enqueue'),
                          (dlss_host_process.ProcessLive, 'dequeue'),
                          (dlss_host_process.ProcessLive, 'update'),
                          (video_export.FFmpegVideoWriter, 'write'),
                          (video_export.FFmpegVideoWriter, 'finish'),
                          (video_export, 'compose_sdr_rgba'),
                          (residual_composition, '_native_inputs'),
                          (residual_composition, '_native_compose_frame'),
                          (gui, '_postprocess_and_write')):
        setattr(module, field, profile.wrap(module.__name__ + '.' + field, getattr(module, field)))
    root = tk.Tk()
    root.withdraw()
    errors = []
    root.report_callback_exception = lambda *exc: errors.append(''.join(traceback.format_exception(*exc)))
    app = gui.App(root)
    for name in ('_preview_decode_tick', '_queue_preview_frame', '_source_cache_store',
                 '_cache_store', '_settings_hash', '_preview_render_settings',
                 '_available_frame_cache_bytes', '_update_preview_timeline_and_status',
                 '_present_current_cached_preview', '_ensure_live', 'display_view'):
        setattr(app, name, profile.wrap(name, getattr(app, name)))
    completions = []
    original_store = app._cache_store
    def store(*args):
        original_store(*args)
        completions.append(time.perf_counter())
    app._cache_store = store
    started = time.perf_counter()
    report = {'passed': False, 'mode': args.mode,
              'scope': 'real GUI path; overlapping stage wall times, not GPU kernel times',
              'neutral_composition': args.neutral_composition,
              'gpu_composition': args.gpu_composition,
              'preview_poll_ms': gui.PREVIEW_BACKGROUND_TICK_MS}
    try:
        assert app._load_video(str(source))
        report.update(source_size=list(app._source_size()), source_frames=app.nframes, fps=app.fps)
        if app._media_w * app._media_h > 3840*2160:
            raise ValueError('Probe limited to the 4K pixel budget')
        if args.mode == 'preview':
            app.nframes = min(app.nframes, args.frames)
            pw, ph = app._playback_preview_size()
            needed = (app._media_w * app._media_h + pw * ph) * 3 * app.nframes
            if needed + 64*1024**2 > app._preview_cache_bytes():
                raise ValueError('Reduce --frames to fit the bounded preview cache')
            report['frames'] = app.nframes
            deadline = time.perf_counter() + args.timeout
            while time.perf_counter() < deadline:
                root.update()
                if errors:
                    raise RuntimeError(errors)
                if len(app._dlss_frame_cache) == app.nframes and not app._dlss_pending:
                    break
                time.sleep(.002)
            else:
                raise TimeoutError(str(app.eta_label.cget('text')))
            report.update(steady_fps=(len(completions)-5)/(completions[-1]-completions[4]),
                first_result_seconds=completions[0]-started,
                input_sha256=[hashlib.sha256(memoryview(app._source_frame_cache[i])).hexdigest()
                              for i in range(min(8, app.nframes))])
        else:
            if app.nframes > 768:
                raise ValueError('Use a clip of at most 768 frames for the export probe')
            output = task / (args.label + '.mp4')
            if output.exists():
                raise FileExistsError(output)
            result = app._export_video_source(str(source), app._collect_settings(),
                app._collect_export_settings(), app._video_color_info,
                out_path=str(output), notify=False)
            assert result['success'], result
            report.update(result=result)
            if len(profile.encoded) > 5:
                report['steady_encode_fps'] = (len(profile.encoded)-5)/(profile.encoded[-1]-profile.encoded[4])
        report.update(passed=True, elapsed_seconds=time.perf_counter()-started,
                      stages=profile.summary(), callback_errors=errors,
                      memory_events=memory_events())
        print(json.dumps({k: report[k] for k in report if k not in ('stages','input_sha256')},
                         ensure_ascii=False), flush=True)
    except Exception:
        report['error'] = traceback.format_exc()
        report['stages'] = profile.summary()
        raise
    finally:
        app._freeze_preview_cache(resume_ms=None)
        worker = getattr(app, '_play_dlss_thread', None)
        if worker is not None:
            worker.join(timeout=5)
        report['worker_retired'] = worker is None or not worker.is_alive()
        app._on_close()
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
