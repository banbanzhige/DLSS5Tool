"""Isolated real-footage optical-flow/DLSS comparison; no production edits.

Real-video metrics are proxies, not ground-truth flow accuracy. Each model runs
in a fresh process. Backward and forward passes are separate; reported backward
time excludes the diagnostic forward pass, DLSS, disk IO and model loading.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
VARIANTS = {
    'zero': (720, 0), 'raft720_6': (720, 6), 'raft720_12': (720, 12),
    'raft1024_6': (1024, 6), 'raft1024_12': (1024, 12),
    'sea720_4': (720, 4), 'sea1024_4': (1024, 4),
    'raft720_6_repeat': (720, 6),
}


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def prepare(args):
    import cv2
    import numpy as np
    args.output.mkdir(parents=True, exist_ok=False)
    cap = cv2.VideoCapture(str(args.source))
    count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    starts = [0, 64, count - 16]
    assert count >= 128
    frames, ids = [], []
    for start in starts:
        cap.set(cv2.CAP_PROP_POS_FRAMES, start)
        for index in range(start, start + 16):
            ok, bgr = cap.read()
            if not ok:
                raise ValueError(f'Cannot decode frame {index}')
            frames.append(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA))
            ids.append(index)
    cap.release()
    frames = np.stack(frames)
    np.save(args.output / 'inputs.npy', frames)
    settings = json.loads((ROOT / 'dlss5_settings.json').read_text(encoding='utf-8'))
    save(args.output / 'input.json', {'source': str(args.source), 'source_frames': count,
        'fps': fps, 'indices': ids, 'starts': starts, 'span': 16,
        'warmup_frames_per_window': 3, 'shape': list(frames.shape),
        'input_sha256': hashlib.sha256(memoryview(frames)).hexdigest(),
        'saved_settings_snapshot': settings})


def model_for(name, updates):
    import torch
    if name.startswith('sea'):
        sys.path.insert(0, str(ROOT / 'tmp/flow-quality-deps'))
        sys.path.insert(0, str(ROOT / 'tmp/flow-quality-sea-raft/core'))
        from raft import RAFT
        from extractor import ResNetFPN
        from safetensors.torch import load_file
        cfg = json.loads((ROOT / 'tmp/flow-quality-sea-raft/config/eval/spring-M.json').read_text())
        cfg['iters'] = updates
        # Skip unnecessary ImageNet download: every parameter AND running-stat
        # buffer is immediately supplied by the full strict checkpoint load.
        initializer = ResNetFPN._init_weights
        try:
            ResNetFPN._init_weights = lambda self, args: None
            model = RAFT(SimpleNamespace(**cfg))
        finally:
            ResNetFPN._init_weights = initializer
        state = load_file(str(ROOT / 'tmp/flow-quality-sea-raft/model.safetensors'))
        # safetensors removes shared-storage aliases: BasicBlock registers the
        # SAME bn3 module both as bn3 and downsample.1. Restore only verified
        # aliases, not missing learned weights or arbitrary default buffers.
        for key in model.state_dict():
            if key not in state and '.downsample.1.' in key:
                alias = key.replace('.downsample.1.', '.bn3.')
                prefix = key.split('.downsample.1.')[0]
                block = model.get_submodule(prefix)
                assert block.downsample[1] is block.bn3
                state[key] = state[alias]
        model.load_state_dict(state, strict=True)
        model.eval().cuda()
        return model, lambda a, b: model(a, b, iters=updates, test_mode=True)['flow'][-1]
    from torchvision.models.optical_flow import raft_large, Raft_Large_Weights
    model = raft_large(weights=None)
    model.load_state_dict(torch.load(ROOT / 'mods/models/raft_large_C_T_SKHT_V2-ff5fadd5.pth',
                                    map_location='cpu', weights_only=True), strict=True)
    model.eval().cuda()
    transform = Raft_Large_Weights.DEFAULT.transforms()
    def call(a, b):
        a, b = transform(a / 255., b / 255.)
        return model(a, b, num_flow_updates=updates)[-1]
    return model, call


def resize_flow(flow, w, h):
    import cv2
    result = cv2.resize(flow, (w, h))
    result[..., 0] *= w / flow.shape[1]
    result[..., 1] *= h / flow.shape[0]
    return result


def run(args):
    import cv2
    import numpy as np
    import torch
    import torchvision
    import dlss_engine
    from guidance_parameters import analysis_size
    from guidance_visualization import guidance_images
    cv2.setNumThreads(4)
    torch.set_num_threads(4)
    torch.manual_seed(0)
    name = args.variant
    edge, updates = VARIANTS[name]
    directory = args.output / name
    directory.mkdir(exist_ok=False)
    inputs = np.load(args.output / 'inputs.npy', mmap_mode='r')
    metadata = json.loads((args.output / 'input.json').read_text(encoding='utf-8'))
    n, h, w, _ = inputs.shape
    fw, fh = analysis_size(w, h, edge)
    model, call = (None, None) if name == 'zero' else model_for(name, updates)
    records = []
    flows = np.lib.format.open_memmap(directory / 'flows.npy', mode='w+', dtype=np.float32,
                                    shape=(n, fh, fw, 2))
    masks = np.lib.format.open_memmap(directory / 'masks.npy', mode='w+', dtype=np.uint8,
                                    shape=(n, h, w))
    outputs = np.lib.format.open_memmap(directory / 'outputs.npy', mode='w+', dtype=np.uint8,
                                      shape=inputs.shape)
    snapshot = metadata['saved_settings_snapshot']
    settings = {**snapshot, 'guidance_mode': 1, 'host_backend': 'v2', 'host_auto_fallback': False,
        'host_in_flight': 1, 'host_zero_fast_path': False, 'host_submission': 'compatibility',
        'frame_format': 'rgba8', 'color_profile': 'srgb', 'motion_scale_x': 1., 'motion_scale_y': 1.}
    dlss_engine.LOG_PATH = str(directory / 'ngx.log')
    class Guidance:
        info = {}
        last_metrics = {}
        mv = np.zeros((h, w, 2), np.float32)
        dp = np.zeros((h, w), np.float32)
        def process(self, rgba, reset, **kwargs):
            return self.mv, self.dp, reset
        def close(self):
            pass
    guide = Guidance()
    live = dlss_engine.Live(w, h, settings)
    live._guidance = guide
    yy, xx = np.mgrid[:h, :w].astype(np.float32)
    fixed = (xx >= 24) & (xx < w-24) & (yy >= 24) & (yy < h-24)
    prev = None
    torch.cuda.reset_peak_memory_stats()
    with torch.inference_mode():
        for index, rgba in enumerate(inputs):
            reset = index % 16 == 0
            start = time.perf_counter()
            rgb = np.asarray(rgba[..., :3])
            small = cv2.resize(rgb, (fw, fh))
            tensor = torch.from_numpy(np.ascontiguousarray(small)).permute(2,0,1)[None].float().cuda()
            if reset or model is None:
                backward = np.zeros((fh, fw, 2), np.float32)
            else:
                backward = call(tensor, prev)[0].permute(1,2,0).cpu().numpy()
            guide.mv = resize_flow(backward, w, h)
            torch.cuda.synchronize()
            backward_ms = (time.perf_counter() - start) * 1000
            assert np.isfinite(backward).all()
            flows[index] = backward
            rec = {'frame': metadata['indices'][index], 'offset': index % 16,
                   'backward_ms': backward_ms}
            if not reset:
                forward = (np.zeros_like(backward) if model is None else
                           call(prev, tensor)[0].permute(1,2,0).cpu().numpy())
                forward = resize_flow(forward, w, h)
                mx, my = xx + guide.mv[...,0], yy + guide.mv[...,1]
                valid = fixed & (mx >= 1) & (mx < w-2) & (my >= 1) & (my < h-2)
                warped = cv2.remap(np.float32(inputs[index-1,...,:3]), mx, my, cv2.INTER_LINEAR)
                photo = np.abs(np.float32(rgb) - warped).mean(axis=2)
                reverse = cv2.remap(forward, mx, my, cv2.INTER_LINEAR)
                fb_sq = np.sum((guide.mv + reverse)**2, axis=2)
                reliable = valid & (fb_sq < .01*(np.sum(guide.mv**2,axis=2)+np.sum(reverse**2,axis=2))+.5)
                masks[index] = reliable
                rec.update(photo_mae=float(photo[valid].mean()),
                           photo_consistent_mae=float(photo[reliable].mean()),
                           fb_mean_px=float(np.sqrt(fb_sq[valid]).mean()),
                           consistent_fraction=float(reliable.sum()/valid.sum()),
                           motion_mean_px=float(np.linalg.norm(guide.mv,axis=2)[valid].mean()))
            else:
                masks[index] = 0
            start = time.perf_counter()
            out = live.process(np.array(rgba), reset=reset)
            if out is None:
                raise RuntimeError('DLSS process returned no output')
            rec['dlss_ms'] = (time.perf_counter()-start)*1000
            rec['enhancement_mae'] = float(np.abs(out[...,:3].astype(np.int16)-rgb).mean())
            outputs[index] = out
            if metadata['indices'][index] in (10, 74, 159):
                cv2.imwrite(str(directory / f"output-{metadata['indices'][index]}.png"), cv2.cvtColor(out, cv2.COLOR_RGBA2BGRA))
                vis = guidance_images(guide.mv, guide.dp, 1, {'guidance_flow_range': 8})['flow']
                cv2.imwrite(str(directory / f"flow-{metadata['indices'][index]}.png"), vis)
                if not reset:
                    cv2.imwrite(str(directory / f"warp-error-{metadata['indices'][index]}.png"), np.uint8(np.clip(photo*16,0,255)))
            records.append(rec)
            prev = tensor
            if index % 16 == 15:
                print(name, f'{index+1}/{n}', flush=True)
    flows.flush(); masks.flush(); outputs.flush()
    save(directory / 'result.json', {'variant': name, 'edge': edge, 'updates': updates,
        'torch': torch.__version__, 'torchvision': torchvision.__version__,
        'gpu': torch.cuda.get_device_name(), 'precision': 'FP32',
        'peak_torch_mib': torch.cuda.max_memory_allocated()/1048576,
        'settings': settings, 'frames': records})
    # Known NGX shutdown hang: release guidance and let this isolated process
    # own runtime cleanup, matching the existing repository probe strategy.
    live.close_guidance()


def summarize(args):
    import cv2
    import numpy as np
    meta = json.loads((args.output/'input.json').read_text(encoding='utf-8'))
    inputs = np.load(args.output/'inputs.npy', mmap_mode='r')
    names = [v for v in VARIANTS if (args.output/v/'result.json').exists()]
    h,w = inputs.shape[1:3]
    yy,xx = np.mgrid[:h,:w].astype(np.float32)
    reports = {}
    selected = [i for i in range(len(inputs)) if i%16 >= 3]
    for name in names:
        rows = json.loads((args.output/name/'result.json').read_text(encoding='utf-8'))['frames']
        chosen = [rows[i] for i in selected]
        reports[name] = {key:float(np.mean([r[key] for r in chosen])) for key in
            ('backward_ms','dlss_ms','photo_mae','photo_consistent_mae','fb_mean_px',
             'consistent_fraction','motion_mean_px','enhancement_mae')}
        reports[name]['backward_p95_ms'] = float(np.percentile([r['backward_ms'] for r in chosen],95))
    # Two FIXED reference warps, each applied to ALL output variants; never
    # evaluate each model's temporal output using only its own inferred flow.
    for reference in ('raft1024_12','sea1024_4'):
        if reference not in names:
            continue
        flows=np.load(args.output/reference/'flows.npy',mmap_mode='r')
        available=[r for r in ('raft1024_12','sea1024_4') if r in names]
        masks=[np.load(args.output/r/'masks.npy',mmap_mode='r') for r in available]
        for name in names:
            outs=np.load(args.output/name/'outputs.npy',mmap_mode='r')
            values=[]; photos=[]; cover=[]
            own=np.load(args.output/name/'flows.npy',mmap_mode='r')
            for i in selected:
                mask=np.logical_and.reduce([m[i].astype(bool) for m in masks])
                f=resize_flow(flows[i],w,h)
                residual=outs[i,...,:3].astype(np.float32)-inputs[i,...,:3].astype(np.float32)
                previous=outs[i-1,...,:3].astype(np.float32)-inputs[i-1,...,:3].astype(np.float32)
                warped=cv2.remap(previous,xx+f[...,0],yy+f[...,1],cv2.INTER_LINEAR)
                values.append(float(np.abs(residual-warped)[mask].mean()))
                if reference == available[0]:
                    f=resize_flow(own[i],w,h)
                    valid=mask & (xx+f[...,0]>=1)&(xx+f[...,0]<w-2)&(yy+f[...,1]>=1)&(yy+f[...,1]<h-2)
                    rgb=cv2.remap(inputs[i-1,...,:3].astype(np.float32),xx+f[...,0],yy+f[...,1],cv2.INTER_LINEAR)
                    photos.append(float(np.abs(inputs[i,...,:3].astype(np.float32)-rgb)[valid].mean()))
                    cover.append(float(valid.mean()))
            reports[name]['temporal_residual_using_'+reference]=float(np.mean(values))
            if photos:
                reports[name]['photo_common_mask_mae']=float(np.mean(photos))
                reports[name]['common_mask_fraction']=float(np.mean(cover))
    save(args.output/'summary.json',{'measured_pairs':len(selected), 'results':reports,
        'limitations':['Real footage has no ground-truth flow; photometric and FB metrics are proxies.',
            'Two learned temporal reference flows can carry model bias; neither is ground truth.',
            'SDR RGBA8 sRGB test; not the GUI scRGB path.',
            'Three 16-frame windows, first three frames excluded per window; no full-clip temporal claims.',
            'Backward times include preprocessing, upload, inference, download and flow resize, not DLSS or diagnostic forward passes.']})
    print(json.dumps(reports,indent=2),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--source',type=Path)
    parser.add_argument('--variant',choices=VARIANTS)
    parser.add_argument('--summary',action='store_true')
    args=parser.parse_args()
    if args.variant:
        run(args)
    elif args.summary:
        summarize(args)
    else:
        prepare(args)
        for name in VARIANTS:
            subprocess.run([sys.executable,__file__,'--output',str(args.output),'--variant',name],
                check=True,timeout=300,creationflags=subprocess.CREATE_NO_WINDOW)
        summarize(args)


if __name__=='__main__':
    main()
