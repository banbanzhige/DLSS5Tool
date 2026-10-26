"""Bind S4 pixel, timeline/audio, ownership and performance evidence to current components."""
import argparse
import ast
from fractions import Fraction
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    from scripts.build_vsr_cuda import safe,digest
    from scripts.s4_candidate import components_for
    from scripts.verify_resident_sr import decoded_md5,timeline
    from dlss5tool.video_export import find_ffmpeg
    from dlss5tool.analysis_quantization import cpu_fingerprint
    import av
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task',required=True,type=Path)
    args=parser.parse_args()
    task=safe(args.task,ROOT/'tmp')
    target=task/'s4-verification.json'
    if not (task/'TASK.md').is_file() or target.exists():raise ValueError('Fresh registered evidence required')
    def read(name):return json.loads((task/(name+'.json')).read_text(encoding='utf-8'))
    def hashes(data):return data.get('frame_hashes') or data['result']['frame_hashes']
    report=dict(schema=1,accepted=False,pyav=av.__version__,av_libraries=av.library_versions,
                analysis_cpu=cpu_fingerprint(),pairs=[],performance=[])
    pairs=[('control-pq-sr','resident-pq1080'),
           ('control-pq-nr-sr-nr','resident-pq-nr-analysis'),
           ('control-pq-fg-sr','resident-pq-fg-analysis'),
           ('control-sdr-nr-fg-sr-nr','resident-sdr-nr-fg-analysis'),
           ('control-hlg-nr-fg-bounded-sr-nr','device-hlg-nr-fg-final-sr-nr'),
           ('control-hlg-fg3-sr','resident-hlg-fg3'),
           ('control-hlg-fg4-sr','resident-hlg-fg4'),
           ('control-pq-cut-fg2-sr','resident-pq-cut-fg2')]
    for control,candidate in pairs:
        old,new=read(control),read(candidate)
        result=new['result']
        assert old['status']==new['status']=='complete', (control,candidate)
        assert old['source_sha256']==new['source_sha256']
        assert hashes(old)==hashes(new) and hashes(new), (control,candidate,'pixels')
        count=len(hashes(new))
        rate=result['output_rate']
        size=[result['width'],result['height']]
        before=decoded_md5(find_ffmpeg(),task/(control+'.mp4'))
        after=decoded_md5(find_ffmpeg(),task/(candidate+'.mp4'))
        assert before==after, (control,candidate,'encoded')
        info=timeline(task/(candidate+'.mp4'),count,size,rate)
        assert info==timeline(task/(control+'.mp4'),count,size,rate)
        if info['audio_packets']:
            meta=old['result']['source_metadata']
            source_info=timeline(Path(old['source']),result['real_frames'],[meta['width'],meta['height']],result.get('source_rate','30000/1001'))
            for key in ('audio_packets','audio_sha256','audio_pts_sha256'):assert source_info[key]==info[key]
        report['pairs'].append(dict(control=control,candidate=candidate,frames=count,
            exact_input_pixels=True,decoded_md5=after,timeline=info))
    oracle,resident=read('oracle-pq4k-nr-fg2'),read('resident-pq4k-nr-fg2')
    assert oracle['accepted'] and resident['status']=='complete'
    assert oracle['source_sha256']==resident['source_sha256'] and oracle['frame_hashes']==hashes(resident)
    report['serial_4k_oracle']=dict(exact_input_pixels=True,frames=len(oracle['frame_hashes']),
        scope=oracle['scope'],concurrent_legacy_preflight=read('control-pq4k-nr-fg2-sr-nr')['error'])
    assert read('d3d-hdr4x')['frame_hashes']==read('cuda-hdr4x')['frame_hashes']
    report['hdr_4x_upload_pixels_equal']=True
    for control,candidate in [('control-pq4k-32-sr','resident-pq4k-32'),
                              ('control-pq-96-sr','resident-pq-96'),
                              ('control-sdr-nr-fg96-sr-nr','resident-sdr-nr-fg96')]:
        old,new=read(control),read(candidate);result=new['result']
        assert old['status']==new['status']=='complete'
        assert old['source_sha256']==new['source_sha256']
        before=decoded_md5(find_ffmpeg(),task/(control+'.mp4'))
        after=decoded_md5(find_ffmpeg(),task/(candidate+'.mp4'))
        assert before==after
        count=result['real_frames']*(2 if 'fg96' in candidate else 1)
        info=timeline(task/(candidate+'.mp4'),count,[result['width'],result['height']],result['output_rate'])
        assert info==timeline(task/(control+'.mp4'),count,[result['width'],result['height']],result['output_rate'])
        assert new['elapsed_seconds']<old['elapsed_seconds']
        report['performance'].append(dict(control=control,candidate=candidate,frames=count,
            control_seconds=old['elapsed_seconds'],candidate_seconds=new['elapsed_seconds'],
            time_reduction=1-new['elapsed_seconds']/old['elapsed_seconds'],
            throughput_ratio=old['elapsed_seconds']/new['elapsed_seconds'],decoded_md5=after,timeline=info))
    for name in ('cancel-hdr-fg','cancel-sdr-fg'):
        data=read(name)
        assert data['status']=='cancelled' and not data['output_exists']
        assert not data['worker_after_retirement']['present'] and not data['transaction_directories_remaining']
        assert data['pool_after_retirement']['reserved_processing_bytes']==0
        seconds=data['elapsed_seconds']-data['cancel_requested_seconds']
        assert seconds<5
        report[name]=dict(seconds_after_request=seconds,worker_retired=True,published_files=0)
    cache=read('cache-contract-v4')
    assert cache['accepted'] and not cache['pool_after_close']['used_bytes']
    report['cache_contract']=cache
    preview=read('preview-demand')
    assert preview['status']=='complete' and all(row['reference_equal'] for row in preview['frames'])
    assert preview['skipped']==list(range(80))
    assert [row['sequence'] for row in preview['frames']]==list(range(80,192))
    assert preview['result']['preview_readbacks']==112 and preview['result']['skipped_preview_readbacks']==80
    assert preview['result']['max_pending_frames']==1 and all(not row['present'] for row in preview['owners'])
    assert read('preview-cancel')['status']=='cancelled'
    assert all(not row['present'] for row in read('preview-cancel')['owners'])
    report['preview_demand']=dict(readbacks=112,skipped=80,max_pending=1,references_equal=True)
    analysis=read('analysis-pinned')
    assert analysis['accepted'] and analysis['analysis_cpu']==cpu_fingerprint()
    components=components_for(task)
    assert analysis['tables_sha256']==digest(Path(components['analysis_tables']))
    assert analysis['ptx_sha256']==digest(Path(components['analysis_ptx']))
    report['analysis']=analysis
    for name,source in [('vsr-hdr-v2-build','scripts/gpu_vsr_hdr.cu'),
                        ('p010-rgba-v2-build','scripts/gpu_p010_rgba.cu'),
                        ('analysis-v2-build','scripts/gpu_analysis.cu')]:
        assert read(name)['source_sha256']==digest(ROOT/source)
    assert read('build')['source_sha256']==digest(ROOT/'native/vsr_cuda/vsr_cuda.cpp')
    for relative,checksum in read('consumer-build-final')['sources'].items():assert digest(ROOT/relative)==checksum
    report['component_sha256']={field:digest(Path(path)) for field,path in components.items()
        if field in components['component_hashes']}
    seen,passed=set(),set()
    for name in ('cached-gpu-tests.xml','consumer-final-tests.xml','color-gui-tests.xml','tk-recheck.xml','runtime-tests.xml'):
        for case in ET.parse(task/name).getroot().findall('.//testcase'):
            identity=(case.get('classname'),case.get('name'))
            seen.add(identity)
            if all(case.find(tag) is None for tag in ('failure','error','skipped')):passed.add(identity)
    assert seen==passed
    report['targeted_test_identities']=len(passed)
    report['all_tests_have_passing_evidence']=True
    source_files=[]
    for command in (['git','diff','--name-only'],['git','ls-files','--others','--exclude-standard']):
        source_files+=subprocess.run(command,cwd=ROOT,check=True,capture_output=True,text=True).stdout.splitlines()
    source_files=sorted({name for name in source_files if name.startswith(('dlss5tool/','scripts/','native/','tests/','packaging/'))
                         and name.endswith(('.py','.cpp','.h','.cu','.bat','.spec'))})
    for name in source_files:
        if name.endswith(('.py','.spec')):ast.parse((ROOT/name).read_text(encoding='utf-8-sig'),filename=name)
    subprocess.run(['git','diff','--check'],cwd=ROOT,check=True,capture_output=True)
    report['source_sha256']={name:digest(ROOT/name) for name in source_files}
    report['adapter']=read('resident-pq4k-32')['result']['adapter']
    report['accepted']=True
    target.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(dict(accepted=True,pixel_pairs=len(pairs)+1,tests=len(passed),performance=report['performance'])))


if __name__=='__main__':main()
