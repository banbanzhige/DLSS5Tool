"""Sequential, cancellable export of visualization-only guidance images/video."""
import os
import math
from pathlib import Path
import tempfile

import cv2

from guidance_client import GuidanceSession
from guidance_visualization import guidance_images
from guidance_parameters import analysis_edge
from video_export import FFmpegVideoWriter


class GuidanceExportCancelled(Exception):
    pass


def export_guidance(source, destination, settings, target, *, frame=None,
                    still=None, cancel=None, progress=None,
                    session_factory=None, writer_factory=None):
    """Write a clean visualization at source dimensions, without UI or audio.

    frame=None exports the entire video. Otherwise write exactly that PNG frame;
    seed optical flow with its actual predecessor. Commit output only on success.
    """
    mode = int(settings.get('guidance_mode', 0))
    if target not in ('depth', 'flow') or mode not in ((2, 3) if target == 'depth' else (1, 3)):
        raise ValueError('Requested guidance is not enabled')
    source_path, destination_path = Path(source).resolve(), Path(destination).resolve()
    if source_path == destination_path or (destination_path.exists() and os.path.samefile(source_path, destination_path)):
        raise ValueError('Cannot overwrite source media')
    video = frame is None and still is None
    if destination_path.suffix.lower() != ('.mp4' if video else '.png'):
        raise ValueError('Guidance export requires MP4 video or PNG image')
    session_factory = session_factory or GuidanceSession
    writer_factory = writer_factory or FFmpegVideoWriter
    capture = session = writer = None
    temporary = None

    def check_cancel():
        if cancel is not None and cancel.is_set():
            raise GuidanceExportCancelled()

    try:
        check_cancel()
        if still is None:
            capture = cv2.VideoCapture(str(source_path))
            if not capture.isOpened():
                raise RuntimeError('Cannot decode source video')
            total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = float(capture.get(cv2.CAP_PROP_FPS))
            if video and (not math.isfinite(fps) or fps <= 0):
                raise ValueError('Invalid source frame rate')
            first = 0 if video else max(int(frame) - 1, 0)
            if first:
                capture.set(cv2.CAP_PROP_POS_FRAMES, first)
            ok, image = capture.read()
            if not ok:
                raise RuntimeError('Cannot decode requested frame')
        else:
            image, fps, total, first = still, 1, 1, 0
        height, width = image.shape[:2]
        scale = min(1.0, analysis_edge(settings) / max(width, height))
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        worker_settings = dict(settings)
        worker_settings.pop('guidance_cache_pool', None)
        worker_settings['guidance_cache_mb'] = 0
        check_cancel()
        session = session_factory(worker_settings, *size)
        fd, temporary = tempfile.mkstemp(prefix='.guidance-export-', suffix=destination_path.suffix,
                                         dir=str(destination_path.parent))
        os.close(fd)
        if video:
            writer = writer_factory(temporary, width, height, fps, audio_source=None)
        count, index = 0, first
        while image is not None:
            check_cancel()
            rgba = cv2.cvtColor(cv2.resize(image, size, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGBA)
            motion, depth, _reset = session.process(rgba, reset=(index == first))
            check_cancel()
            if video or still is not None or index == int(frame):
                output = guidance_images(motion, depth, 2 if target == 'depth' else 1, settings)[target]
                output = cv2.resize(output, (width, height), interpolation=cv2.INTER_LINEAR)
                if video:
                    writer.write(output)
                else:
                    ok, encoded = cv2.imencode('.png', output)
                    if not ok:
                        raise RuntimeError('Cannot encode guidance PNG')
                    encoded.tofile(temporary)
                count += 1
                if progress:
                    progress(count, max(total, count) if video else 1)
                if not video:
                    break
            index += 1
            ok, image = capture.read() if capture is not None else (False, None)
            if not ok:
                if not count or (video and total > 0 and index < total):
                    raise RuntimeError('Source decoding stopped before export completed')
                break
        check_cancel()
        if writer is not None:
            writer.finish()
        check_cancel()
        os.replace(temporary, destination_path)
        temporary = None
        return count
    finally:
        if writer is not None:
            writer.abort()  # idempotent temp cleanup, never deletes destination
        if session is not None:
            session.close()
        if capture is not None:
            capture.release()
        if temporary and os.path.exists(temporary):
            os.remove(temporary)
