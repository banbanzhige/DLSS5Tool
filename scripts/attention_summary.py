"""Summarize isolated attention experiments without importing CUDA libraries."""
import argparse
import json
from pathlib import Path
import statistics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--rounds', type=int, nargs='+', default=[2, 3])
    args = parser.parse_args()
    reference = json.loads((args.directory / 'vanilla_fp32-r0/result.json').read_text(encoding='utf-8'))
    reference_flows = [f['flow_sha256'] for f in reference['frames']]
    by_variant = {}
    for path in sorted(args.directory.glob('*/result.json')):
        data = json.loads(path.read_text(encoding='utf-8'))
        summary = data['summary']
        if summary['round'] not in args.rounds:
            continue
        summary = {**summary, 'flow_matches_baseline': reference_flows == [f['flow_sha256'] for f in data['frames']]}
        by_variant.setdefault(summary['variant'], []).append(summary)
    report = {}
    for variant, samples in by_variant.items():
        report[variant] = {'rounds': [s['round'] for s in samples],
            'samples': sum(s['samples'] for s in samples),
            **{key: statistics.mean(s[key] for s in samples) for key in
               ('process_mean_ms', 'depth_mean_ms', 'depth_gpu_mean_ms', 'flow_mean_ms', 'depth_mae', 'output_mae_8bit')},
            'peak_allocated_mib': max(s['peak_allocated_mib'] for s in samples),
            'peak_reserved_mib': max(s['peak_reserved_mib'] for s in samples),
            'depth_max': max(s['depth_max'] for s in samples),
            'output_max_8bit': max(s['output_max_8bit'] for s in samples),
            'all_outputs_identical_to_baseline': all(s['output_identical'] for s in samples),
            'all_flows_identical_to_baseline': all(s['flow_matches_baseline'] for s in samples)}
        report[variant]['fps_process_only'] = 1000 / report[variant]['process_mean_ms']
    (args.directory / 'summary.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    for variant, record in report.items():
        print(variant, json.dumps(record))


if __name__ == '__main__':
    main()
