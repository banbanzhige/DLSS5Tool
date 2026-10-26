"""Verify S2 native/frame evidence and prepare a reviewable local install plan.

Does not install, publish, delete or change the default SR backend. The plan is
applied by install_vsr_upgrade.ps1 after all comparisons and source hashes pass.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.build_vsr_cuda import safe, digest


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--prepare-install', action='store_true')
    args = parser.parse_args()
    task = safe(args.task, ROOT/'tmp')
    if not (task/'TASK.md').is_file():
        raise ValueError('Registered task required')
    cases = []
    for control, candidate in [('d3d-small', 'cuda-small-bounded'), ('d3d1080', 'cuda1080'),
            ('d3d4k', 'cuda4k'), ('d3d4x', 'cuda4x'),
            ('d3d4k', 'd3d4k-selected'), ('old-hdr', 'selected-hdr')]:
        before, after = (read(task/f'{name}.json') for name in (control, candidate))
        assert before['frame_hashes'] == after['frame_hashes'], (control, candidate)
        assert len(before['frame_hashes']) == before['frames'], control
        assert len(after['frame_hashes']) == after['frames'] == before['frames'], candidate
        for name, report in ((control, before), (candidate, after)):
            process = read(task/f'{name}-process.json')
            assert not process['worker_still_alive'], name
            pool = process['pool_after_retirement']
            assert all(pool[key] == 0 for key in ('used_bytes', 'other_bytes',
                'reserved_processing_bytes', 'waiting_processing_bytes')), name
            assert report['status'] in ('complete', 'complete_with_forced_retirement'), name
        if after['backend'] == 'cuda':
            record = after['adapter']
            assert after['actual_luid'] == ((record['luid_high'] & 0xffffffff) << 32) | record['luid_low']
        if after.get('actual_adapter'):
            assert after['actual_adapter']['verified']
            assert after['actual_adapter']['id'] == after['adapter']['id']
        cases.append(dict(control=control, candidate=candidate, equal_frames=len(after['frame_hashes']),
            control_host_median_ms=before['warm_median_ms'], candidate_host_median_ms=after['warm_median_ms'],
            device_only_median_ms=after.get('device_only_median_ms'), native_cleanup=after.get('native_cleanup', 'normal')))
    missing = read(task/'missing-adapter.json')
    assert missing['status'] == 'rejected_missing_adapter'
    passed, failed, runs = set(), set(), []
    for path in sorted(task.glob('*.xml'), key=lambda p: p.stat().st_mtime_ns):
        count, failures = 0, 0
        for case in ET.parse(path).iter('testcase'):
            identity = case.get('classname')+'::'+case.get('name')
            count += 1
            if case.find('failure') is not None or case.find('error') is not None:
                failed.add(identity)
                failures += 1
            elif case.find('skipped') is None:
                passed.add(identity)
        runs.append(dict(file=path.name, cases=count, failures=failures))
    assert passed and not failed-passed, sorted(failed-passed)
    build = read(task/'build.json')
    assert digest(task/'build/vsr_cuda.dll') == build['output_sha256']
    assert digest(ROOT/'native/vsr_cuda/vsr_cuda.cpp') == build['source_sha256']
    native = task/'d3d-candidate/vsr_host.dll'
    baseline = read(task/'baseline.json')
    source_files = ['dlss5tool/cuda_session.py', 'dlss5tool/cuda_frame.py', 'dlss5tool/cuda_vsr.py',
        'dlss5tool/cuda_vsr_runtime.py', 'dlss5tool/super_resolution.py', 'dlss5tool/gui.py',
        'dlss5tool/frame_generation.py', 'native/vsr_host/vsr_host.cpp', 'native/vsr_host/build.bat',
        'native/vsr_cuda/vsr_cuda.cpp', 'scripts/build_vsr_cuda.py', 'scripts/probe_cuda_vsr.py',
        'scripts/verify_cuda_vsr.py', 'scripts/install_vsr_upgrade.ps1', 'scripts/archive_cuda_vsr.py',
        'tests/test_cuda_vsr.py', 'tests/test_scene_detection.py',
        'packaging/DLSS5Tool.spec']
    summary = dict(status='verified', comparisons=cases, missing_adapter_rejected=True,
        source_manifest={name: digest(ROOT/name) for name in source_files},
        candidate_d3d_sha256=digest(native), previous_d3d_sha256=baseline['files']['runtime/vsr_host.dll'],
        cuda_build=build, tests=dict(passed_identities=len(passed), recovered=sorted(failed & passed), runs=runs),
        default_route='D3D12 CPU-frame path retained; CUDA backend is an explicit completed-device-frame producer',
        limits=['CUDA SDR RGBA8, highest quality, 2x/4x <=4K tested',
            'NGX CUDA shutdown hangs on this machine; isolated worker forced retirement is required',
            'No end-to-end NVDEC-to-VSR-to-NVENC pipeline, HDR CUDA VSR or multi-GPU machine acceptance',
            'No release packaging/publication or long-run native memory plateau claim'])
    if (task/'installed.json').is_file():
        from dlss5tool.cuda_vsr_runtime import load_components
        from dlss5tool.video_export import find_ffmpeg, find_ffprobe
        components = load_components()
        assert components
        installed = read(task/'installed.json')
        assert digest(ROOT/'runtime/vsr_host.dll') == installed['host_sha256'] == digest(native)
        assert digest(Path(installed['rollback'])) == baseline['files']['runtime/vsr_host.dll']
        installed_cases = []
        for label in ('installed-d3d4k', 'installed-cuda4k'):
            result = read(task/f'{label}.json')
            assert result['frame_hashes'] == read(task/'d3d4k.json')['frame_hashes']
            process = read(task/f'{label}-process.json')
            assert not process['worker_still_alive'] and not process['pool_after_retirement']['used_bytes']
            installed_cases.append(dict(label=label, frames=len(result['frame_hashes']),
                                        status=result['status'], source=label+'.json'))
        cached = read(task/'s2-cached4k-final-sr.json')
        previous = read(ROOT/'docs/development/sr-scene-optimization-20261026/upgraded4k-atomic-r1-sr.json')
        assert cached['frame_hashes'] == previous['frame_hashes']
        assert cached['stages']['vsr']['calls'] == cached['result']['real_frames'] == 96
        assert not cached['replay_requests'] and not cached['worker_processes_still_alive']
        assert cached['result']['super_resolution_adapter']['verified']
        assert all(cached['memory_pool_after_close'][key] == 0 for key in (
            'used_bytes', 'other_bytes', 'reserved_processing_bytes', 'waiting_processing_bytes'))
        video = task/'s2-cached4k-final-sr.mp4'
        decoded = subprocess.run([find_ffmpeg(), '-hide_banner', '-loglevel', 'error', '-threads', '2',
            '-i', str(video), '-map', '0:v:0', '-an', '-f', 'framemd5', '-'], check=True,
            capture_output=True, text=True, timeout=45)
        frame_md5 = [line.rsplit(',', 1)[-1].strip() for line in decoded.stdout.splitlines()
                     if line.strip() and not line.startswith('#')]
        s1 = read(ROOT/'docs/development/sr-scene-optimization-20261026/verification-summary.json')
        assert frame_md5 == s1['outputs']['upgraded4k-atomic-r1-sr']['decoded_frame_md5']
        probe = subprocess.run([find_ffprobe(), '-v', 'error', '-select_streams', 'v:0',
            '-count_frames', '-show_streams', '-of', 'json', str(video)], check=True,
            capture_output=True, text=True, timeout=30)
        stream = json.loads(probe.stdout)['streams'][0]
        assert (stream['width'], stream['height'], int(stream['nb_read_frames']), stream['avg_frame_rate']) == (3840, 2160, 96, '30/1')
        summary['installed'] = dict(components=components, record=installed, comparisons=installed_cases)
        summary['cached_export'] = dict(encoder_input_equal_to_s1=True, decoded_equal_to_s1=True,
            frames=96, replay_requests=0, vsr_calls=96, seconds=cached['elapsed_seconds'],
            fps=cached['steady_output_fps'], stream=stream, output_sha256=digest(video), decoded_frame_md5=frame_md5)
    if (task/'resident-decode-ready.json').is_file():
        decoder = read(task/'resident-decode-ready.json')
        process = read(task/'resident-decode-ready-process.json')
        assert decoder['status'] == 'resident_decode_verified' and len(decoder['frames']) == 3
        assert all(plane['same_primary_context'] for frame in decoder['frames'] for plane in frame['planes'])
        assert not process['worker_still_alive']
        summary['resident_decode'] = dict(device_contract='verified on three frames',
            natural_exit='timeout; parent job reaped worker', source='resident-decode-ready.json',
            next_step='Reuse existing PyAV only after its lifecycle and NV12-to-RGBA contract pass S3')
    if (task/'retirement-observation.json').is_file():
        summary['os_retirement_observation'] = read(task/'retirement-observation.json')
    if (task/'cuda-final-owner.json').is_file():
        final = read(task/'cuda-final-owner.json')
        assert final['frame_hashes'] == read(task/'d3d-small.json')['frame_hashes']
        process = read(task/'cuda-final-owner-process.json')
        assert not process['worker_still_alive']
        assert all(process['pool_after_retirement'][key] == 0 for key in (
            'used_bytes', 'other_bytes', 'reserved_processing_bytes', 'waiting_processing_bytes'))
        summary['final_producer_completion'] = dict(frames=16, equal_to_d3d12=True,
            source='cuda-final-owner.json', native_cleanup=final.get('native_cleanup', 'normal'))
    (task/'verification-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.prepare_install:
        material = task/'cuda-runtime-12.8'
        files = [
            (task/'build/vsr_cuda.dll', ROOT/'runtime/vsr-cuda/vsr_cuda.dll'),
            (material/'nvidia/cuda_runtime/bin/cudart64_12.dll', ROOT/'runtime/vsr-cuda/cudart64_12.dll'),
            (material/'nvidia_cuda_runtime_cu12-12.8.90.dist-info/License.txt', ROOT/'runtime/vsr-cuda/licenses/CUDA-runtime-License.txt'),
            (ROOT/'third_party/RTX_Video_SDK/NVIDIA_RTX_Video_SDK_License.pdf', ROOT/'runtime/vsr-cuda/licenses/NVIDIA-RTX-Video-SDK-License.pdf'),
        ]
        manifest = dict(schema=1, enabled=True, abi=1, quality=4, formats=['rgba8-srgb'], scales=[2, 4],
            max_output_pixels=3840*2160, cuda_runtime_version='12.8.90',
            provider_sha256=digest(ROOT/'runtime/nvngx_vsr.dll'),
            files={destination.name: digest(source) for source, destination in files[:2]},
            packaging_license_review_complete=False, default_route=False,
            acceptance='S2 device-frame producer; isolated worker retirement required; CUDA HDR and full export pending')
        manifest_path = task/'cuda-runtime-manifest.json'
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        files.append((manifest_path, ROOT/'runtime/vsr-cuda/manifest.json'))
        plan = dict(task=str(task), proof_sha256=digest(task/'verification-summary.json'),
            previous_d3d_sha256=summary['previous_d3d_sha256'], candidate_d3d_sha256=digest(native),
            d3d_source=str(native), d3d_target=str(ROOT/'runtime/vsr_host.dll'),
            rollback=str(task/'rollback/vsr_host.dll'),
            files=[dict(source=str(safe(source)), destination=str(safe(destination, ROOT/'runtime/vsr-cuda')),
                        sha256=digest(source)) for source, destination in files])
        (task/'install-plan.json').write_text(json.dumps(plan, indent=2), encoding='utf-8')
    print(json.dumps(dict(status='verified', comparisons=len(cases), passing_identities=len(passed),
                         prepared_install=args.prepare_install)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
