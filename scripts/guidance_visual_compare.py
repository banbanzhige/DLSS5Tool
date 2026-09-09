"""Historical sampled-frame comparison of guidance optimizations.

Uses the same prepared real-video windows, fresh process per variant, current
native host and fixed appearance settings. Never modifies production settings.
The final-only RAFT variant was withdrawn after real-footage temporal jitter.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SELECTED = [3, 10, 14, 21, 25, 32]
LABELS = {'fp32': '原始 FP32', 'sdpa': 'SDPA + FP16（当前）',
          'raft': '加 RAFT 精简', 'streams': '加双 Stream',
          'combined': 'RAFT 精简 + 双 Stream', 'repeat': 'FP32 重复运行'}


def run(args):
    sys.path.insert(0, str(ROOT / 'tmp/dlss5standaloneV2/models'))
    import cv2
    import numpy as np
    import torch
    from dlss5tool import dlss_engine
    from dlss5tool.guidance_worker import Models
    from scripts.guidance_schedule_probe import raft_final_only, parallel_process

    inputs = np.load(ROOT / 'output/attention-ab-20260908/inputs.npy', mmap_mode='r')
    metadata = json.loads((ROOT / 'output/attention-ab-20260908/input.json').read_text(encoding='utf-8'))
    directory = args.output / args.variant
    directory.mkdir(exist_ok=False)
    _, h, w, _ = inputs.shape
    profile = 'fp32' if args.variant in ('fp32', 'repeat') else 'sdpa_fp16'
    settings = {'guidance_mode': 3, 'guidance_device': 'cuda', 'guidance_edge': 720,
        'guidance_depth_encoder': 'vitl', 'guidance_depth_profile': profile,
        'guidance_flow_direction': 'backward',
        'flow_weights': str(ROOT / 'mods/models/raft_large_C_T_SKHT_V2-ff5fadd5.pth'),
        'depth_weights': str(ROOT / 'mods/models/depth_anything_v2_vitl.pth'),
        'mods_directory': str(ROOT / 'mods'), 'host_backend': 'v2', 'host_auto_fallback': False,
        'host_in_flight': 1, 'host_persistent_buffers': True, 'host_submission': 'merged',
        'style': 0, 'intensity': 1.0, 'local_tone': 1.0, 'local_struct': 1.0,
        'skin_struct': 0.5, 'use_auto_mask': 0, 'ui_correction': 0}
    model = Models(settings)
    if args.variant in ('raft', 'combined'):
        model.flow.forward = types.MethodType(raft_final_only, model.flow)
    if args.variant in ('streams', 'combined'):
        model.flow_stream = torch.cuda.Stream()
        model.depth_stream = torch.cuda.Stream()
        model.process = types.MethodType(parallel_process, model)

    class Guidance:
        info = {'device': 'cuda', 'device_name': model.device_name, 'precision': model.precision}
        mv = np.empty((h, w, 2), np.float32)
        dp = np.empty((h, w), np.float32)
        @property
        def last_metrics(self): return model.last_metrics
        def process(self, frame, reset=False, *, copy_outputs=False):
            return model.process(frame, reset, outputs=(self.mv, self.dp))
        def close(self): pass

    dlss_engine.LOG_PATH = str(directory / 'ngx.log')
    live = dlss_engine.Live(w, h, settings)
    local = Guidance()
    live._guidance = local
    reference = np.lib.format.open_memmap(args.output / 'reference.npy', mode='w+', dtype=np.uint8,
        shape=inputs.shape) if args.variant == 'fp32' else np.load(args.output / 'reference.npy', mmap_mode='r')
    records = []
    for index, source in enumerate(inputs):
        frame = np.array(source)
        output = live.process(frame, reset=index % 11 == 0)
        if output is None: raise RuntimeError('No DLSS output')
        if not np.isfinite(local.mv).all() or not np.isfinite(local.dp).all():
            raise RuntimeError('Nonfinite guidance')
        if args.variant == 'fp32': reference[index] = output
        delta = np.abs(output[..., :3].astype(np.int16) - reference[index, ..., :3].astype(np.int16))
        record = {'input_frame': metadata['indices'][index], 'window_offset': index % 11,
            'max_8bit': int(delta.max()), 'mae_8bit': float(delta.mean()),
            'changed_channels': int(np.count_nonzero(delta)),
            'output_sha256': hashlib.sha256(memoryview(output)).hexdigest(),
            'flow_sha256': hashlib.sha256(memoryview(local.mv)).hexdigest(),
            'depth_sha256': hashlib.sha256(memoryview(local.dp)).hexdigest()}
        records.append(record)
        if index in SELECTED:
            source_id = metadata['indices'][index]
            cv2.imwrite(str(directory / f'frame-{source_id:03d}.png'), cv2.cvtColor(output, cv2.COLOR_RGBA2BGRA))
            if args.variant == 'fp32':
                cv2.imwrite(str(args.output / f'source-{source_id:03d}.png'), cv2.cvtColor(frame, cv2.COLOR_RGBA2BGRA))
        if index % 11 == 10:
            print(args.variant, f'{index+1}/{len(inputs)}', flush=True)
    if args.variant == 'fp32': reference.flush()
    (directory / 'result.json').write_text(json.dumps({'variant': args.variant,
        'settings': settings, 'frames': records}, indent=2), encoding='utf-8')
    live.close_guidance()
    # Process exit owns NGX cleanup; do not call known-problematic shutdown.


def boards(args):
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    metadata = json.loads((ROOT / 'output/attention-ab-20260908/input.json').read_text(encoding='utf-8'))
    selected = [metadata['indices'][i] for i in SELECTED]
    variants = ['fp32', 'sdpa', 'raft', 'streams', 'combined', 'repeat']
    results = {v: json.loads((args.output / v / 'result.json').read_text(encoding='utf-8'))['frames'] for v in variants}
    summary = {v: {'frames': len(results[v]), 'max_8bit': max(x['max_8bit'] for x in results[v]),
        'changed_channels': sum(x['changed_channels'] for x in results[v]),
        'sampled_frames': selected} for v in variants}
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')
    font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 22)
    title_font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 28)
    bg = '#151b23'
    def canvas(size, title, subtitle):
        img = Image.new('RGB', size, bg)
        draw = ImageDraw.Draw(img)
        draw.text((20, 12), title, font=title_font, fill='white')
        draw.text((20, 52), subtitle, font=font, fill='#c7d2df')
        return img, draw
    def read(variant, frame):
        return Image.open(args.output / variant / f'frame-{frame:03d}.png').convert('RGB')
    # Six real frames, before/after. Downscaling is presentation only; raw PNGs retained.
    overview, draw = canvas((1536, 1160), '原片六帧：FP32基线 / RAFT精简＋双Stream（深度SDPA FP16）',
        '每组左为基线、右为组合；均为同一源帧，按连续窗口渲染后抽取，非单帧重置推理。')
    for n, frame in enumerate(selected):
        gx = (n % 2) * 768
        gy = 96 + (n // 2) * 350
        for col, variant in enumerate(('fp32', 'combined')):
            x = gx + col * 376 + 12
            draw.text((x, gy), f'帧 {frame} · ' + ('FP32' if col == 0 else '组合'), font=font, fill='white')
            overview.paste(read(variant, frame).resize((306,306),Image.Resampling.LANCZOS), (x,gy+34))
    overview.save(args.output / 'overview.png')
    # Middle frame: full picture then true 1:1 crops; no sharpening or denoising.
    frame = 80
    detail, draw = canvas((1960, 1540), f'帧 {frame}：完整画面与 1:1 原像素细节',
        '第一行缩小展示；后三行不缩放，依次为眼部／刘海／手指与发卷边缘。')
    rois = [(480,650,960,910), (300,340,780,600), (920,1080,1400,1340)]
    shown = ['sdpa','raft','streams','combined']
    for col, variant in enumerate(shown):
        x = 10 + col * 490
        draw.text((x,96),LABELS[variant],font=font,fill='white')
        source = read(variant,frame)
        detail.paste(source.resize((480,480),Image.Resampling.LANCZOS),(x,134))
        for row, roi in enumerate(rois):
            y=650+row*290
            draw.text((x,y-30),['眼部 1:1','刘海 1:1','手指／发卷 1:1'][row],font=font,fill='#c7d2df')
            detail.paste(source.crop(roi),(x,y))
    detail.save(args.output/'details-frame080.png')
    diffboard, draw = canvas((1440, 1110), '绝对差异 ×32（黑色＝像素相同）',
        '比较 FP32 与组合方案；固定倍率，不自动拉伸。看不到差异不代替数值检查。')
    for n, frame in enumerate(selected):
        x=(n%3)*480+10; y=110+(n//3)*495
        a=np.array(read('fp32',frame)).astype(np.int16)
        b=np.array(read('combined',frame)).astype(np.int16)
        delta=np.abs(a-b)
        draw.text((x,y),f'帧 {frame} · 最大差 {delta.max()}/255',font=font,fill='white')
        pic=Image.fromarray(np.uint8(np.clip(delta*32,0,255)))
        diffboard.paste(pic.resize((460,460),Image.Resampling.NEAREST),(x,y+32))
    diffboard.save(args.output/'difference-x32.png')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--variant')
    parser.add_argument('--boards-only',action='store_true')
    args=parser.parse_args()
    if args.variant: run(args); return
    if not args.boards_only:
        args.output.mkdir(parents=True,exist_ok=False)
        for variant in LABELS:
            subprocess.run([sys.executable,__file__,'--output',str(args.output),'--variant',variant],
                check=True,timeout=120,creationflags=subprocess.CREATE_NO_WINDOW)
    boards(args)


if __name__=='__main__':main()
