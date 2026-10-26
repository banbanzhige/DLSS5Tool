"""Archive verified small S4 evidence and sources; temporary media cleanup is separate."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    from scripts.build_vsr_cuda import safe,digest
    from dlss5tool.resident_hdr_runtime import load_components
    from dlss5tool.resident_sr_runtime import load_components as load_s3
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task',required=True,type=Path)
    parser.add_argument('--destination',required=True,type=Path)
    args=parser.parse_args()
    task=safe(args.task,ROOT/'tmp')
    destination=safe(args.destination,ROOT/'docs/development')
    if not (task/'TASK.md').is_file() or destination.exists():raise ValueError('Fresh archive required')
    summary=json.loads((task/'s4-verification.json').read_text(encoding='utf-8'))
    assert summary['accepted'] and load_components() is not None and load_s3() is not None
    for name in ('gui-installed-pq','gui-installed-hlg-consumers','cache-installed'):
        assert json.loads((task/(name+'.json')).read_text(encoding='utf-8'))['accepted']
    for relative,checksum in summary['source_sha256'].items():assert digest(ROOT/relative)==checksum
    destination.mkdir(parents=True)
    records=[]
    def copy(source,relative):
        source=safe(source)
        target=safe(destination/relative,destination)
        target.parent.mkdir(parents=True,exist_ok=True)
        with source.open('rb') as incoming,target.open('xb') as outgoing:outgoing.write(incoming.read())
        assert digest(source)==digest(target)
        records.append(dict(path=target.relative_to(destination).as_posix(),bytes=target.stat().st_size,sha256=digest(target)))
    for directory,children,files in os.walk(task,followlinks=False):
        children[:]=[name for name in children if not name.startswith('pytest-') and name not in (
            'cuda-headers','build','consumer-build','runtime-staging','oracle-pq4k-nr-fg2-frames')]
        for name in children:safe(Path(directory)/name,task)
        for name in files:
            path=safe(Path(directory)/name,task)
            if path.suffix.lower() in ('.json','.jsonl','.xml','.log','.csv','.md','.ptx') or name=='analysis-tables.npz':
                if path.stat().st_size>8*1024**2:raise ValueError('Unexpected large report: '+str(path))
                copy(path,path.relative_to(task))
    changed=[]
    for command in (['git','diff','--name-only'],['git','ls-files','--others','--exclude-standard']):
        changed+=subprocess.run(command,cwd=ROOT,check=True,capture_output=True,text=True).stdout.splitlines()
    for name in sorted(set(changed)):
        if name.endswith(('.py','.cpp','.h','.cu','.spec','.bat')):copy(ROOT/name,Path('source')/name)
    for name in ('resident-hdr','resident-sr','vsr-cuda','gpu-export'):
        copy(ROOT/'runtime'/name/'manifest.json','installed-'+name+'-manifest.json')
    # Small original reference modules make later numerical investigations
    # possible without depending on the retention of temporary fixtures.
    for name in ('dlss5tool/guidance_color.py','dlss5tool/frame_generation.py','dlss5tool/render_cache.py'):
        data=subprocess.run(['git','show','22ee79c:'+name],cwd=ROOT,check=True,capture_output=True).stdout
        target=safe(destination/'oracle-s3'/name,destination)
        target.parent.mkdir(parents=True,exist_ok=True)
        with target.open('xb') as stream:stream.write(data)
        records.append(dict(path=target.relative_to(destination).as_posix(),bytes=len(data),sha256=digest(target)))
    state=dict(base_commit=subprocess.run(['git','rev-parse','HEAD'],cwd=ROOT,check=True,capture_output=True,text=True).stdout.strip(),
        accepted=True,tests=summary['targeted_test_identities'],source_committed=False,packaged=False,
        installed_manifest_sha256=digest(ROOT/'runtime/resident-hdr/manifest.json'),
        evidence_manifest_sha256=digest(task/'s4-verification.json'),
        new_development_docs_ignored=True,
        performance_scope='Comparison reports time the full export call; invocation reports additionally include the CLI startup and adapter discovery.',
        retained_boundaries=['unverified HDR geometry/4x NVDEC uses existing route','scRGB/sequence/composition retain existing contracts',
            'FG remains experimental with pinned provider','multi-GPU and hours-long stability belong to separate acceptance'])
    target=destination/'source-state.json'
    target.write_text(json.dumps(state,indent=2),encoding='utf-8')
    records.append(dict(path=target.name,bytes=target.stat().st_size,sha256=digest(target)))
    for row in records:assert digest(destination/row['path'])==row['sha256']
    manifest=dict(schema=1,files=records,total_bytes=sum(row['bytes'] for row in records),accepted=True)
    (destination/'archive-manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print(json.dumps(dict(destination=str(destination),files=len(records),bytes=manifest['total_bytes'],tests=state['tests'])))


if __name__=='__main__':main()
