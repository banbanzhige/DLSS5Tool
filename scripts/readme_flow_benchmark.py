"""Reproducible README A/B: frozen-worker export speed and unencoded quality proxies.

Never changes saved settings, model weights, runtime DLLs, or backend defaults.
The speed pass runs alone; quality diagnostics are intentionally a separate pass.
"""
import argparse
import hashlib
import json
import platform
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def windows(count, length=16):
    if count < length * 3:
        raise ValueError('Need at least 48 frames for three disjoint windows')
    return [0, (count - length) // 2, count - length]


def common_mask(flows, border=24):
    """Identical interior/in-bounds pixels for both algorithms, no own-mask bias."""
    import numpy as np
    h, w = flows[0].shape[:2]
    yy, xx = np.mgrid[:h, :w].astype(np.float32)
    mask = (xx >= border) & (xx < w-border) & (yy >= border) & (yy < h-border)
    for flow in flows:
        mx, my = xx + flow[..., 0], yy + flow[..., 1]
        mask &= (mx >= 1) & (mx < w-2) & (my >= 1) & (my < h-2)
    if not mask.any():
        raise ValueError('Empty common evaluation region')
    return mask


def warp(image, flow):
    import cv2
    import numpy as np
    yy, xx = np.mgrid[:flow.shape[0], :flow.shape[1]].astype(np.float32)
    return cv2.remap(image.astype(np.float32), xx+flow[..., 0], yy+flow[..., 1], cv2.INTER_LINEAR)


def photo_mae(previous, current, flow, mask):
    import numpy as np
    return float(np.abs(current.astype(np.float32) - warp(previous, flow))[mask].mean())


def speed(args):
    from dlss5tool import dlss_engine, mod_paths
    args.output.mkdir(parents=True, exist_ok=False)
    gpu = subprocess.check_output(['nvidia-smi', '--query-gpu=name,driver_version,memory.total',
                                   '--format=csv,noheader'], text=True).strip()
    settings_path = ROOT / 'var/dlss5_settings.json'
    metadata = {'date': time.strftime('%Y-%m-%d %H:%M:%S %z'), 'platform': platform.platform(),
                'python': platform.python_version(), 'gpu': gpu, 'grid': args.grid,
                'edge': args.edge, 'repeats': args.repeats,
                'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'worker_sha256': sha(ROOT/'mods/enhancement/guidance_worker.exe'),
                'runtime_sha256': sha(mod_paths.runtime_path()),
                'host_sha256': sha(dlss_engine.HOST_DLL_V2),
                'weights_sha256': sha(ROOT/'mods/models/raft_large_C_T_SKHT_V2-ff5fadd5.pth'),
                'settings_sha256_before': sha(settings_path),
                'sources': [{'id': f'clip{i+1}', 'path': str(p.resolve()), 'sha256': sha(p)}
                            for i, p in enumerate(args.source)]}
    save(args.output/'environment.json', metadata)
    for repeat in range(args.repeats):
        order = ['raft', 'nvofa'] if repeat % 2 == 0 else ['nvofa', 'raft']
        for index, source in enumerate(args.source):
            directory = args.output / f'clip{index+1}-r{repeat+1}'
            command = [sys.executable, '-B', str(ROOT/'scripts/nvofa_integration_probe.py'),
                       '--mods', str(ROOT/'mods'), '--output', str(directory), '--source', str(source),
                       '--backends', *order, '--grid', str(args.grid), '--edge', str(args.edge),
                       '--flow-weights', str(ROOT/'mods/models/raft_large_C_T_SKHT_V2-ff5fadd5.pth')]
            print(f'Speed: clip{index+1} repeat {repeat+1}/{args.repeats} {order}', flush=True)
            result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                    errors='replace', timeout=300,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            (args.output/f'clip{index+1}-r{repeat+1}.log').write_text(
                result.stdout + result.stderr, encoding='utf-8')
            if result.returncode:
                raise RuntimeError(result.stderr[-4000:] or result.stdout[-4000:])
            report = json.loads((directory/'report.json').read_text(encoding='utf-8'))
            print([(r['backend'], round(r['wall_s'], 2), r['decoded_frames']) for r in report['runs']], flush=True)
    metadata['settings_sha256_after'] = sha(settings_path)
    save(args.output/'environment.json', metadata)


def quality(args):
    import cv2
    import numpy as np
    from dlss5tool import app_settings
    from dlss5tool.dlss_host_process import ProcessLive
    from dlss5tool.guidance_client import GuidanceSession
    from dlss5tool.guidance_visualization import guidance_images
    cv2.setNumThreads(4)
    env = json.loads((args.output/'environment.json').read_text(encoding='utf-8'))
    settings = {**app_settings.DEFAULTS, 'guidance_mode': 1, 'guidance_device': 'cuda',
                'guidance_flow_fallback': False, 'guidance_cache_mb': 0,
                'guidance_flow_edge': env['edge'], 'guidance_flow_grid': env['grid'],
                'guidance_flow_direction': 'backward', 'guidance_execution': 'serial',
                'host_backend': 'v2', 'host_auto_fallback': False,
                'mods_directory': str(ROOT/'mods'), 'frame_format': 'rgba8', 'color_profile': 'srgb'}
    for source in env['sources']:
        print(f"Quality: {source['id']}", flush=True)
        directory = args.output / (source['id']+'-quality')
        directory.mkdir(exist_ok=False)
        if sha(source['path']) != source['sha256']:
            raise RuntimeError('Source changed after speed pass')
        cap = cv2.VideoCapture(source['path'])
        if not cap.isOpened():
            raise RuntimeError('Cannot open source')
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        starts = windows(count)
        indices = [i for start in starts for i in range(start, start+16)]
        chosen, frames = set(indices), []
        index = 0
        while True:
            ok, bgr = cap.read()
            if not ok:
                break
            if index in chosen:
                frames.append(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA))
            index += 1
        cap.release()
        if index != count or len(frames) != 48:
            raise RuntimeError('Incomplete quality input')
        h, w = frames[0].shape[:2]
        outputs = {}
        for backend in ('zero', 'raft', 'nvofa'):
            selected = {**settings, 'guidance_mode': 0 if backend == 'zero' else 1,
                        'guidance_flow_backend': 'raft' if backend == 'zero' else backend}
            array = np.lib.format.open_memmap(directory/f'{backend}-outputs.npy', mode='w+',
                                             dtype=np.uint8, shape=(48, h, w, 4))
            live = ProcessLive(w, h, selected)
            try:
                for i, frame in enumerate(frames):
                    out = live.process(frame, reset=i % 16 == 0)
                    if out is None:
                        raise RuntimeError('Missing unencoded DLSS output')
                    if backend != 'zero' and live.guidance_info.get('flow_backend') != backend:
                        raise RuntimeError('Backend fallback in quality pass')
                    array[i] = out
                    if i in (10, 26, 42):
                        cv2.imwrite(str(directory/f'{backend}-{indices[i]}.png'), cv2.cvtColor(out, cv2.COLOR_RGBA2BGRA))
            finally:
                live.close()
            array.flush()
            outputs[backend] = array
            print(f"  {backend}: 48 unencoded outputs", flush=True)
        sessions = {}
        rows = []
        try:
            for backend in ('raft', 'nvofa'):
                sessions[backend] = GuidanceSession({**settings, 'guidance_flow_backend': backend}, w, h)
                if sessions[backend].info['flow_backend'] != backend:
                    raise RuntimeError('Backend mismatch in raw flow diagnostics')
            for i, frame in enumerate(frames):
                flows, resets = {}, []
                for backend, session in sessions.items():
                    flow, depth, reset = session.process(frame, reset=i % 16 == 0)
                    flows[backend] = flow
                    resets.append(reset)
                    if i in (10, 26, 42):
                        vis = guidance_images(flow, depth, 1, {'guidance_flow_range': 5})['flow']
                        cv2.imwrite(str(directory/f'{backend}-flow-{indices[i]}.png'), vis)
                if i % 16 < 3 or any(resets):
                    continue
                mask = common_mask(list(flows.values()))
                previous, current = frames[i-1][..., :3], frame[..., :3]
                row = {'frame': indices[i], 'common_mask_fraction': float(mask.mean()),
                       'photo_mae': {b: photo_mae(previous, current, f, mask) for b, f in flows.items()},
                       'temporal_residual': {}}
                # A reference warp is fixed across ALL output variants. Both references
                # are reported; neither inferred flow is ground truth.
                for reference, flow in flows.items():
                    row['temporal_residual'][reference] = {}
                    for backend, array in outputs.items():
                        residual = array[i, ..., :3].astype(np.float32) - current
                        prev_residual = array[i-1, ..., :3].astype(np.float32) - previous
                        row['temporal_residual'][reference][backend] = photo_mae(prev_residual, residual, flow, mask)
                rows.append(row)
        finally:
            for session in sessions.values():
                session.close()
        if not rows:
            raise RuntimeError('No valid quality pairs')
        result = {'source_sha256': source['sha256'], 'width': w, 'height': h,
                  'window_starts': starts, 'indices': indices, 'measured_pairs': len(rows),
                  'settings': settings, 'frames': rows,
                  'common_mask_fraction': float(np.mean([r['common_mask_fraction'] for r in rows])),
                  'photo_mae': {b: float(np.mean([r['photo_mae'][b] for r in rows])) for b in flows},
                  'temporal_residual': {ref: {b: float(np.mean([r['temporal_residual'][ref][b] for r in rows]))
                                             for b in outputs} for ref in flows}}
        save(directory/'report.json', result)
        print({k: result[k] for k in ('measured_pairs', 'photo_mae', 'temporal_residual')}, flush=True)


def summarize(args):
    import numpy as np
    env = json.loads((args.output/'environment.json').read_text(encoding='utf-8'))
    # Publishable data excludes private media paths and ignored output/ links.
    result = {'environment': {k: v for k, v in env.items() if k != 'sources'}, 'clips': []}
    for source in env['sources']:
        reports = [json.loads((args.output/f"{source['id']}-r{i+1}/report.json").read_text(encoding='utf-8'))
                   for i in range(env['repeats'])]
        clip = {'id': source['id'], 'source_sha256': source['sha256'], 'speed': {}}
        for backend in ('raft', 'nvofa'):
            runs = [next(r for r in report['runs'] if r['backend'] == backend) for report in reports]
            clip.update({k: runs[0][k] for k in ('width', 'height', 'frames', 'source_fps')})
            walls = [r['wall_s'] for r in runs]
            # Exclude the first eight frames ONLY from steady-stage timing, not wall.
            means = [float(np.mean([m['flow_ms'] for m in r['guidance_metrics'][8:]
                                    if m.get('flow_model_calls') == 1])) for r in runs]
            clip['speed'][backend] = {
                'wall_s_runs': walls, 'wall_s_median': float(np.median(walls)),
                'wall_fps': runs[0]['frames']/float(np.median(walls)),
                'flow_ms_run_means': means, 'flow_ms_median': float(np.median(means)),
                'process_p95_ms_median': float(np.median([r['process_p95_ms'] for r in runs])),
                'analysis_size': runs[0]['guidance_metrics'][-1]['flow_size'],
                'ready': runs[0]['ready'],
                'all_encoded_frames_verified': all(r['decoded_frames'] == r['frames'] for r in runs)}
        clip['wall_reduction_percent'] = 100*(1-clip['speed']['nvofa']['wall_s_median']/clip['speed']['raft']['wall_s_median'])
        path = args.output/(source['id']+'-quality')/'report.json'
        if path.exists():
            q = json.loads(path.read_text(encoding='utf-8'))
            clip['quality'] = {k: q[k] for k in ('window_starts', 'indices', 'measured_pairs',
                                               'common_mask_fraction', 'photo_mae', 'temporal_residual')}
        result['clips'].append(clip)
    save(args.output/'summary.json', result)
    if args.publish:
        if args.publish.exists():
            raise FileExistsError('Publish target exists; choose a new evidence file')
        save(args.publish, result)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('speed', 'quality', 'summary'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source', type=Path, nargs='+')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--grid', type=int, choices=(1, 2, 4), default=1)
    parser.add_argument('--edge', type=int, default=512)
    parser.add_argument('--publish', type=Path, help='Optional new, path-sanitized JSON evidence file (summary only)')
    args = parser.parse_args()
    if args.mode == 'speed' and (not args.source or args.repeats < 1):
        parser.error('speed requires --source and positive --repeats')
    {'speed': speed, 'quality': quality, 'summary': summarize}[args.mode](args)


if __name__ == '__main__':
    from multiprocessing import freeze_support
    freeze_support()
    main()
