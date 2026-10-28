"""Verify real frozen rendering workers plus production PTS/audio muxing.

The parent runs from the development interpreter after verify_frozen_release
has matched the corresponding frozen modules to source. Rendering, RAFT and
video encoding execute inside the packaged EXE with a restricted child PATH.
"""
import argparse
from fractions import Fraction
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import wave

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dlss5tool.app_version import APP_VERSION
from dlss5tool import app_settings
from dlss5tool.video_export import mux_source_audio
from dlss5tool.video_timeline import inspect_video_timeline
from dlss5tool.vfr_mux import retime_encoded_video
from scripts.probe_av_sync import make_video, snapshot, run


def safe(path):
    path = path.absolute()
    for parent in (path, *path.parents):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError(f'Reparse point: {parent}')
    if path.resolve() != path:
        raise ValueError(f'Redirected path: {path}')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packages', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--allow-external-output', action='store_true')
    args = parser.parse_args()
    packages, work = safe(args.packages), safe(args.output)
    if work.exists() or (not args.allow_external_output and not work.is_relative_to(ROOT / 'tmp')):
        raise ValueError('Use a new task directory, or explicitly allow external output')
    full = safe(packages / f'DLSS5Tool-{APP_VERSION}-win64-full')
    executable = safe(full / 'DLSS5Tool.exe')
    ffmpeg = safe(full / '_internal/ffmpeg.exe')
    work.mkdir(parents=True)
    wav = work / 'pulse.wav'
    samples = np.zeros(4 * 48000, np.float64)
    samples[96000:100800] = np.sin(2 * np.pi * 880 * np.arange(4800) / 48000) * .5
    with wave.open(str(wav), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes((samples * 32767).astype('<i2').tobytes())
    source = {}
    for name in ('vfr', 'offset'):
        video = work / f'{name}-video.mp4'
        stamps = ([str(Fraction(i, 15)) for i in range(30)]
                  + [str(2 + Fraction(i, 45)) for i in range(90)]) if name == 'vfr' else None
        make_video(video, stamps)
        source[name] = work / f'{name}-source.mp4'
        run(ffmpeg, '-v', 'error', '-nostdin', '-n', '-copyts',
            '-itsoffset', '1' if name == 'offset' else '0', '-i', video,
            '-i', wav, '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac', source[name])
    child_env = dict(os.environ, PATH=str(Path(os.environ['SystemRoot']) / 'System32'))
    child_env.pop('PYTHONPATH', None)
    child_env.pop('PYTHONHOME', None)
    records = []
    for label, name, mode in (('vfr-flow-off', 'vfr', 0), ('vfr-flow-on', 'vfr', 1),
                              ('offset-flow-on', 'offset', 1)):
        print('FROZEN AV SYNC', label, flush=True)
        encoded = work / f'{label}-encoded.mp4'
        result_path = work / f'{label}-worker.json'
        settings = {**app_settings.DEFAULTS, 'guidance_mode': mode, 'guidance_cache_mb': 0,
                    'mods_directory': str(full / 'mods'), 'dlss_runtime': '__bundled__',
                    'output_mix': 1, 'output_view': 0}
        result = subprocess.run([str(executable), '--parallel-worker', '--input', str(source[name]),
            '--output', str(encoded), '--start', '0', '--end', '120', '--warmup', '0', '--worker-id', '0',
            '--nvenc', '0', '--fps', '30', '--disable-b-frames', '--settings', json.dumps(settings),
            '--progress', str(work / f'{label}-progress.json'), '--result', str(result_path)],
            cwd=work, env=child_env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=180)
        payload = json.loads(result_path.read_text(encoding='utf-8'))
        if result.returncode or not payload.get('ok') or payload['kept_frames'] != 120:
            raise RuntimeError(payload)
        timing = inspect_video_timeline(source[name], allow_cpu_fallback=True)
        retimed = work / f'{label}-retimed.mp4'
        retime_encoded_video(encoded, retimed, timing.timestamps, timing.end)
        output = work / f'{label}-final.mp4'
        audio_mode = mux_source_audio(str(ffmpeg), str(retimed), str(source[name]), str(output),
                                      source_video_start=str(timing.start))
        before, after = snapshot(source[name]), snapshot(output)
        assert after['frames'] == before['frames'] == 120
        for a, b in zip(before['pts'], after['pts']):
            assert abs((Fraction(a) - Fraction(before['video_start']))
                       - (Fraction(b) - Fraction(after['video_start']))) <= Fraction(1, 1_000_000)
        assert before['audio_payload_sha256'] == after['audio_payload_sha256']
        shift = ((Fraction(after['video_start']) - Fraction(after['audio_first_pts']))
                 - (Fraction(before['video_start']) - Fraction(before['audio_first_pts'])))
        assert abs(shift) <= Fraction(1, 1_000_000)
        records.append(dict(case=label, passed=True, guidance_mode=mode,
            worker=payload, frames=120, audio_mode=audio_mode, relative_av_shift_seconds=float(shift),
            normalized_video_pts_exact=True, audio_payload_exact=True))
        print('PASS', label, flush=True)
    report = dict(version=APP_VERSION, passed=True, cases=records,
        scope='Frozen rendering/RAFT/encode worker + source-verified production parent retiming/audio mux; no GUI automation')
    (work / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
