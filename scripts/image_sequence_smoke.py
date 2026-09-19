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
from dlss5tool.video_export import find_ffmpeg, find_ffprobe, probe_video_stream, FFmpegHDRVideoReader


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--work-dir', required=True)
    parser.add_argument('--hdr', choices=('pq', 'hlg'), help='16-bit BT.2020 RGB PNG instead of SDR')
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
        if args.hdr:
            ramp = np.linspace(1000, 60000, 320, dtype=np.uint16)[None, :].repeat(180, axis=0)
            image = np.stack((ramp, ramp // 2 + 8192, ramp // 3 + 12288), axis=-1)
            cv2.rectangle(image, (40 + i * 3, 45), (90 + i * 3, 100), (12000, 24000, 36000), -1)
        else:
            image = np.full((180, 320, 3), 40, np.uint8)
            cv2.rectangle(image, (40 + i * 3, 45), (90 + i * 3, 100), (80, 180, 240), -1)
        ok, encoded = cv2.imencode('.png', image)
        if not ok:
            raise RuntimeError('fixture encoding failed')
        encoded.tofile(work / f'frame_{i:04}.png')
    sequence = ImageSequence.scan(work / 'frame_0000.png', 24,
        color_profile=('hdr10_pq' if args.hdr == 'pq' else 'hdr10_hlg') if args.hdr else 'srgb')
    manifest = sequence.save(work / 'records')
    results = []
    for scale, multiplier in ((1, 1), (2, 1), (1, 2), (2, 2)):
        name = f'sr{scale}-fg{multiplier}'
        config = dict(super_resolution_scale=scale, frame_generation_multiplier=multiplier,
            output_view=0, output_mix=1, guidance_mode=0, style=0, preset=0,
            intensity=0, local_tone=0, local_struct=0, skin_struct=0, use_auto_mask=0,
            hdr_mode=bool(args.hdr), host_backend='v2', host_auto_fallback=False)
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
            color_evidence = {}
            if args.hdr:
                assert video['codec_name'] == 'hevc' and video['profile'] == 'Main 10'
                assert video['pix_fmt'] == 'yuv420p10le' and video['color_range'] == 'tv'
                assert video['color_transfer'] == sequence.metadata['color_transfer']
                assert video['color_primaries'] == 'bt2020' and video['color_space'] == 'bt2020nc'
                decoder = FFmpegHDRVideoReader(output, video['width'], video['height'],
                    probe_video_stream(find_ffmpeg(), output))
                errors = []
                distinct = []
                try:
                    for index in range(6 * multiplier):
                        rendered = session.wait(index, threading.Event())[0]
                        decoded = decoder.read()
                        assert decoded is not None and rendered.dtype == decoded.dtype == np.float16
                        assert np.isfinite(rendered).all() and np.isfinite(decoded).all()
                        errors.append(float(np.mean(np.abs(decoded[..., :3].astype(np.float32) -
                            np.clip(rendered[..., :3].astype(np.float32), 0, 1)))))
                        distinct.append(len(np.unique(decoded[..., :3])))
                    assert decoder.read() is None
                finally:
                    decoder.close()
                assert min(distinct) > 256, distinct
                # Signal-code error against actual cached render frames, not an
                # assertion that neural enhancement equals the unprocessed source.
                assert max(errors) < .01, errors
                color_evidence = dict(profile=video['profile'], pixel_format=video['pix_fmt'],
                    transfer=video['color_transfer'], primaries=video['color_primaries'],
                    matrix=video['color_space'], range=video['color_range'],
                    max_frame_code_mae=max(errors), min_decoded_distinct_values=min(distinct))
            results.append(dict(case=name, status='passed', width=video['width'],
                height=video['height'], frames=video['nb_read_frames'], rate=video['r_frame_rate'],
                duration=video['duration'], audio_streams=0, generated_frames=result['generated_frames'],
                **color_evidence))
            print(json.dumps(results[-1]), flush=True)
        finally:
            manager.close()
    (work / 'result.json').write_text(json.dumps(results, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
