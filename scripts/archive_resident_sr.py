"""Verify and archive small S3 evidence; media cleanup is a separate explicit step."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task',required=True,type=Path)
    parser.add_argument('--destination',required=True,type=Path)
    args=parser.parse_args()
    from scripts.build_vsr_cuda import safe,digest
    from scripts.verify_resident_sr import decoded_md5,timeline
    from dlss5tool.video_export import find_ffmpeg
    from dlss5tool.resident_sr_runtime import load_components
    task=safe(args.task,ROOT/'tmp')
    destination=safe(args.destination,ROOT/'docs/development')
    if not (task/'TASK.md').is_file() or destination.exists():raise ValueError('Fresh archive and registered work required')
    read=lambda name:json.loads((task/(name+'.json')).read_text(encoding='utf-8'))
    summary=read('verification-summary')
    gui=read('gui-entry')
    assert summary['accepted'] and gui['accepted'] and load_components() is not None
    old,new=read('control-hevc-sr'),read('resident-hevc-audit')
    assert old['frame_hashes']==new['result']['frame_hashes'] and len(old['frame_hashes'])==96
    assert not new['worker_after_retirement']['present']
    old_md5=decoded_md5(find_ffmpeg(),task/'control-hevc-sr.mp4')
    new_md5=decoded_md5(find_ffmpeg(),task/'resident-hevc-audit.mp4')
    assert old_md5==new_md5
    summary['pairs'].append(dict(control='control-hevc-sr',candidate='resident-hevc-audit',
        input_pixels_equal=True,decoded_md5=new_md5,
        timeline=timeline(task/'resident-hevc-audit.mp4',96,[1920,1080],'30')))
    passed,seen=set(),set()
    for path in sorted(task.glob('targeted-*.xml')):
        for case in ET.parse(path).getroot().findall('.//testcase'):
            key=(case.get('classname'),case.get('name'))
            seen.add(key)
            if all(case.find(tag) is None for tag in ('failure','error','skipped')):passed.add(key)
    assert seen==passed
    summary.update(targeted_test_identities=len(passed),all_have_passing_evidence=True,
        gui_entry_accepted=True,gui_elapsed_seconds=gui['elapsed_seconds'],
        initial_audit_retirement_note='Initial 4K pixel oracle preceded confirm_retired; see implementation record. Later speed/long/cancel/GUI owners disappeared before publication.')
    final=task/'verification-final.json'
    with final.open('x',encoding='utf-8') as out:json.dump(summary,out,indent=2)
    destination.mkdir(parents=True)
    records=[]
    def copy(source,relative):
        source=safe(source)
        target=safe(destination/relative,destination)
        target.parent.mkdir(exist_ok=True,parents=True)
        with source.open('rb') as incoming,target.open('xb') as outgoing:outgoing.write(incoming.read())
        checksum=digest(source)
        assert checksum==digest(target)
        records.append(dict(path=relative.replace('\\','/'),bytes=target.stat().st_size,sha256=checksum))
    for path in sorted(task.rglob('*')):
        relative=path.relative_to(task)
        if any(part.startswith('pytest-') for part in relative.parts):continue
        if path.is_file() and path.suffix.lower() in ('.json','.jsonl','.csv','.xml','.log','.md','.ptx'):
            if path.stat().st_size>8*1024**2:raise ValueError('Unexpected large report: '+str(path))
            copy(path,str(relative))
    changed=subprocess.run(['git','diff','--name-only'],check=True,capture_output=True,text=True,cwd=ROOT).stdout.splitlines()
    added=subprocess.run(['git','ls-files','--others','--exclude-standard'],check=True,capture_output=True,text=True,cwd=ROOT).stdout.splitlines()
    source_files=[]
    for relative in sorted(set(changed+added)):
        if relative.endswith(('.py','.cu','.spec')):
            copy(ROOT/relative,'source/'+relative)
            source_files.append(relative)
    copy(ROOT/'runtime/resident-sr/manifest.json','installed-resident-manifest.json')
    state=dict(base_commit=subprocess.run(['git','rev-parse','HEAD'],check=True,capture_output=True,text=True,cwd=ROOT).stdout.strip(),
        source_files=source_files,tests=len(passed),new_development_docs_ignored=True,
        runtime_manifest_sha256=digest(ROOT/'runtime/resident-sr/manifest.json'))
    state_path=destination/'source-state.json'
    state_path.write_text(json.dumps(state,indent=2),encoding='utf-8')
    records.append(dict(path='source-state.json',bytes=state_path.stat().st_size,sha256=digest(state_path)))
    manifest=destination/'archive-manifest.json'
    manifest.write_text(json.dumps(dict(schema=1,files=records,total_bytes=sum(row['bytes'] for row in records)),indent=2),encoding='utf-8')
    for row in records:assert digest(destination/row['path'])==row['sha256']
    print(json.dumps(dict(destination=str(destination),files=len(records),bytes=sum(row['bytes'] for row in records),
        tests=len(passed),pixel_pairs=len(summary['pairs']))))


if __name__=='__main__':main()
