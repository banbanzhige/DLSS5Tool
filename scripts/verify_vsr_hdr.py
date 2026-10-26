"""Verify HDR conversion/analysis/export evidence and prepare local promotion."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    from scripts.build_vsr_cuda import safe,digest
    from scripts.verify_resident_sr import decoded_md5,timeline
    from dlss5tool.video_export import find_ffmpeg
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task',type=Path,required=True)
    parser.add_argument('--label',default='verification-summary')
    args=parser.parse_args();task=safe(args.task,ROOT/'tmp')
    if not args.label.replace('-','').isalnum():raise ValueError('Safe report label required')
    proof=task/(args.label+'.json')
    if not (task/'TASK.md').is_file() or proof.exists():raise ValueError('Fresh registered evidence required')
    read=lambda name:json.loads((task/(name+'.json')).read_text(encoding='utf-8-sig'))
    build=read('build-final');operators=read('operators-final');analysis=read('analysis-lut')
    assert operators['accepted'] and analysis['accepted']
    assert operators['library_sha256']==digest(task/'build/hdr_operators.dll')==build['outputs']['hdr_operators']
    assert analysis['candidate_sha256']==digest(ROOT/'dlss5tool/guidance_color.py')
    assert read('operators-final-process')['after_retirement']['present'] is False
    for name,checksum in build['source_files'].items():assert digest(ROOT/name)==checksum,name
    native=[]
    for control,candidate,mode in [('cpu-hdr4k','gpu-hdr4k-final','gpu-rgb10a2'),
                                   ('cpu-sdr','gpu-sdr','direct-rgba8'),
                                   ('cpu-hdr-small','gpu-hdr-small','cpu-rgb10a2')]:
        a,b=read(control),read(candidate)
        assert a['frame_hashes']==b['frame_hashes'] and len(a['frame_hashes'])==a['frames']==b['frames']
        assert b['actual_adapter']['hdr_conversion']==mode
        for label in (control,candidate):
            process=read(label+'-process')
            assert process['returncode']==0 and not process['worker_still_alive']
            assert process['pool_after_retirement']['reserved_processing_bytes']==0
        native.append(dict(control=control,candidate=candidate,frames=b['frames'],mode=mode,
            old_median_ms=a['warm_median_ms'],new_median_ms=b['warm_median_ms']))
    exports=[]
    for control,candidate,trc in [('cpu-pq4k-sr','gpu-pq4k-lut-sr',16),('cpu-hlg4k-sr','gpu-hlg4k-lut-sr',18)]:
        a,b=read(control),read(candidate);result=b['result']
        count=a['result']['real_frames']
        assert a['status']==b['status']=='complete'
        assert a['source_sha256']==b['source_sha256']
        assert len(a['frame_hashes'])==count==len(b['frame_hashes'])
        assert a['frame_hashes']==b['frame_hashes']
        assert a['result']['scene_cuts']==result['scene_cuts']
        assert result['super_resolution_adapter']['hdr_conversion']=='gpu-rgb10a2'
        assert not a['worker_processes_still_alive'] and not b['worker_processes_still_alive']
        assert b['memory_pool_after_close']['reserved_processing_bytes']==0
        before=decoded_md5(find_ffmpeg(),task/(control+'.mp4'))
        after=decoded_md5(find_ffmpeg(),task/(candidate+'.mp4'))
        assert before==after
        old_timeline=timeline(task/(control+'.mp4'),count,[3840,2160],result['output_rate'])
        new_timeline=timeline(task/(candidate+'.mp4'),count,[3840,2160],result['output_rate'])
        assert old_timeline==new_timeline
        import av
        with av.open(str(task/(candidate+'.mp4'))) as container:
            context=container.streams.video[0].codec_context
            assert context.color_trc==trc and context.color_primaries==9 and context.colorspace==9 and context.color_range==1
        exports.append(dict(control=control,candidate=candidate,frames=count,pixels_equal=True,
            decoded_md5=after,timeline=new_timeline,old_seconds=a['elapsed_seconds'],new_seconds=b['elapsed_seconds'],
            time_reduction=1-b['elapsed_seconds']/a['elapsed_seconds']))
    cancel=read('gpu-pq-cancel-sr')
    assert cancel['status']=='cancelled' and not cancel['output_exists']
    assert not cancel['transaction_files_remaining'] and not cancel['worker_processes_still_alive']
    assert cancel['memory_pool_after_close']['reserved_processing_bytes']==0
    cancel_seconds=cancel['elapsed_seconds']-cancel['cancel_requested_seconds']
    assert cancel_seconds<8
    passed=set()
    for case in ET.parse(task/'targeted-core.xml').getroot().iter('testcase'):
        assert case.find('failure') is None and case.find('error') is None and case.find('skipped') is None
        passed.add((case.get('classname'),case.get('name')))
    source_names=['dlss5tool/guidance_color.py','dlss5tool/super_resolution.py','native/vsr_host/vsr_host.cpp',
        'native/vsr_host/hdr_gpu_conversion.h','native/vsr_host/hdr_operator_probe.cpp','native/vsr_host/build.bat',
        'scripts/build_vsr_hdr.py','scripts/probe_vsr_hdr.py','scripts/probe_hdr_analysis.py','scripts/verify_vsr_hdr.py',
        'scripts/install_vsr_hdr_upgrade.ps1','scripts/profile_cached_export.py','scripts/probe_cuda_vsr.py','tests/test_vsr_hdr.py']
    report=dict(status='verified',base_commit=build['base_commit'],source_manifest={name:digest(ROOT/name) for name in source_names},
        native=native,exports=exports,operator_cases=operators['cases'],analysis_cases=analysis['cases'],
        tests=len(passed),cancel_seconds=cancel_seconds,previous_host_sha256=build['previous_host_sha256'],
        candidate_host_sha256=digest(task/'build/vsr_host.dll'),provider_sha256=build['provider_sha256'])
    assert report['candidate_host_sha256']==build['outputs']['vsr_host']
    proof.write_text(json.dumps(report,indent=2),encoding='utf-8')
    plan=dict(task=str(task),proof=str(proof),proof_sha256=digest(proof),candidate=str(task/'build/vsr_host.dll'),
        target=str(ROOT/'runtime/vsr_host.dll'),previous_sha256=build['previous_host_sha256'],
        candidate_sha256=report['candidate_host_sha256'],provider_sha256=build['provider_sha256'],
        rollback=str(task/'rollback/vsr_host.dll'))
    (task/'install-plan.json').write_text(json.dumps(plan,indent=2),encoding='utf-8')
    print(json.dumps(dict(status=report['status'],tests=report['tests'],cancel_seconds=cancel_seconds,
        exports=[{key:row[key] for key in ('frames','old_seconds','new_seconds','time_reduction')} for row in exports])))


if __name__=='__main__':main()
