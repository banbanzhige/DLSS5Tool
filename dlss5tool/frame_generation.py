"""Opt-in experimental DLSSG video export, not production quality certification."""
import argparse
import ctypes
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import struct
import subprocess
import threading
import time
import uuid

import cv2
import numpy as np

from dlss5tool import i18n
from dlss5tool import paths
from dlss5tool.guidance_color import analysis_rgba8
from dlss5tool.video_export import (
    FFmpegHDRVideoReader, FFmpegVideoWriter, find_ffmpeg, find_ffprobe,
    probe_video_stream, compose_hdr_frame, compose_output_frame, tone_map_hdr_preview,
    resize_original,
)

PINNED_RUNTIME = '135eaf0733c1e37381a8c28abcf7a862404a54132b81787c04e35d09efc5e36f'
WARNING = ('实验功能：普通视频采用 NVOFA 估计光流及平面深度；遮挡/字幕/HDR 画质未完整验收。'
           '3×/4×目前仅限本机 RTX 4070 SUPER 与固定运行库，已知部分生成帧时间位置偏差。'
           '不会自动降倍率或位深；正常帧对生成无效时停止。')


class Cancelled(RuntimeError):
    pass


def check_cancel(cancel):
    if cancel.is_set():
        raise Cancelled('已取消插帧导出')


def runtime_files():
    worker = paths.runtime_root() / 'dlssg_video_worker.exe'
    bundled = paths.runtime_root() / 'nvngx_dlssg.dll'
    runtime = bundled if bundled.is_file() else paths.project_root() / 'third_party/NVIDIA-DLSS/lib/Windows_x86_64/rel/nvngx_dlssg.dll'
    return worker, runtime


def _read_exact(pipe, size):
    data = bytearray(size)
    view = memoryview(data)
    offset = 0
    while offset < size:
        count = pipe.readinto(view[offset:])
        if not count:
            raise RuntimeError('插帧工作进程退出或输出不完整，请查看 native.log')
        offset += count
    return data


class NativeStream:
    def __init__(self, width, height, multiplier, hdr, log_dir, cancel):
        worker, runtime = runtime_files()
        if not worker.is_file() or not runtime.is_file():
            raise RuntimeError('缺少插帧组件。源码环境请先运行 scripts\\build_dlssg_video.bat。')
        with runtime.open('rb') as handle:
            self.runtime_hash = hashlib.file_digest(handle, 'sha256').hexdigest()
        if multiplier > 2 and self.runtime_hash != PINNED_RUNTIME:
            raise RuntimeError('3×/4×实验路径与当前 DLSSG 运行库不匹配，未自动换库')
        self.shape = (height, width, 4)
        self.dtype = np.float16 if hdr else np.uint8
        self.bytes = height * width * 4 * np.dtype(self.dtype).itemsize
        self.multiplier, self.cancel = multiplier, cancel
        self.log = (log_dir / 'native.log').open('wb')
        self.proc = None
        try:
            self.proc = subprocess.Popen(
                [str(worker), str(runtime.parent), str(log_dir), str(width), str(height), str(multiplier),
                 'hdr-coded' if hdr else 'sdr'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self.log, cwd=log_dir, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), bufsize=0,
            )
            magic, low, high = struct.unpack('<III', self.call(lambda: _read_exact(self.proc.stdout, 12)))
            if magic != 0x31474746:
                raise RuntimeError('插帧组件协议不匹配')
            self.luid = (high << 32) | low
        except BaseException:
            self.close()
            raise

    def call(self, operation, timeout=120):
        result = queue.Queue(maxsize=1)
        def run():
            try:
                result.put((True, operation()))
            except BaseException as exc:
                result.put((False, exc))
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.monotonic() + timeout
        while True:
            if self.cancel.is_set() or time.monotonic() > deadline:
                if self.proc and self.proc.poll() is None:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
                thread.join(timeout=2)
                check_cancel(self.cancel)
                raise RuntimeError('插帧工作进程超时，已停止；请查看 native.log')
            try:
                ok, value = result.get(timeout=.1)
                if not ok:
                    raise value
                return value
            except queue.Empty:
                continue

    def process(self, rgba, motion, reset):
        if rgba.shape != self.shape or rgba.dtype != self.dtype:
            raise ValueError('插帧颜色尺寸或位深不匹配')
        if motion.shape != self.shape[:2] + (2,) or not np.isfinite(motion).all():
            raise ValueError('插帧运动向量不匹配')
        if self.dtype == np.float16 and (not np.isfinite(rgba).all() or rgba.min() < 0 or rgba.max() > 1):
            raise ValueError('HDR 插帧只接受归一化 PQ/HLG 值，未做截断或 SDR 转换')
        def exchange():
            for block in (struct.pack('<I', int(reset)), memoryview(np.ascontiguousarray(rgba)).cast('B'),
                          memoryview(np.ascontiguousarray(motion, dtype=np.float32)).cast('B')):
                view = memoryview(block)
                while view:
                    written = self.proc.stdin.write(view)
                    if not written:
                        raise RuntimeError('插帧输入管道已关闭')
                    view = view[written:]
            self.proc.stdin.flush()
            frames, valid = [], []
            for _ in range(self.multiplier - 1):
                valid.append(struct.unpack('<I', _read_exact(self.proc.stdout, 4))[0] == 1)
                frame = np.frombuffer(_read_exact(self.proc.stdout, self.bytes), dtype=self.dtype).reshape(self.shape)
                if self.dtype == np.float16 and not reset and (not np.isfinite(frame).all() or frame.min() < 0 or frame.max() > 1):
                    raise RuntimeError('DLSSG 返回无效 HDR 数值，未截断或降为 SDR')
                frames.append(frame)
            return frames, all(valid)
        return self.call(exchange)

    def close(self):
        if self.proc:
            try:
                if self.proc.poll() is None:
                    self.proc.stdin.write(struct.pack('<I', 2))
                    self.proc.stdin.flush()
                    self.proc.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                if self.proc.poll() is None:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
            for pipe in (self.proc.stdin, self.proc.stdout):
                if pipe:
                    pipe.close()
        self.log.close()


def validate_timestamps(timestamps, rate):
    """Reject VFR instead of silently re-timing it through the existing CFR writer."""
    expected = 1 / rate
    first = previous = None
    count = 0
    for timestamp in timestamps:
        timestamp = Fraction(timestamp)
        if first is None:
            first = timestamp
        elif abs((timestamp - previous) - expected) > max(Fraction(2, 1000000), expected / 500):
            raise ValueError('这个视频是变帧率或时间戳不连续，实验入口暂不重定时；未转换为恒定帧率')
        previous = timestamp
        count += 1
    if not count:
        raise ValueError('视频没有可解码画面')
    if abs(first) > expected / 2:
        raise ValueError('视频起始时间不为零，实验入口暂不处理音画偏移；请使用从零开始的测试片段')
    return count


def _emit_progress(progress, text, fraction, counts=None):
    if not progress:
        return
    try:
        progress(text, fraction, counts)
    except TypeError:
        progress(text, fraction)


def inspect_source(source, cancel, progress=None):
    ffmpeg = find_ffmpeg()
    probe = find_ffprobe(ffmpeg)
    if not probe:
        raise RuntimeError('实验入口需要 ffprobe 检查真实帧时间戳，请安装完整 FFmpeg')
    meta = probe_video_stream(ffmpeg, source)
    try:
        rate = Fraction(meta['r_frame_rate'])
        width, height = int(meta['width']), int(meta['height'])
        if rate <= 0:
            raise ValueError()
    except (KeyError, ValueError, ZeroDivisionError):
        raise ValueError('无法确定视频尺寸和准确帧率') from None
    try:
        expected = int(meta.get('frames') or meta.get('nb_frames') or 0)
    except (TypeError, ValueError):
        expected = 0
    def emit(done, total=expected):
        if total:
            text = i18n.tr('status.inspect_timestamps')
            fraction = min(done / total, 1.0) if total else 0
            _emit_progress(progress, text, fraction, (done, total))
        else:
            _emit_progress(
                progress, i18n.tr('status.inspect_timestamps_unknown', done=done), 0,
            )
    emit(0)
    proc = subprocess.Popen([probe, '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                             'frame=best_effort_timestamp_time', '-of', 'csv=p=0', str(source)],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    events = queue.Queue(maxsize=128)
    stop = threading.Event()
    def reader():
        try:
            for line in proc.stdout:
                while not stop.is_set():
                    try:
                        events.put(line, timeout=.1)
                        break
                    except queue.Full:
                        pass
                if stop.is_set():
                    break
        finally:
            while not stop.is_set():
                try:
                    events.put(None, timeout=.1)
                    break
                except queue.Full:
                    pass
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    def timestamps():
        deadline = time.monotonic() + 60
        last_report = 0.0
        count = 0
        while True:
            check_cancel(cancel)
            if time.monotonic() > deadline:
                raise RuntimeError('时间戳扫描无响应')
            try:
                line = events.get(timeout=.1)
            except queue.Empty:
                continue
            if line is None:
                break
            deadline = time.monotonic() + 60
            field = line.decode().strip().split(',')[0]
            if field:
                count += 1
                now = time.monotonic()
                if count == 1 or (expected and count >= expected) or now - last_report >= 0.2:
                    last_report = now
                    emit(count)
                yield field
    try:
        count = validate_timestamps(timestamps(), rate)
        if proc.wait(timeout=10):
            raise RuntimeError('ffprobe 时间戳扫描失败')
    finally:
        stop.set()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        thread.join(timeout=2)
        proc.stdout.close()
    emit(count, expected or count)
    return meta, rate, width, height, count


def is_cut(previous, current):
    # Conservative experimental cut detector; does not certify arbitrary edits.
    a = cv2.resize(previous[..., :3], (64, 64), interpolation=cv2.INTER_AREA).astype(np.float32)
    b = cv2.resize(current[..., :3], (64, 64), interpolation=cv2.INTER_AREA).astype(np.float32)
    return float(np.mean(np.abs(a-b))) / 255 > .45


def export_video(source, output, *, multiplier=2, scale=1, enhance=False, settings=None,
                 cancel=None, progress=None, log_dir=None, frame_sink=None, render_gate=None,
                 metadata_sink=None, input_session=None, source_inspector=None):
    cancel = cancel or threading.Event()
    progress = progress or (lambda text, value: None)
    source = Path(source).resolve(strict=True)
    output = Path(output).resolve() if output is not None else None
    render_only = frame_sink is not None
    if not render_only and output is None:
        raise ValueError('输出路径缺失')
    if output is not None and (output.exists() or source == output):
        raise ValueError('请使用新的输出文件名，不覆盖源文件或已有结果')
    if output is not None and output.suffix.lower() not in ('.mp4', '.mkv', '.mov'):
        raise ValueError('请选择 MP4 / MKV / MOV 输出文件')
    if type(multiplier) is not int or multiplier not in (1, 2, 3, 4) or type(scale) is not int or scale not in (1, 2, 4):
        raise ValueError('不支持的倍率')
    if output is not None and not output.parent.is_dir():
        raise ValueError('输出目录不存在')
    log_root = (Path(os.environ['DLSS5TOOL_FG_LOG_ROOT']) if os.environ.get('DLSS5TOOL_FG_LOG_ROOT')
                else paths.state_path('fg-experiments'))
    log_dir = Path(log_dir) if log_dir else log_root / uuid.uuid4().hex
    log_dir.mkdir(parents=True, exist_ok=False)
    report = {'experimental': True, 'warning': WARNING, 'source': str(source), 'output': str(output),
              'multiplier': multiplier, 'spatial_scale': scale, 'enhance': enhance, 'status': 'running',
              'real_frames': 0, 'generated_frames': 0, 'cut_holds': 0, 'endpoint_holds': 0}
    writer = native = flow = capture = sr = nr = None
    staging = output.with_name(f'.{output.stem}.fg-{uuid.uuid4().hex}{output.suffix}') if output else None
    try:
        if input_session is None:
            inspect = source_inspector or inspect_source
            try:
                meta, rate, w, h, total = inspect(source, cancel, progress=progress)
            except TypeError:
                meta, rate, w, h, total = inspect(source, cancel)
        else:
            upstream = input_session.wait_metadata(cancel)
            meta = upstream['source_metadata']
            rate = Fraction(upstream['source_rate'])
            w, h = upstream['width'], upstream['height']
            total = upstream['source_frames']
        config = dict(settings or {})
        process_w, process_h = (w, h) if input_session else (w*scale, h*scale)
        ow, oh = config.get('output_size') or (process_w, process_h)
        view = int(config.get('output_view', 0))
        if view not in (0, 1, 2):
            raise ValueError('不支持的输出预览模式')
        if min(ow, oh) < 128 or max(ow, oh) > 8192 or ow % 2 or oh % 2:
            raise ValueError('实验入口要求最终宽高均为偶数、128～8192，不会自动缩小或裁剪')
        estimate = ow*oh*(8 if meta['is_hdr'] else 4)*20
        disk_need = source.stat().st_size*multiplier*scale*scale*3
        if not render_only and shutil.disk_usage(output.parent).free < disk_need + 15*1024**3:
            raise ValueError('输出盘余量不足预计峰值＋15 GiB，请选择其他输出目录')
        from dlss5tool.super_resolution import query_gpu_memory
        memory = query_gpu_memory(cache_seconds=0)
        if memory and memory['free_bytes'] < estimate + 1024**3:
            raise ValueError('当前显存余量不足此配置，请关闭其他 GPU 任务或自行更改配置；未自动降级')
        hdr = bool(meta['is_hdr'] and config.get('hdr_mode', True))
        config.update(frame_format='rgba16f' if hdr else 'rgba8', color_profile=meta['profile'] if hdr else 'srgb',
                      color_primaries=meta['color_primaries'], host_auto_fallback=False,
                      host_in_flight=1, guidance_flow_fallback=False)
        if multiplier > 1:
            _emit_progress(progress, i18n.tr('status.init_dlssg'), 0)
        native = NativeStream(ow, oh, multiplier, hdr, log_dir, cancel) if multiplier > 1 else None
        need_reference = bool(view or render_only)
        from dlss5tool.nvofa import OpticalFlow, align_size
        fw, fh = align_size(ow, oh)
        if native:
            flow = OpticalFlow(fw, fh, grid=1)
            raw_luid = ctypes.create_string_buffer(8)
            node = ctypes.c_uint32()
            get_luid = flow._bind(flow.cuda, 'cuDeviceGetLuid', ctypes.c_int,
                                 ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int)
            flow.check(get_luid(raw_luid, ctypes.byref(node), flow.device), 'CUDA LUID')
            if int.from_bytes(raw_luid.raw, 'little') != native.luid:
                raise RuntimeError('NVOFA 与 DLSSG 不在同一物理显卡，已停止，未自动换卡')
        if scale > 1 and input_session is None:
            from dlss5tool.super_resolution import ProcessSuperResolution
            sr = ProcessSuperResolution(w, h, scale, is_hdr=hdr)
        if enhance and (input_session is None or config.get('_render_stage') == 'base'):
            from dlss5tool.dlss_host_process import ProcessLive
            nr = ProcessLive(process_w, process_h, config)
        if input_session is None:
            capture = FFmpegHDRVideoReader(source, w, h, meta) if meta['is_hdr'] else cv2.VideoCapture(str(source))
        writer = None if render_only else FFmpegVideoWriter(staging, ow, oh, float(rate*multiplier), audio_source=source,
                                   use_nvenc=True, hdr_metadata=meta if hdr else None,
                                   nvenc_preset=config.get('nvenc_preset', 'p5'),
                                   rate_control=config.get('rate_control', 'quality'),
                                   quality_profile=config.get('quality_profile', 'high'),
                                   video_bitrate_mbps=config.get('video_bitrate_mbps', 20))
        report.update(width=ow, height=oh, source_rate=str(rate), output_rate=str(rate*multiplier),
                      color=meta['profile'] if hdr else 'srgb', output_view=view,
                      runtime_sha256=native.runtime_hash if native else None, log_dir=str(log_dir))
        scene_cuts = []
        def publish_metadata():
            if metadata_sink:
                metadata_sink({**report, 'source_frames': total, 'source_metadata': meta,
                               'scene_cuts': tuple(scene_cuts), 'hdr_metadata': meta if hdr else None})
        publish_metadata()
        previous = previous_proxy = previous_scene_proxy = None
        previous_reference = None
        index = 0
        output_index = 0
        def write(rgba, reference=None, mix_source=None):
            nonlocal output_index
            check_cancel(cancel)
            if render_only:
                if mix_source is None:
                    frame_sink(output_index, rgba, reference)
                else:
                    frame_sink(output_index, rgba, reference, mix_source=mix_source)
                output_index += 1
                return
            if view:
                if hdr:
                    rgba = compose_hdr_frame(reference, rgba, view=view, mix=1, profile=meta['profile'])
                else:
                    result = compose_output_frame(cv2.cvtColor(reference, cv2.COLOR_RGBA2BGR),
                        cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR), view=view, mix=1)
                    writer.write(result)
                    return
            writer.write(rgba if hdr else cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR))
        def motion_for(proxy, earlier, reset):
            if reset:
                return np.zeros((oh, ow, 2), np.float32)
            a = cv2.copyMakeBorder(proxy[..., :3], 0, fh-oh, 0, fw-ow, cv2.BORDER_REPLICATE)
            b = cv2.copyMakeBorder(earlier[..., :3], 0, fh-oh, 0, fw-ow, cv2.BORDER_REPLICATE)
            return np.ascontiguousarray(flow.calculate(a, b)[:oh, :ow])
        def final_size(rgba):
            if (ow, oh) != (process_w, process_h):
                dtype = rgba.dtype
                working = rgba.astype(np.float32) if dtype == np.float16 else rgba
                return np.ascontiguousarray(cv2.resize(working, (ow, oh), interpolation=cv2.INTER_LANCZOS4).astype(dtype))
            return np.ascontiguousarray(rgba)
        while True:
            check_cancel(cancel)
            if render_gate:
                render_gate(index)
            mix_source = None
            if input_session is not None:
                if index >= total:
                    break
                pair = input_session.wait(index, cancel)
                rgba, reference = pair
                mix_source = getattr(pair, 'mix_source', reference)
            elif meta['is_hdr']:
                rgba = capture.read()
                if rgba is None:
                    break
                if not hdr:
                    coded_bgr = np.ascontiguousarray((np.clip(rgba[..., 2::-1], 0, 1)*255+.5).astype(np.uint8))
                    rgba = cv2.cvtColor(tone_map_hdr_preview(coded_bgr, meta), cv2.COLOR_BGR2RGBA)
            else:
                ok, bgr = capture.read()
                if not ok:
                    break
                rgba = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA)
            if rgba.shape[:2] != (h, w):
                raise ValueError('视频帧尺寸变化，已停止')
            if input_session is None and need_reference:
                # Capture before VSR: the original side must contain source pixels.
                reference = resize_original(rgba, (ow, oh)).copy()
            if sr:
                rgba = sr.process(rgba).copy()
            scene_proxy = analysis_rgba8(rgba, config)
            cut = previous_scene_proxy is not None and is_cut(previous_scene_proxy, scene_proxy)
            if input_session is not None:
                cut = index in input_session.metadata.get('scene_cuts', ())
            if cut:
                scene_cuts.append(index)
                publish_metadata()
            if not need_reference:
                reference = None
            if nr:
                mix_source = rgba
                processed = nr.process(rgba, reset=index == 0 or cut)
                if processed is None:
                    raise RuntimeError('DLSS 5 增强失败')
                if hdr:
                    rgba = compose_hdr_frame(rgba, processed, view=0, mix=1 if view else config.get('output_mix', 1), profile=meta['profile'])
                else:
                    mixed = compose_output_frame(cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR),
                        cv2.cvtColor(processed, cv2.COLOR_RGBA2BGR), view=0, mix=1 if view else config.get('output_mix', 1))
                    rgba = cv2.cvtColor(mixed, cv2.COLOR_BGR2RGBA)
            elif input_session is not None and config.get('_render_stage') == 'output':
                if hdr:
                    rgba = compose_hdr_frame(mix_source, rgba, view=0,
                        mix=config.get('output_mix', 1), profile=meta['profile'])
                else:
                    rgba = cv2.cvtColor(compose_output_frame(
                        cv2.cvtColor(mix_source, cv2.COLOR_RGBA2BGR),
                        cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR), view=0,
                        mix=config.get('output_mix', 1)), cv2.COLOR_BGR2RGBA)
            rgba = final_size(rgba)
            if input_session is not None and config.get('_render_stage') == 'output' and reference is not None:
                reference = final_size(reference)
            proxy = analysis_rgba8(rgba, config)
            if native:
                motion = motion_for(proxy, previous_proxy, previous is None or cut)
                frames, valid = native.process(rgba, motion, reset=previous is None or cut)
            else:
                frames, valid = [], True
            if previous is not None:
                if not cut and not valid:
                    raise RuntimeError(f'帧对 {index-1}→{index} 返回无效插帧，已停止，未补重复帧')
                for interpolated in frames:
                    # No source frame exists at an interpolated timestamp. Hold
                    # the preceding real source, never synthesize an "original".
                    write(previous if cut else interpolated, previous_reference)
                report['cut_holds' if cut else 'generated_frames'] += multiplier-1
            # Real frames do not depend on a future frame. Publish immediately;
            # the next iteration inserts the intervening generated frames.
            write(rgba, reference, mix_source=(mix_source
                  if config.get('_render_stage') == 'base' and scale > 1 else None))
            previous, previous_proxy = rgba.copy(), proxy.copy()
            previous_reference = reference
            previous_scene_proxy = cv2.resize(scene_proxy, (64, 64), interpolation=cv2.INTER_AREA)
            index += 1
            report['real_frames'] = index
            progress(f'处理 {index}/{total} 源帧 · 输出 {rate*multiplier} fps', min(index/total, .99))
        if index != total or previous is None:
            raise RuntimeError(f'解码帧数与时间戳扫描不一致：{index}/{total}')
        for _ in range(multiplier-1):
            write(previous, previous_reference)
        report['endpoint_holds'] = multiplier-1
        progress('编码完成，正在封装原音轨…', .99)
        if writer:
            writer.finish()
        check_cancel(cancel)
        if writer:
            os.rename(staging, output)  # Windows refuses replacement if another writer won.
        report['status'] = 'complete'
        progress(f'已导出：{output}', 1)
        return report
    except BaseException as error:
        report.update(status='cancelled' if isinstance(error, Cancelled) else 'failed', error=str(error))
        if isinstance(error, Cancelled):
            raise
        raise RuntimeError(f'{error}\n诊断记录：{log_dir}') from error
    finally:
        cleanup_errors = []
        actions = []
        if writer and report['status'] != 'complete':
            actions.append(writer.abort)
        for item in (native, flow, capture, nr, sr):
            if item is not None:
                actions.append(item.close if hasattr(item, 'close') else item.release)
        for action in actions:
            try:
                action()
            except Exception as cleanup:
                cleanup_errors.append(str(cleanup))
        if staging is not None and staging.exists():
            try:
                staging.unlink()
            except OSError as cleanup:
                cleanup_errors.append(str(cleanup))
        report['cleanup_errors'] = cleanup_errors
        (log_dir / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


def frozen_check_main(argv):
    """Explicit release verification entry, using the production shared cache."""
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('report', type=Path)
    parser.add_argument('--multiplier', type=int, choices=[1, 2, 3, 4], default=2)
    parser.add_argument('--scale', type=int, choices=[1, 2, 4], default=1)
    args = parser.parse_args(argv)
    if args.report.exists() or args.output.exists():
        raise ValueError('Verification output already exists')
    from dlss5tool.render_cache import RenderCache, encode_cached
    config = {'frame_generation_multiplier': args.multiplier, 'super_resolution_scale': args.scale,
              'guidance_mode': 0, 'host_backend': 'v2', 'dlss_runtime': '__bundled__',
              'host_zero_fast_path': True, 'hdr_mode': True, 'output_mix': .7, 'intensity': .5}
    manager = RenderCache(1024**3)
    cancel = threading.Event()
    timer = threading.Timer(180, cancel.set)
    timer.start()
    try:
        session = manager.session(args.source, config)
        metadata = session.wait_metadata(cancel)
        for index in range(metadata['source_frames']*args.multiplier):
            session.wait(index, cancel)
        result = encode_cached(session, args.output, config, cancel, lambda *_: None)
        result['runtime_files'] = [str(p) for p in runtime_files()]
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    finally:
        timer.cancel()
        manager.close()


def main():
    parser = argparse.ArgumentParser(description=WARNING)
    parser.add_argument('source')
    parser.add_argument('output')
    parser.add_argument('--multiplier', type=int, choices=[2, 3, 4], default=2)
    parser.add_argument('--scale', type=int, choices=[1, 2, 4], default=1)
    parser.add_argument('--enhance', action='store_true')
    args = parser.parse_args()
    export_video(args.source, args.output, multiplier=args.multiplier, scale=args.scale,
                 enhance=args.enhance, progress=lambda message, _: print(message, flush=True))


if __name__ == '__main__':
    main()
