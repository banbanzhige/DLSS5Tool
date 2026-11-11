"""Summarize bounded worker-recycling evidence without equating exit to plateau."""
import argparse
import json
from pathlib import Path
import statistics

MIB = 1024**2


def load_lines(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def process_value(sample, pid, key, *, gpu=False):
    matches = [r for r in sample.get('gpu_processes' if gpu else 'processes', []) if r['pid'] == pid]
    return matches[0].get(key) if matches else None


def trend(samples, pid, key='private_bytes', *, gpu=False):
    points = [(s['wall'], process_value(s, pid, key, gpu=gpu)) for s in samples]
    points = [(t, v) for t, v in points if v is not None]
    if len(points) < 4:
        return dict(samples=len(points), measured=False)
    count = max(1, len(points)//5)
    before, after = median([v for _, v in points[:count]]), median([v for _, v in points[-count:]])
    elapsed = median([t for t, _ in points[-count:]])-median([t for t, _ in points[:count]])
    delta = after-before
    return dict(measured=True, samples=len(points), first_window_bytes=before,
        last_window_bytes=after, delta_bytes=delta, median_window_span_seconds=elapsed,
        mib_per_minute=delta/MIB/max(elapsed, .001)*60,
        max_observed_bytes=max(v for _, v in points), min_observed_bytes=min(v for _, v in points))


def summarize(work):
    result = json.loads((work/'result.json').read_text(encoding='utf-8'))
    samples = load_lines(work/'samples.jsonl')
    events = load_lines(work/'events.jsonl')
    select = lambda start, end: [s for s in samples if start <= s['wall'] <= end]
    idle = []
    for event in events:
        if event['event'] != 'idle-start':
            continue
        end = next((e for e in events if e['event']=='idle-end' and e['cycle']==event['cycle']), None)
        if end is None:
            continue
        window = select(event['wall']+10, end['wall'])
        idle.append(dict(cycle=event['cycle'], pid=event['worker_pid'],
            private_commit=trend(window, event['worker_pid']),
            dedicated_gpu=trend(window, event['worker_pid'], 'dedicated_bytes', gpu=True),
            committed_gpu=trend(window, event['worker_pid'], 'gpu_committed_bytes', gpu=True),
            shared_gpu=trend(window, event['worker_pid'], 'shared_bytes', gpu=True)))
    closes = []
    for start in events:
        if start['event'] != 'close-start':
            continue
        closed = next((e for e in events if e['event']=='closed' and e['cycle']==start['cycle']), None)
        end = next((e for e in events if e['event']=='post-close' and e['cycle']==start['cycle']), None)
        if closed is None or end is None:
            closes.append(dict(cycle=start['cycle'], returned=False)); continue
        before = select(start['wall']-5, start['wall'])
        after = select(max(closed['wall'], end['wall']-5), end['wall'])
        pid = start['worker_pid']
        system_before = median([s['system']['commit_bytes'] for s in before])
        system_after = median([s['system']['commit_bytes'] for s in after])
        closes.append(dict(cycle=start['cycle'], pid=pid, returned=True,
            close_seconds=closed['wall']-start['wall'], post_close_samples=len(after),
            worker_absent_in_all_post_close_samples=bool(after) and all(pid not in s['members'] for s in after),
            pre_close_worker_private_bytes=median([process_value(s, pid, 'private_bytes') for s in before]),
            post_close_job_private_bytes=median([s['private_bytes'] for s in after]),
            pre_close_system_commit_bytes=system_before, post_close_system_commit_bytes=system_after,
            system_commit_drop_bytes=(system_before-system_after if system_before is not None and system_after is not None else None),
            pre_close_dedicated_gpu_bytes=median([process_value(s, pid, 'dedicated_bytes', gpu=True) for s in before]),
            post_close_dedicated_gpu_bytes=median([process_value(s, pid, 'dedicated_bytes', gpu=True) for s in after]),
            gpu_counter_presence_after=sum(any(r['pid']==pid for r in s.get('gpu_processes', [])) for s in after),
            shared_frames_unlinked=closed['shared_frames_unlinked']))
    pid = next((e['worker_pid'] for e in events if e['event']=='opened'), None)
    idle_starts = [e for e in events if e['event']=='idle-start']
    idle_ends = [e for e in events if e['event']=='idle-end']
    combined = (trend(select(idle_starts[0]['wall']+10, idle_ends[-1]['wall']), pid)
                if idle_starts and idle_ends and '-hold-' in work.name else None)
    processed = [e for e in events if e['event']=='processed']
    variants = {}
    for event in processed:
        variants.setdefault(f"{event['width']}x{event['height']}", set()).add(event['sha256'])
    rebuild_events = [e for e in events if e['event'] in ('rebuilt', 'parameter-update')]
    feature_window = (select(rebuild_events[3]['wall'], processed[-1]['wall']+result['arguments']['settle_seconds'])
                      if len(rebuild_events)>4 and processed else [])
    return dict(name=work.name, result=result, completed_cycles=len(processed),
        total_frames=sum(e['frames'] for e in processed),
        output_hashes_by_geometry={k:sorted(v) for k,v in variants.items()},
        idle_windows=idle, combined_hold_private_commit=combined, closes=closes,
        feature_observation=dict(first_three_cycles_excluded=True,
            private_commit=trend(feature_window, pid),
            dedicated_gpu=trend(feature_window, pid, 'dedicated_bytes', gpu=True),
            committed_gpu=trend(feature_window, pid, 'gpu_committed_bytes', gpu=True),
            shared_gpu=trend(feature_window, pid, 'shared_bytes', gpu=True)) if feature_window else None,
        interpretation='Finite synthetic observations. System commit includes background activity. '
            'An absent WDDM PID counter is not by itself proof that all driver allocations returned. '
            'Processing/exit and memory plateau are separate results.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    records = [summarize(path.parent) for path in sorted(args.task.glob('*/result.json'))]
    args.output.write_text(json.dumps(records, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    for row in records:
        print(json.dumps(dict(name=row['name'], processing_and_exit_passed=row['result']['processing_and_exit_passed'],
            frames=row['total_frames'], combined_hold=row['combined_hold_private_commit'],
            closes=row['closes'], features=row['feature_observation']), ensure_ascii=False))


if __name__ == '__main__':
    main()
