"""Retain small verifiable evidence from a registered RAM coordination task."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def safe(path,root):
    path=path.absolute()
    if path.resolve()!=path or not path.is_relative_to(root):raise ValueError('Path outside intended root')
    for item in (path,*path.parents):
        if item.exists() and item.lstat().st_file_attributes&0x400:raise ValueError('Reparse point')
    return path


def digest(path):
    with path.open('rb') as source:return hashlib.file_digest(source,'sha256').hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task',type=Path,required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    args=parser.parse_args()
    from scripts.profile_memory_headroom import validate,save
    task=validate(args.task)
    evidence=safe(args.evidence,ROOT/'docs'/'development')
    evidence.mkdir(exist_ok=True)
    names=['final-associated.xml','final-coordination.xml','tk-rerun.xml','lifecycle-gates.xml',
           'model-cache-tests.txt','final-policy-policy-evaluation.json','coordinated-cache-sr-nr.json',
           'encoder-memory.json']
    summaries=sorted(p for prefix in ('before-calibration','calibrated-runtime','final-fg')
                     for p in task.glob(prefix+'-*.summary.json'))
    names.extend(p.name for p in summaries)
    manifest=[]
    for name in names:
        source=safe(task/name,task);destination=safe(evidence/name,evidence)
        if not source.is_file():raise FileNotFoundError(source)
        if destination.exists() and digest(destination)!=digest(source):raise ValueError('Refusing replacement')
        if not destination.exists():shutil.copyfile(source,destination)
        assert digest(source)==digest(destination)
        manifest.append(dict(file=name,bytes=source.stat().st_size,sha256=digest(source)))
    cases={}
    for name in ('final-associated.xml','final-coordination.xml','tk-rerun.xml','lifecycle-gates.xml'):
        for item in ET.parse(task/name).iter('testcase'):
            status=('failed' if item.find('failure') is not None or item.find('error') is not None else
                    'skipped' if item.find('skipped') is not None else 'passed')
            cases[(item.get('classname'),item.get('name'))]=dict(status=status,evidence=name)
    model_text=(task/'model-cache-tests.txt').read_text(encoding='utf-8-sig')
    assert 'Ran 2 tests' in model_text and '\nOK' in model_text
    for name in ('test_cut_inactive_mode_and_failure','test_replay_seek_partial_hit_and_eviction'):
        cases[('tests.test_guidance_cache.ModelCacheTests',name)]=dict(status='passed',evidence='model-cache-tests.txt')
    unresolved=[dict(classname=k[0],name=k[1],**v) for k,v in cases.items() if v['status']!='passed']
    assert not unresolved,unresolved
    peaks=[];comparisons=[]
    for source in summaries:
        data=json.loads(source.read_text(encoding='utf-8'))
        sample_path=source.with_name(source.name.replace('.summary.json','.samples.json'))
        samples=json.loads(sample_path.read_text(encoding='utf-8'))
        retained=[]
        for phase in sorted({s['phase'] for s in samples}):
            retained.append(max((s for s in samples if s['phase']==phase),key=lambda s:s['private_bytes']))
        peaks.append(dict(run=source.name,source_samples_sha256=digest(sample_path),
                          full_samples_count=len(samples),phase_peak_samples=retained))
        if source.name.startswith(('calibrated-runtime','final-fg')):
            old=json.loads((task/f'before-calibration-{data["case"]}.summary.json').read_text(encoding='utf-8'))
            comparisons.append(dict(run=source.name,case=data['case'],frames=data['frames'],
                                    hashes_equal=data['hashes']==old['hashes']))
    assert all(row['hashes_equal'] for row in comparisons)
    save(evidence/'phase-peak-samples.json',peaks)
    sources=['dlss5tool/'+name+'.py' for name in ('memory_budget','shared_cache_budget','guidance_cache',
        'render_cache','dlss_host_process','super_resolution','gui','frame_generation','export_preflight',
        'gpu_decode','gpu_export_process','video_export')]
    sources+=['runtime/'+name for name in ('vsr_host.dll','nvngx_vsr.dll','dlssnr_host_v2.dll','nvngx_dlssnr.dll',
        'dlssg_video_worker.exe','gpu-export/encoder.dll')]
    sources+=['mods/enhancement/guidance_worker.exe','scripts/profile_memory_headroom.py',
              'scripts/verify_memory_export.py','scripts/profile_cached_export.py']
    source_manifest=[dict(file=name,sha256=digest(ROOT/name)) for name in sources]
    report=dict(distinct_tests_passed=len(cases),unresolved_tests=unresolved,frame_comparisons=comparisons,
        copied_evidence=manifest,source_manifest=source_manifest,
        retention='Summaries and phase peak rows retained; full 50 ms streams and synthetic fixtures are rebuildable.',
        tests=[dict(classname=k[0],name=k[1],**v) for k,v in sorted(cases.items())])
    save(evidence/'verification-summary.json',report)
    print(json.dumps(dict(distinct_tests_passed=len(cases),evidence_files=len(manifest)+2,
        evidence_bytes=sum(p.stat().st_size for p in evidence.iterdir() if p.is_file()),
        frame_comparisons=len(comparisons))),flush=True)


if __name__=='__main__':main()
