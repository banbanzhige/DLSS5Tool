"""Bounded real-worker regression and HDR analysis/export smoke checks."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2
import numpy as np
from dlss5tool.guidance_client import GuidanceSession
from dlss5tool.guidance_export import export_guidance
from dlss5tool.preview_comparison import guidance_input_pair
from dlss5tool.video_export import find_ffmpeg, probe_video_stream


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--component', type=Path, required=True, help='directory containing enhancement/')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    settings = {'guidance_mode': 1, 'guidance_flow_edge': 512, 'guidance_device': 'cuda',
                'guidance_flow_updates': 6, 'guidance_cache_mb': 0,
                'guidance_flow_weights': str(ROOT / 'mods/enhancement/models/raft_large_C_T_SKHT_V2-ff5fadd5.pth')}
    # A narrow frame exercises the full 2048 edge with a bounded correlation
    # volume. This is not a 2048-square VRAM guarantee or quality benchmark.
    source = cv2.imread(str(ROOT / 'img/01.png'))
    if source is None:
        raise RuntimeError('Missing probe source img/01.png')
    first = cv2.cvtColor(cv2.resize(source, (2048, 256)), cv2.COLOR_BGR2RGBA)
    second = np.roll(first, 8, axis=1)
    report = {}

    def run(label, component, edge):
        session = GuidanceSession({**settings, 'mods_directory': str(component.resolve()),
                                   'guidance_flow_edge': edge}, 2048, 256)
        try:
            motion, _, reset = session.process(first, True)
            assert reset and not motion.any()
            motion, _, reset = session.process(second, False)
            assert not reset and np.isfinite(motion).all() and np.abs(motion).max() > 0
            report[label] = {'info': session.info, 'metrics': session.last_metrics,
                             'motion_abs_mean': float(np.abs(motion).mean())}
            return motion
        finally:
            session.close()

    before = run('baseline_512', ROOT / 'mods', 512)
    after = run('updated_512', args.component, 512)
    np.testing.assert_array_equal(before, after)
    run('updated_2048', args.component, 2048)
    report['default_bit_exact'] = True
    ffmpeg = find_ffmpeg()
    for profile in ('hdr10', 'hlg'):
        source = ROOT / f'tmp/hdr-e2e/source-{profile}.mp4'
        info = probe_video_stream(ffmpeg, str(source))
        assert info['is_hdr']
        current, previous = guidance_input_pair(str(source), 2, None, settings, info)
        assert current.dtype == previous.dtype == np.uint8 and current.shape == previous.shape
        output = args.output / f'{profile}-flow.png'
        count = export_guidance(source, output, {**settings, 'mods_directory': str(args.component.resolve())},
                                'flow', frame=2, color_info=info)
        assert count == 1 and cv2.imread(str(output)).shape[:2] == (info['height'], info['width'])
        report[profile + '_analysis'] = {'preview': True, 'png': str(output), 'size': current.shape[:2]}
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'ok': True, 'default_bit_exact': True, 'edge_2048': True,
                      'hdr_analysis': True, 'report': str(args.output / 'report.json')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
