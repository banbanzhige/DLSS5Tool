"""Export a real, full-resolution depth on/off comparison without GUI state changes.

Each native render runs in a fresh process. Only presentation thumbnails are
resized; source, depth visualization and both DLSS results retain source size.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SOURCES = ('test2.jpg', 'test3.png', 'test4.png', 'test5.png')
COLUMNS = ('original', 'depth', 'dlss-no-depth', 'dlss-depth')
LABELS = ('原图', '深度图', 'DLSS · 关闭深度', 'DLSS · 开启深度')


def sha(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def settings(mode):
    from dlss5tool.app_settings import DEFAULTS
    return {**DEFAULTS, 'style': 0, 'intensity': 1.0,
            'use_local_tone': False, 'local_tone': 0.0,
            'local_struct': 1.0, 'use_auto_mask': 1, 'skin_struct': 1.0,
            'output_mix': 1.0, 'output_view': 0, 'super_resolution_scale': 1,
            'guidance_mode': mode, 'guidance_device': 'cuda',
            'guidance_depth_encoder': 'vitl', 'guidance_depth_profile': 'sdpa_fp16',
            'guidance_depth_edge': 512, 'guidance_cache_mb': 0,
            'guidance_depth_palette': 'gray', 'guidance_depth_invert': False,
            'mods_directory': str(ROOT / 'mods'), 'dlss_runtime': '__bundled__',
            'host_backend': 'v2', 'host_auto_fallback': False,
            'host_tiled_mode': False, 'ui_language': 'zh_CN'}


def render(args):
    import cv2
    import numpy as np
    from dlss5tool import dlss_engine
    from dlss5tool.guidance_visualization import guidance_images

    source = ROOT / 'tmp/img' / args.source
    name = source.stem
    variant = 'dlss-depth' if args.mode else 'dlss-no-depth'
    directory = args.output / name
    rgba = cv2.cvtColor(cv2.imdecode(np.fromfile(source, dtype=np.uint8),
                                   cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGBA)
    height, width = rgba.shape[:2]
    config = settings(args.mode)
    dlss_engine.LOG_PATH = str(args.output / 'records' / f'{name}-{variant}-ngx.log')
    start = time.perf_counter()
    print(f'{name} {variant}: init {width}x{height}', flush=True)
    live = dlss_engine.Live(width, height, config)
    try:
        output = live.process(rgba, reset=True)
        if output is None or output.shape != rgba.shape or output.dtype != np.uint8:
            raise RuntimeError('Missing or invalid native output')
        image_path = directory / f'{variant}.png'
        if not cv2.imwrite(str(image_path), cv2.cvtColor(output, cv2.COLOR_RGBA2BGR)):
            raise RuntimeError(f'Failed to save {image_path}')
        record = {'source': args.source, 'size': [width, height], 'settings': config,
                  'source_file_sha256': sha(source), 'output_file_sha256': sha(image_path),
                  'output_rgb_sha256': hashlib.sha256(output[..., :3].copy()).hexdigest(),
                  'actual_backend': live.backend, 'runtime_path': live.runtime_path,
                  'guidance_info': live.guidance_info, 'guidance_metrics': live.guidance_metrics,
                  'reset': True, 'elapsed_seconds': time.perf_counter() - start}
        if args.mode:
            if live.guidance_info.get('device') != 'cuda':
                raise RuntimeError('Depth must run on the GPU')
            if not np.isfinite(live._dp).all() or float(np.ptp(live._dp)) <= 0:
                raise RuntimeError('Invalid or empty depth prediction')
            if np.any(live._mv):
                raise RuntimeError('This comparison must not contain optical flow')
            depth_path = directory / 'depth.png'
            depth_image = guidance_images(live._mv, live._dp, 2, config)['depth']
            if not cv2.imwrite(str(depth_path), depth_image):
                raise RuntimeError(f'Failed to save {depth_path}')
            record['depth_file_sha256'] = sha(depth_path)
            record['depth_float_sha256'] = hashlib.sha256(live._dp).hexdigest()
            record['depth_range'] = [float(live._dp.min()), float(live._dp.max())]
        (args.output / 'records' / f'{name}-{variant}.json').write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'{name} {variant}: saved, {record["elapsed_seconds"]:.2f}s', flush=True)
    finally:
        live.close_guidance()
    # Match the existing offline-render scripts: process exit owns native NGX
    # teardown, avoiding the known driver shutdown hang. No GUI is terminated.


def assemble(root):
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 24)
    title_font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 38)
    label_font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 28)
    cell_w, cell_h, gap = 600, 650, 16
    width = 4 * cell_w + 5 * gap
    header = 225
    row_heights = []
    for source in SOURCES:
        with Image.open(root / Path(source).stem / 'original.png') as im:
            scaled_height = round(im.height * min(cell_w / im.width, cell_h / im.height, 1))
            row_heights.append(78 + scaled_height + 34)
    board = Image.new('RGB', (width, header + sum(row_heights) + 55), '#f4f6f8')
    draw = ImageDraw.Draw(board)
    draw.text((gap, 18), 'DLSS5Tool · 深度引导实测对比', font=title_font, fill='#152638')
    draw.text((gap, 76), '本地色调关闭｜强度 / 输出混合 / 本地结构 / 皮肤蒙版 = 1｜默认风格｜无超分、无光流',
              font=font, fill='#435467')
    draw.text((gap, 113), '深度：ViT-L · 分析长边 512 · SDPA FP16｜仅此总览等比缩小，完整 PNG 保持原尺寸',
              font=font, fill='#435467')
    metrics = []
    lines = ['# 深度引导实测对比', '',
             '四组素材均为实际模型推理与原生 DLSS 渲染输出，不是示意图。点击缩略图查看原尺寸 PNG。', '',
             '**统一参数：** 默认风格；本地色调关闭（实际值 0）；强度、输出混合、本地结构、皮肤蒙版均为 1，皮肤蒙版开启；不超分、不使用光流。', '',
             '**深度设置：** Depth Anything V2 Large（ViT-L），分析长边 512，SDPA FP16；灰度、不反相。深度图为相对深度的 8 位可视化，不是照片或实际距离。', '',
             '每张图片使用独立会话、首帧重置。两组保持其余参数和源尺寸一致，仅切换深度引导。', '',
             '![四组总览](comparison.png)', '']
    for index, source in enumerate(SOURCES):
        name = Path(source).stem
        directory = root / name
        original = Image.open(directory / 'original.png').convert('RGB')
        y = header + sum(row_heights[:index])
        draw.text((gap, y), f'{name} · {original.width} × {original.height}', font=label_font, fill='#152638')
        lines += [f'## {name} · {original.width} × {original.height}', '',
                  '| 原图 | 深度图 | DLSS · 关闭深度 | DLSS · 开启深度 |',
                  '| --- | --- | --- | --- |']
        cells = []
        for col, (key, label) in enumerate(zip(COLUMNS, LABELS)):
            path = directory / f'{key}.png'
            with Image.open(path) as im:
                if im.size != original.size:
                    raise RuntimeError(f'Output dimensions differ: {path}')
                thumb = im.convert('RGB')
                thumb.thumbnail((cell_w, cell_h), Image.Resampling.LANCZOS)
                thumb.save(directory / f'{key}-preview.png')
            x = gap + col * (cell_w + gap)
            draw.text((x, y + 42), label, font=font, fill='#435467')
            board.paste(thumb, (x + (cell_w - thumb.width) // 2, y + 78))
            cells.append(f'[![{label}]({name}/{key}-preview.png)]({name}/{key}.png)')
        lines += ['| ' + ' | '.join(cells) + ' |', '']
        off = cv2.imread(str(directory / 'dlss-no-depth.png'))
        on = cv2.imread(str(directory / 'dlss-depth.png'))
        delta = np.abs(on.astype(np.int16) - off.astype(np.int16))
        metrics.append({'source': source, 'size': list(original.size),
                        'mean_abs_channel_difference_8bit': float(delta.mean()),
                        'max_channel_difference_8bit': int(delta.max()),
                        'changed_pixel_fraction': float(np.any(delta > 0, axis=2).mean())})
    draw.text((gap, board.height - 47), '原图 / 深度 / 深度关闭 / 深度开启｜结果因素材而异；静态图片不能证明闪烁改善。',
              font=font, fill='#435467')
    if all(item['max_channel_difference_8bit'] == 0 for item in metrics):
        result_note = '本次四组深度开启 / 关闭的最终 RGB 像素完全一致；深度图为真实模型输出。'
        draw.text((gap, 157), result_note, font=font, fill='#8a4b08')
        lines[4:4] = ['> **实测结果：** ' + result_note + ' 这组结果不能作为深度引导提升画质的证据，也不代表所有素材或运行库都无差异。', '']
    board.save(root / 'comparison.png')
    lines += ['## 阅读说明', '',
              '- 优先对照灯光、边缘、材质及暗部；不要把所有阴影差异都当成伪影改善。',
              '- 静态图只能展示空间与外观差异，不能用于证明光流效果或视频闪烁改善。',
              '- 原图由输入解码后无损保存；test2 的输入本身是 JPEG，转为 PNG 不会恢复其已损失的信息。',
              '- 展示缩略图均使用同一种等比缩放，不额外锐化、降噪或调色。',
              '- 本目录没有改动源素材或应用设置。参数、运行设备及校验记录位于 records。', '']
    (root / 'README.md').write_text('\n'.join(lines), encoding='utf-8')
    (root / 'records/comparison-metrics.json').write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(metrics, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source', choices=SOURCES)
    parser.add_argument('--mode', type=int, choices=(0, 2))
    parser.add_argument('--assemble-only', action='store_true')
    args = parser.parse_args()
    args.output = args.output.resolve()
    if args.source:
        if args.mode is None:
            parser.error('--source requires --mode')
        render(args)
        return
    if args.assemble_only:
        assemble(args.output)
        return
    import cv2
    import numpy as np
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / 'records').mkdir()
    for source in SOURCES:
        destination = args.output / Path(source).stem
        destination.mkdir()
        bgr = cv2.imdecode(np.fromfile(ROOT / 'tmp/img' / source, dtype=np.uint8), cv2.IMREAD_COLOR)
        if bgr is None or not cv2.imwrite(str(destination / 'original.png'), bgr):
            raise RuntimeError(f'Cannot decode/save {source}')
    # Smallest landscape first as a native/GPU smoke test; display order stays 2–5.
    for source in ('test3.png', 'test2.jpg', 'test4.png', 'test5.png'):
        for mode in (0, 2):
            subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()),
                            '--output', str(args.output), '--source', source, '--mode', str(mode)],
                           check=True, timeout=180,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assemble(args.output)


if __name__ == '__main__':
    main()
