"""Tiny real-GPU sequence smoke test. Reuses installed components; never builds.

Run with --work-dir tmp/<registered-task>/smoke. Keep outputs until inspected.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dlss5tool.image_sequence import ImageSequence
from dlss5tool.render_cache import RenderCache, encode_cached
from dlss5tool.video_export import find_ffmpeg, find_ffprobe


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--work-dir', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    work = Path(args.work_dir).resolve()
    if not work.is_relative_to(root / 'tmp') or work.exists():
        raise ValueError('Use a NEW directory under the registered task in tmp/')
    if shutil.disk_usage(root).free < 15 * 1024**3 + 50 * 1024**2:
        raise ValueError('Insufficient free space')
    probe = find_ffprobe(find_ffmpeg())
    if not probe:
        raise RuntimeError('ffprobe required for verification')
    work.mkdir(parents=True)
    os.environ['DLSS5TOOL_FG_LOG_ROOT'] = str(work / 'logs')
    # Moving high-contrast object, not identical duplicate images.
    for i in range(6):
        image = np.full((180, 320, 3), 40, np.uint8)
        cv2.rectangle(image, (40 + i * 3, 45), (90 + i * 3, 100), (80, 180, 240), -1)
        ok, encoded = cv2.imencode('.png', image)
        if not ok:
            raise RuntimeError('fixture encoding failed')
        encoded.tofile(work / f'frame_{i:04}.png')
    sequence = ImageSequence.scan(work / 'frame_0000.png', 24)
    manifest = sequence.save(work / 'records')
    results = []
    for scale, multiplier in ((1, 1), (2, 1), (1, 2), (2, 2)):
        name = f'sr{scale}-fg{multiplier}'
        config = dict(super_resolution_scale=scale, frame_generation_multiplier=multiplier,
            output_view=0, output_mix=1, guidance_mode=0, style=0, preset=0,
            intensity=0, local_tone=0, local_struct=0, skin_struct=0, use_auto_mask=0,
            hdr_mode=False, host_backend='v2', host_auto_fallback=False)
        manager = RenderCache(64 * 1024**2)
        try:
            session = manager.session(manifest, config)
            output = work / (name + '.mp4')
            result = encode_cached(session, output, config, threading.Event(), lambda *a: None)
            process = subprocess.run([probe, '-v', 'error', '-count_frames', '-show_streams',
                '-of', 'json', str(output)], capture_output=True, text=True, check=True,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            streams = json.loads(process.stdout)['streams']
            assert len(streams) == 1 and streams[0]['codec_type'] == 'video'
            video = streams[0]
            assert int(video['nb_read_frames']) == 6 * multiplier
            assert video['width'] == 320 * scale and video['height'] == 180 * scale
            assert abs(float(video['duration']) - .25) < .002
            assert video['r_frame_rate'] == f'{24 * multiplier}/1'
            results.append(dict(case=name, status='passed', width=video['width'],
                height=video['height'], frames=video['nb_read_frames'], rate=video['r_frame_rate'],
                duration=video['duration'], audio_streams=0, generated_frames=result['generated_frames']))
            print(json.dumps(results[-1]), flush=True)
        finally:
            manager.close()
    (work / 'result.json').write_text(json.dumps(results, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
