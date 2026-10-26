"""Verify and archive the bounded 2026-10-26 scene/cache upgrade evidence.

Uses registered synthetic sources and existing FFmpeg. Does not run inference,
download dependencies, remove inputs, or treat a short probe as a long-run test.
"""
import argparse
from collections import Counter
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def checked(path, parent=None):
    path = path.absolute()
    if path.resolve() != path or (parent is not None and not path.is_relative_to(parent)):
        raise ValueError(f'Unexpected path: {path}')
    for item in (path, *path.parents):
        if item.exists() and item.lstat().st_file_attributes & 0x400:
            raise ValueError(f'Reparse point: {item}')
    return path


def digest(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True, type=Path)
    parser.add_argument('--archive', type=Path)
    args = parser.parse_args()
    task = checked(args.task, ROOT/'tmp')
    if not (task/'TASK.md').is_file():
        raise ValueError('Registered task required')
    from dlss5tool.video_export import find_ffmpeg, find_ffprobe
    ffmpeg, ffprobe = find_ffmpeg(), find_ffprobe(find_ffmpeg())
    pairs = [
        ('1080p-sr2-r1', 'baseline1080-paired-control-sr', 'upgraded1080-atomic-r1-sr'),
        ('1080p-sr2-r2', 'baseline1080-r1-sr', 'upgraded1080-atomic-r2-sr'),
        ('4k-sr2-r1', 'baseline4k-paired-r1-sr', 'upgraded4k-atomic-r1-sr'),
        ('4k-sr2-r2', 'baseline4k-paired-r2-sr', 'upgraded4k-atomic-r2-sr'),
        ('4k-sr2-without-control-replay', 'baseline4k-final-r1-sr', 'upgraded4k-atomic-r1-sr'),
        ('4k-sr4', 'baseline4x4k-sr', 'upgraded4x4k-atomic-sr'),
        ('pq-sr2', 'baselinepq-vui-sr', 'upgradedpq-atomic-sr'),
        ('hlg-sr2', 'baselinehlg-vui-sr', 'upgradedhlg-atomic-sr'),
        ('sdr-cut16-nr-fg2', 'baseline-cut-nr-fg-sr-nr', 'upgraded-cut16-overlap-sr-nr'),
        ('sdr-cut64-nr-fg2', 'baseline-cut64-nr-fg-sr-nr', 'upgraded-cut64-overlap-sr-nr'),
    ]
    reports, outputs, sources = {}, {}, {}
    for _, control, candidate in pairs:
        for name in (control, candidate):
            if name in reports:
                continue
            report = load(task/f'{name}.json')
            assert report['status'] == 'complete', name
            assert not report['worker_processes_still_alive'], name
            pool = report['memory_pool_after_close']
            assert all(pool[key] == 0 for key in ('used_bytes', 'other_bytes',
                'reserved_processing_bytes', 'waiting_processing_bytes')), name
            expected = report['result']['real_frames']*report['settings']['frame_generation_multiplier']
            assert len(report['frame_hashes']) == expected, name
            reports[name] = report
            source = checked(Path(report['result']['source']), task)
            sources[source.name] = dict(sha256=digest(source), bytes=source.stat().st_size)
            if report.get('source_sha256'):
                assert report['source_sha256'] == sources[source.name]['sha256'], name
            path = checked(task/f'{name}.mp4', task)
            probe = subprocess.run([ffprobe, '-v', 'error', '-select_streams', 'v:0',
                '-count_frames', '-show_streams', '-show_frames', '-show_entries',
                'stream=codec_name,width,height,pix_fmt,r_frame_rate,avg_frame_rate,color_space,'
                'color_transfer,color_primaries,color_range,nb_read_frames:frame=best_effort_timestamp_time',
                '-of', 'json', str(path)], check=True, capture_output=True, text=True, timeout=30,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            data = json.loads(probe.stdout)
            stream, frames = data['streams'][0], data['frames']
            assert int(stream['nb_read_frames']) == len(frames) == expected, name
            assert (stream['width'], stream['height']) == (report['result']['width'], report['result']['height']), name
            rate = Fraction(report['result']['output_rate'])
            assert Fraction(stream['avg_frame_rate']) == rate, name
            timestamps = [Fraction(frame['best_effort_timestamp_time']) for frame in frames]
            assert all(abs(value-Fraction(index, 1)/rate) <= Fraction(1, 100000)
                       for index, value in enumerate(timestamps)), name
            decoded = subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-threads', '2',
                '-i', str(path), '-map', '0:v:0', '-an', '-f', 'framemd5', '-'],
                check=True, capture_output=True, text=True, timeout=45,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            frame_md5 = [line.rsplit(',', 1)[-1].strip() for line in decoded.stdout.splitlines()
                         if line.strip() and not line.startswith('#')]
            assert len(frame_md5) == expected, name
            if report['color_profile'] != 'srgb':
                transfer = 'smpte2084' if report['color_profile'] == 'hdr10_pq' else 'arib-std-b67'
                assert stream['color_transfer'] == transfer and stream['color_primaries'] == 'bt2020', name
                assert stream['color_space'] == 'bt2020nc' and '10' in stream['pix_fmt'], name
            outputs[name] = dict(sha256=digest(path), bytes=path.stat().st_size,
                stream=stream, timestamps_seconds=[float(value) for value in timestamps],
                decoded_frame_md5=frame_md5)
    comparisons = []
    for case, control, candidate in pairs:
        before, after = reports[control], reports[candidate]
        assert before['baseline_commit'] and not after['baseline_commit'], case
        assert before['settings'] == after['settings'], case
        if 'source_sha256' in before and 'source_sha256' in after:
            assert before['source_sha256'] == after['source_sha256'], case
        assert not after.get('replay_requests'), case
        assert after['stages']['vsr']['calls'] == after['result']['real_frames'], case
        encoded_equal = before['frame_hashes'] == after['frame_hashes']
        decoded_equal = outputs[control]['decoded_frame_md5'] == outputs[candidate]['decoded_frame_md5']
        if before['case'] == 'sr':
            assert encoded_equal and decoded_equal, case
        else:
            before_nr = [(item['input'], item['reset']) for item in before['nr_hashes']]
            after_nr = [(item['input'], item['reset']) for item in after['nr_hashes']]
            assert before_nr == after_nr, case
            half = after['result']['real_frames']//2
            assert [index for index, (_, reset) in enumerate(after_nr) if reset] == [0, half], case
            for report in (before, after):
                assert [index for index, item in enumerate(report['fg_events']) if item['reset']] == [0, half], case
                hashes = report['frame_hashes']
                assert hashes[2*half-1] == hashes[2*half-2], case
            assert after['result']['scene_cuts'] == [half], case
        comparisons.append(dict(case=case, control=control, candidate=candidate,
            encoded_input_frames_equal=encoded_equal, final_decoded_frames_equal=decoded_equal,
            control_seconds=before['elapsed_seconds'], candidate_seconds=after['elapsed_seconds'],
            control_fps=before['steady_output_fps'], candidate_fps=after['steady_output_fps'],
            note=('NR outputs may differ across runs; input/reset and cut hold verified'
                  if before['case'] != 'sr' else 'All encoder input and decoded frames identical')))
    passes, failures, tests = set(), set(), []
    xml_paths = sorted(task.glob('*.xml'), key=lambda path: path.stat().st_mtime_ns)
    for path in xml_paths:
        counts = Counter()
        for case in ET.parse(path).iter('testcase'):
            identity = case.get('classname', '')+'::'+case.get('name', '')
            outcome = ('failed' if case.find('failure') is not None or case.find('error') is not None
                       else 'skipped' if case.find('skipped') is not None else 'passed')
            counts[outcome] += 1
            if outcome == 'passed':
                passes.add(identity)
            elif outcome == 'failed':
                failures.add(identity)
        tests.append(dict(file=path.name, outcomes=dict(counts)))
    assert not failures-passes, sorted(failures-passes)
    # Match the current collection so renamed/expanded parameter sets from
    # intermediate iterations are not counted as extra completed tests.
    modules = sorted({identity.split('::')[0].split('.')[1] for identity in passes|failures})
    collection = subprocess.run([sys.executable, '-B', '-m', 'pytest', '--collect-only', '-q',
        '-p', 'no:cacheprovider', *(f'tests/{module}.py' for module in modules)], cwd=ROOT,
        check=True, capture_output=True, text=True)
    current = set()
    for line in collection.stdout.splitlines():
        if not line.startswith('tests/') or '::' not in line:
            continue
        parts = line.split('::')
        classname = parts[0][:-3].replace('/', '.')
        if len(parts) > 2:
            classname += '.'+'.'.join(parts[1:-1])
        current.add(classname+'::'+parts[-1])
    assert current and not current-passes, sorted(current-passes)
    baseline = subprocess.run(['git', 'rev-parse', '--verify', 'f6a1808^{commit}'], cwd=ROOT,
                              check=True, capture_output=True, text=True).stdout.strip()
    source_paths = [ROOT/name for name in ('dlss5tool/frame_generation.py', 'dlss5tool/guidance_color.py',
        'dlss5tool/render_cache.py', 'tests/test_scene_detection.py', 'tests/test_render_cache.py',
        'scripts/profile_cached_export.py', 'scripts/profile_sr_efficiency.py',
        'scripts/verify_sr_scene_upgrade.py')]
    source_paths.extend(ROOT/'runtime'/name for name in ('vsr_host.dll', 'nvngx_vsr.dll',
        'dlssnr_host_v2.dll', 'nvngx_dlssnr.dll', 'dlssg_video_worker.exe', 'nvngx_dlssg.dll'))
    source_paths.extend([ROOT/'runtime/gpu-export/manifest.json', ROOT/'requirements.txt'])
    summary = dict(status='verified', baseline_commit=baseline, scope='S0/S1 bounded local verification',
        source_manifest={str(path.relative_to(ROOT)).replace('\\', '/'): digest(path)
                         for path in source_paths if path.is_file()}, ffmpeg=str(ffmpeg), ffmpeg_sha256=digest(Path(ffmpeg)),
        tests=dict(passing_identities=len(current), current_collection=sorted(current),
                   intermediate_identities_excluded=sorted(passes-current),
                   unresolved_failed_identities=sorted(failures-passes),
                   recovered_failed_identities=sorted(failures & passes), runs=tests),
        comparisons=comparisons, sources=sources, outputs=outputs,
        caveats=['Other GPU applications remained active; not an isolated GPU benchmark',
                 '16-frame HDR and cut cases validate contracts, not sustained throughput',
                 'No long-run native memory growth, multi-GPU, SDK upgrade or release-package acceptance',
                 'Initial PQ/HLG probes without -vui source suffix were SDR due missing source VUI; excluded',
                 'baseline1080-paired-r1 is a mislabeled candidate; baseline_commit=null verifies its identity'])
    destination = checked(task/'verification-summary.json', task)
    destination.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.archive:
        archive = checked(args.archive, ROOT/'docs/development')
        archive.mkdir(parents=True, exist_ok=True)
        files = [*task.glob('*.json'), *xml_paths, task/'TASK.md',
                 *(path for path in (task/'native-logs').rglob('*') if path.is_file())]
        manifest = {}
        for source in files:
            checked(source, task)
            relative = source.relative_to(task)
            target = checked(archive/relative, archive)
            target.parent.mkdir(parents=True, exist_ok=True)
            sha256 = digest(source)
            if target.exists() and digest(target) != sha256:
                raise ValueError(f'Archive exists with different content: {target}')
            if not target.exists():
                shutil.copy2(source, target)
            assert digest(target) == sha256
            manifest[relative.as_posix()] = dict(sha256=sha256, bytes=target.stat().st_size)
        (archive/'archive-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps(dict(status=summary['status'], pairs=len(comparisons),
        passing_identities=len(current), unresolved_failures=len(failures-passes)), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
