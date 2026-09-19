"""Bounded CFR packet timing gate; synthetic YUV, not full FG/audio acceptance."""
import argparse
import ctypes
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dlss5tool.nvenc_yuv import NativeYuvEncoder, YuvEncoderConfig, ReadyYuvFrame
from scripts.encode_acceptance_nvenc_contract import framemd5_bytes, framemd5_file, sha256_file


def run(command, payload=None):
    result = subprocess.run(command, input=payload, capture_output=True, timeout=45,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace')[-2000:])
    return result.stdout


def packet_row(packet):
    return dict(pts=packet.pts, dts=packet.dts, duration=packet.duration,
                keyframe=packet.is_keyframe, picture_type=packet.picture_type,
                bytes=len(packet.data), sha256=hashlib.sha256(packet.data).hexdigest())


def probe_packets(probe, path, time_base):
    document = json.loads(run([probe, '-v', 'error', '-select_streams', 'v:0',
        '-show_packets', '-show_streams', '-show_entries',
        'packet=pts,dts,duration,flags:stream=time_base,codec_name,profile,pix_fmt,color_transfer,color_primaries,color_space',
        '-of', 'json', str(path)]))
    stream = document['streams'][0]
    scale = Fraction(stream['time_base']) / time_base
    rows = []
    for packet in document['packets']:
        row = {name: str(Fraction(packet[name]) * scale) for name in ('pts', 'dts', 'duration')}
        row['keyframe'] = 'K' in packet['flags']
        rows.append(row)
    return dict(stream=stream, packets=rows)


def make_frames(np, config, count):
    """Legal-range moving gradients, spatial and temporal chroma variation."""
    w, h = config.width, config.height
    yy, xx = np.indices((h, w), dtype=np.uint32)
    cy, cx = np.indices((h // 2, w // 2), dtype=np.uint32)
    frames = []
    for index in range(count):
        y = 16 + (xx + yy * 2 + index * 7) % 220
        u = 16 + (cx * 3 + cy + index * 5) % 225
        v = 16 + (cx + cy * 3 + index * 9) % 225
        if config.codec:
            uv = np.stack((u, v), axis=-1).reshape(-1)
            samples = np.concatenate((y.reshape(-1), uv)) * 4
            frame = (samples << 6).astype('<u2').view(np.uint8)
        else:
            frame = np.concatenate((y.reshape(-1), u.reshape(-1), v.reshape(-1))).astype(np.uint8)
        frames.append(frame)
    return frames


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--native-root', type=Path, required=True)
    parser.add_argument('--label', default='packet-timing')
    args = parser.parse_args()
    work, native = args.work.resolve(), args.native_root.resolve()
    report_path = work / (args.label + '.json')
    if not args.label.replace('-', '').isalnum() or not (work / 'TASK.md').is_file() or report_path.exists():
        parser.error('Fresh registered work directory required')
    os.environ['TEMP'] = os.environ['TMP'] = str(work)
    import numpy as np
    import torch
    from dlss5tool.video_export import find_ffmpeg, find_ffprobe, build_video_encoder_args
    ffmpeg = find_ffmpeg()
    probe = find_ffprobe(ffmpeg)
    dll_path = native / 'native-encoder/ring.dll'
    dll = ctypes.WinDLL(str(dll_path))
    owned = dll.ring_owned_buffers
    owned.argtypes = []; owned.restype = ctypes.c_int
    sources = ['scripts/gpu_nvenc_ring.cpp', 'dlss5tool/nvenc_yuv.py',
               'scripts/encoder_packet_gate.py', 'tests/test_nvenc_yuv.py']
    report = dict(scope=__doc__, argv=sys.argv, native_sha256=sha256_file(dll_path),
        source_sha256={name: sha256_file(ROOT / name) for name in sources},
        ffmpeg_version=run([ffmpeg, '-version']).decode(errors='replace').splitlines()[0],
        cases=[], empty=[])
    try:
        torch.empty(0, device='cuda')
        for codec, profile in enumerate(('sdr', 'pq', 'hlg')):
            with NativeYuvEncoder(dll_path, YuvEncoderConfig(160, 96, 24, 1, codec)) as session:
                empty = session.finish()
                if empty or session.finish():
                    raise RuntimeError('Empty/repeated EOS emitted data')
            report['empty'].append(dict(profile=profile, packets=0, owned=owned()))
            if owned():
                raise RuntimeError('Empty session leaked resources')
            for count, num, den in ((1,24,1), (2,24,1), (3,24,1), (17,30000,1001),
                                    (32,60,1), (48,72,1), (48,96,1)):
                config = YuvEncoderConfig(160, 96, num, den, codec)
                label = f'{args.label}-{profile}-{count}-{num}-{den}'
                destination = work / (label + '.mp4')
                if destination.exists():
                    raise FileExistsError(destination)
                frames = make_frames(np, config, count)
                raw = b''.join(frame.tobytes() for frame in frames)
                colors = [] if not codec else ['-color_range', 'tv', '-color_primaries', 'bt2020',
                    '-color_trc', 'smpte2084' if codec == 1 else 'arib-std-b67', '-colorspace', 'bt2020nc']
                reference_command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                    '-f', 'rawvideo', '-pixel_format', config.layout, '-video_size', '160x96',
                    '-framerate', f'{num}/{den}', *colors, '-i', 'pipe:0', '-an',
                    *build_video_encoder_args(bool(codec), True), *colors, str(destination)]
                run(reference_command, raw)
                expected = probe_packets(probe, destination, config.time_base)
                packets = []
                with NativeYuvEncoder(dll_path, config) as session:
                    headers = session.headers
                    for index, frame in enumerate(frames):
                        gpu = torch.from_numpy(frame).cuda()
                        torch.cuda.synchronize()
                        packets.extend(session.write(ReadyYuvFrame(gpu.data_ptr(),gpu.numel(),config.layout,index)))
                    packets.extend(session.finish())
                    if session.finish():
                        raise RuntimeError('Repeated EOS emitted duplicate data')
                encoded = headers + b''.join(packet.data for packet in packets)
                actual_decode = framemd5_bytes(ffmpeg, encoded, 'hevc' if codec else 'h264')
                expected_decode = framemd5_file(ffmpeg, destination)
                timing = [dict(pts=str(p.pts),dts=str(p.dts),duration=str(p.duration),keyframe=p.is_keyframe)
                          for p in packets]
                row = dict(label=label, frames=count, fps=f'{num}/{den}', time_base=str(config.time_base),
                    reference_command=reference_command, reference=expected,
                    packets=[packet_row(p) for p in packets], input_sha256=hashlib.sha256(raw).hexdigest(),
                    native_bitstream_sha256=hashlib.sha256(encoded).hexdigest(),
                    reference_sha256=sha256_file(destination), actual_decode=actual_decode,
                    expected_decode=expected_decode, owned_after_close=owned(),
                    pts_permutation=sorted(p.pts for p in packets)==list(range(count)),
                    timing_equal=timing==expected['packets'],
                    decode_equal=actual_decode['returncode']==expected_decode['returncode']==0
                        and actual_decode['frames']==expected_decode['frames']==count
                        and actual_decode['md5']==expected_decode['md5'])
                row['gate'] = row['timing_equal'] and row['decode_equal'] and row['pts_permutation'] and owned()==0
                report['cases'].append(row)
                print(json.dumps(dict(label=label,gate=row['gate'],timing=row['timing_equal'],decode=row['decode_equal'])),flush=True)
        report['status'] = 'passed' if all(row['gate'] for row in report['cases']) else 'failed'
    except BaseException as exc:
        report.update(status='failed', error=repr(exc), traceback=traceback.format_exc())
        traceback.print_exc()
    finally:
        with report_path.open('x', encoding='utf-8') as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
        print(f'Report: {report_path}', flush=True)
    return 0 if report['status']=='passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
