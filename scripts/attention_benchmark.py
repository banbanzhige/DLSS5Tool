"""Isolated attention/precision experiment; never changes production modules.

Run with the existing CUDA Python. xFormers is loaded from a separate --deps
directory. Each variant gets a fresh subprocess and the same prepared frames.
Only depth attention/precision changes; RAFT stays FP32 with six updates.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')


def prepare(args):
    import cv2
    import numpy as np
    args.output.mkdir(parents=True, exist_ok=False)
    cap = cv2.VideoCapture(str(args.source))
    frames, indices, starts = [], [], []
    try:
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        span = args.samples + 3
        if count < span * 3:
            raise ValueError('Need three non-overlapping video segments')
        starts = [0, (count - span) // 2, count - span]
        for start in starts:
            cap.set(cv2.CAP_PROP_POS_FRAMES, start)
            for index in range(start, start + span):
                ok, frame = cap.read()
                if not ok:
                    raise ValueError(f'Cannot decode frame {index}')
                frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2RGBA))
                indices.append(index)
    finally:
        cap.release()
    np.save(args.output / 'inputs.npy', np.stack(frames))
    write_json(args.output / 'input.json', {'source': str(args.source.resolve()),
        'source_frames': count, 'indices': indices, 'starts': starts,
        'samples_per_segment': args.samples, 'warmup_per_segment': 3,
        'shape': list(frames[0].shape),
        'input_sha256': hashlib.sha256(memoryview(np.stack(frames))).hexdigest()})


def configure_attention(model, backend):
    import torch
    import torch.nn.functional as F
    from depth_anything_v2.dinov2_layers.attention import Attention, MemEffAttention
    if backend == 'xformers':
        from depth_anything_v2.dinov2_layers.attention import XFORMERS_AVAILABLE
        if not XFORMERS_AVAILABLE:
            raise RuntimeError('Depth architecture did not enable xFormers; refusing silent fallback')
    count = 0
    def sdpa(self, x, attn_bias=None):
        if attn_bias is not None:
            raise ValueError('This probe only supports ordinary non-nested attention')
        b, n, c = x.shape
        qkv = self.qkv(x).reshape(b, n, 3, self.num_heads, c // self.num_heads).permute(2, 0, 3, 1, 4)
        q, k, v = qkv.unbind(0)
        # The API applies self.scale exactly once. Dropout is disabled in eval.
        x = F.scaled_dot_product_attention(q, k, v, dropout_p=0.0, scale=self.scale)
        return self.proj_drop(self.proj(x.transpose(1, 2).reshape(b, n, c)))
    for module in model.modules():
        if isinstance(module, Attention):
            function = {'vanilla': Attention.forward, 'xformers': MemEffAttention.forward, 'sdpa': sdpa}[backend]
            module.forward = types.MethodType(function, module)
            count += 1
    if count != 24:
        raise ValueError(f'Expected ViT-L 24 attention layers, found {count}')
    return count


def run_variant(args):
    sys.path.insert(0, str(args.deps.resolve()))
    sys.path.insert(0, str(ROOT / 'tmp/dlss5standaloneV2/models'))
    import cv2
    import numpy as np
    import torch
    try:
        import xformers
    except ImportError:
        xformers = None
    import dlss_engine
    from guidance_worker import Models

    backend, precision = args.variant.split('_')
    if backend == 'xformers' and xformers is None:
        raise RuntimeError('xFormers has not been installed in the isolated dependency directory')
    directory = args.output / f'{args.variant}-r{args.round}'
    directory.mkdir(exist_ok=False)
    metadata = json.loads((args.output / 'input.json').read_text(encoding='utf-8'))
    frames = np.load(args.output / 'inputs.npy', mmap_mode='r')
    _, h, w, _ = frames.shape
    sample_count = metadata['samples_per_segment'] * 3
    span = metadata['samples_per_segment'] + 3
    is_reference = args.variant == 'vanilla_fp32' and args.round == 0
    if is_reference:
        reference_output = np.lib.format.open_memmap(args.output / 'reference-output.npy',
            mode='w+', dtype=np.uint8, shape=(sample_count, h, w, 4))
        reference_depth = np.lib.format.open_memmap(args.output / 'reference-depth.npy',
            mode='w+', dtype=np.float32, shape=(sample_count, h, w))
    else:
        reference_output = np.load(args.output / 'reference-output.npy', mmap_mode='r')
        reference_depth = np.load(args.output / 'reference-depth.npy', mmap_mode='r')

    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = True
    settings = {'guidance_mode': 3, 'guidance_device': 'cuda', 'guidance_edge': 720,
        'guidance_depth_encoder': 'vitl', 'guidance_flow_direction': 'backward',
        'flow_weights': str(ROOT / 'mods/models/raft_large_C_T_SKHT_V2-ff5fadd5.pth'),
        'depth_weights': str(ROOT / 'mods/models/depth_anything_v2_vitl.pth'),
        'mods_directory': str(ROOT / 'mods'), 'host_backend': 'v2', 'host_auto_fallback': False,
        'host_in_flight': 1, 'host_persistent_buffers': True, 'host_submission': 'merged',
        'style': 0, 'intensity': 1.0}
    load_start = time.perf_counter()
    models = Models(settings)
    patched = configure_attention(models.depth, backend)
    depth_forward = models.depth.forward
    dtype = {'fp16': torch.float16, 'bf16': torch.bfloat16}.get(precision)
    start_event, end_event = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
    def depth_with_precision(tensor):
        start_event.record()
        with torch.autocast('cuda', dtype=dtype) if dtype else contextlib.nullcontext():
            values = depth_forward(tensor)
        # Production CPU normalization remains FP32 (NumPy cannot consume BF16).
        values = values.float()
        end_event.record()
        return values
    models.depth.forward = depth_with_precision

    class LocalGuidance:
        info = {'device': 'cuda', 'device_name': models.device_name, 'precision': precision,
                'attention': backend, 'transport': 'in_process_probe'}
        mv = np.empty((h, w, 2), np.float32)
        dp = np.empty((h, w), np.float32)
        @property
        def last_metrics(self):
            return models.last_metrics
        def process(self, frame, reset=False, *, copy_outputs=False):
            return models.process(frame, reset, outputs=(self.mv, self.dp))
        def close(self):
            pass

    dlss_engine.LOG_PATH = str(directory / 'ngx.log')
    live = dlss_engine.Live(w, h, settings)
    local = LocalGuidance()
    live._guidance = local
    load_seconds = time.perf_counter() - load_start
    records, sample = [], 0
    for index, source in enumerate(frames):
        # The read-only mmap is copied before timing, matching mutable production input.
        frame = np.array(source)
        if index % span == 3:
            torch.cuda.reset_peak_memory_stats()
        tick = time.perf_counter()
        output = live.process(frame, reset=index % span == 0)
        elapsed = (time.perf_counter() - tick) * 1000
        if output is None:
            raise RuntimeError('Native DLSS returned no output')
        if index % span < 3:
            continue
        depth_gpu_ms = start_event.elapsed_time(end_event)
        metrics = dict(models.last_metrics)
        if not np.isfinite(local.dp).all() or not np.isfinite(local.mv).all():
            raise RuntimeError('Non-finite guidance output')
        if is_reference:
            reference_output[sample] = output
            reference_depth[sample] = local.dp
        # All quality analysis is outside the timed call.
        diff = np.abs(output[..., :3].astype(np.int16) - reference_output[sample, ..., :3].astype(np.int16))
        depth_diff = np.abs(local.dp - reference_depth[sample])
        record = {'source_frame': metadata['indices'][index], 'process_ms': elapsed,
            'depth_gpu_ms': depth_gpu_ms, **metrics,
            'depth_mae': float(depth_diff.mean()), 'depth_max': float(depth_diff.max()),
            'depth_p99_sampled': float(np.percentile(depth_diff[::4, ::4], 99)),
            'output_mae_8bit': float(diff.mean()), 'output_max_8bit': int(diff.max()),
            'output_changed_fraction': float(np.count_nonzero(diff) / diff.size),
            'output_sha256': hashlib.sha256(memoryview(output)).hexdigest(),
            'depth_sha256': hashlib.sha256(memoryview(local.dp)).hexdigest(),
            'flow_sha256': hashlib.sha256(memoryview(local.mv)).hexdigest()}
        records.append(record)
        if sample % metadata['samples_per_segment'] == 0:
            cv2.imwrite(str(directory / f'output-{sample}.png'), cv2.cvtColor(output, cv2.COLOR_RGBA2BGRA))
            depth_preview = np.uint8(np.clip(local.dp, 0, 1) * 255)
            cv2.imwrite(str(directory / f'depth-{sample}.png'), cv2.resize(depth_preview, (512, 512)))
        sample += 1
        if sample % metadata['samples_per_segment'] == 0:
            print(f'{args.variant} r{args.round}: {sample}/{sample_count}', flush=True)
    if is_reference:
        reference_output.flush()
        reference_depth.flush()
    summary = {'variant': args.variant, 'round': args.round, 'samples': len(records),
        'device': models.device_name, 'torch': torch.__version__, 'cuda': torch.version.cuda,
        'xformers': xformers.__version__ if xformers else None, 'depth_precision': precision, 'flow_precision': 'fp32',
        'attention_layers': patched, 'load_seconds': load_seconds,
        'process_mean_ms': statistics.mean(r['process_ms'] for r in records),
        'process_median_ms': statistics.median(r['process_ms'] for r in records),
        'depth_mean_ms': statistics.mean(r['depth_ms'] for r in records),
        'depth_gpu_mean_ms': statistics.mean(r['depth_gpu_ms'] for r in records),
        'flow_mean_ms': statistics.mean(r['flow_ms'] for r in records),
        'peak_allocated_mib': max(r['peak_allocated_mib'] for r in records),
        'peak_reserved_mib': max(r['peak_reserved_mib'] for r in records),
        'depth_mae': statistics.mean(r['depth_mae'] for r in records),
        'depth_max': max(r['depth_max'] for r in records),
        'output_mae_8bit': statistics.mean(r['output_mae_8bit'] for r in records),
        'output_max_8bit': max(r['output_max_8bit'] for r in records),
        'output_identical': all(r['output_max_8bit'] == 0 for r in records)}
    summary['fps_process_only'] = 1000 / summary['process_mean_ms']
    write_json(directory / 'result.json', {'summary': summary, 'frames': records})
    print(json.dumps(summary), flush=True)
    # Profile separately; CPU operator names also identify dispatch without CUPTI.
    try:
        with torch.profiler.profile(activities=[torch.profiler.ProfilerActivity.CPU,
                                                torch.profiler.ProfilerActivity.CUDA]) as prof:
            live.process(np.array(frames[-1]), False)
        events = [{'name': event.key, 'calls': event.count} for event in prof.key_averages()
                  if any(token in event.key.lower() for token in ('attention', 'fmha', 'cutlass'))]
        write_json(directory / 'attention-operators.json', events)
    except Exception as exc:
        write_json(directory / 'attention-operators.json', {'error': repr(exc)})
    live.close_guidance()
    # Never invoke native shutdown: this disposable process reclaims D3D12 state.


def suite(args):
    if not (args.output / 'input.json').is_file():
        prepare(args)
    variants = args.variants
    if variants[0] != 'vanilla_fp32' and not (args.output / 'reference-output.npy').is_file():
        raise ValueError('First variant must create the vanilla_fp32 reference')
    report = []
    for run in range(args.rounds):
        ordered = variants if run % 2 == 0 else list(reversed(variants))
        for variant in ordered:
            command = [sys.executable, str(Path(__file__).resolve()), 'run', '--output', str(args.output.resolve()),
                '--deps', str(args.deps.resolve()), '--variant', variant, '--round', str(run + args.start_round)]
            result = subprocess.run(command, cwd=ROOT, timeout=180,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            directory = args.output / f'{variant}-r{run + args.start_round}'
            if result.returncode:
                report.append({'variant': variant, 'round': run + args.start_round, 'failed': result.returncode})
                if variant == 'vanilla_fp32' and run + args.start_round == 0:
                    raise RuntimeError('Baseline failed')
            else:
                report.append(json.loads((directory / 'result.json').read_text(encoding='utf-8'))['summary'])
            write_json(args.output / f'suite-r{args.start_round}.json', report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['suite', 'run'])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--deps', type=Path, required=True)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--samples', type=int, default=8)
    parser.add_argument('--rounds', type=int, default=1)
    parser.add_argument('--start-round', type=int, default=0)
    parser.add_argument('--variants', nargs='+', default=['vanilla_fp32', 'xformers_fp32', 'sdpa_fp32',
        'vanilla_fp16', 'xformers_fp16', 'sdpa_fp16', 'xformers_bf16', 'sdpa_bf16'])
    parser.add_argument('--variant')
    parser.add_argument('--round', type=int, default=0)
    args = parser.parse_args()
    if args.command == 'run':
        run_variant(args)
    else:
        suite(args)


if __name__ == '__main__':
    main()
