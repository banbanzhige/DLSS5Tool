"""Install one accepted additive S4 component set, preserving the verified S3 pair."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    from scripts.build_vsr_cuda import safe,digest
    from scripts.s4_candidate import components_for
    from dlss5tool.resident_hdr_runtime import FILES,load_components
    from dlss5tool.resident_sr_runtime import load_components as load_s3
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task',required=True,type=Path)
    args=parser.parse_args()
    task=safe(args.task,ROOT/'tmp')
    evidence=safe(task/'s4-verification.json',task)
    record=json.loads(evidence.read_text(encoding='utf-8'))
    if not (task/'TASK.md').is_file() or not record['accepted'] or load_s3() is None:
        raise ValueError('Accepted evidence and verified S3 dependencies required')
    for relative,checksum in record['source_sha256'].items():
        if digest(safe(ROOT/relative,ROOT))!=checksum:
            raise ValueError('Source changed since acceptance: '+relative)
    components=components_for(task)
    root=safe(ROOT/'runtime/resident-hdr',ROOT/'runtime')
    stage=safe(task/'runtime-staging',task)
    if root.exists() or stage.exists():raise FileExistsError('Existing component set retained')
    if shutil.disk_usage(root.parent).free<15*1024**3+16*1024**2:
        raise ValueError('15 GiB reserve required')
    manifest=dict(schema=1,enabled=True,formats=['hdr10_pq','hdr10_hlg'],quality=4,
        geometry=[[960,540,2],[1920,1080,2]],verified_devices=[[record['adapter']['vendor_id'],record['adapter']['device_id']]],
        pyav=record['pyav'],av_libraries=record['av_libraries'],numpy=record['analysis_cpu']['numpy'],
        opencv=record['analysis_cpu']['opencv'],analysis_cpu=record['analysis_cpu'],
        cuda_vsr_manifest=digest(ROOT/'runtime/vsr-cuda/manifest.json'),
        encoder_manifest=digest(ROOT/'runtime/gpu-export/manifest.json'),
        nv12_sha256=digest(ROOT/'runtime/resident-sr/nv12-rgba.ptx'),
        files={},evidence_sha256=digest(evidence),packaging_license_review_complete=False,
        acceptance='PQ/HLG NVDEC 960x540 or 1920x1080 2x; neutral NR, FG1..4, bounded preview/cache; streaming >=96 frames; exact accepted pixel/timeline contracts')
    stage.mkdir()
    for field,filename in FILES.items():
        source=safe(components[field],task)
        checksum=digest(source)
        if checksum!=record['component_sha256'][field]:raise ValueError('Component changed: '+field)
        target=safe(stage/filename,stage)
        with source.open('rb') as incoming,target.open('xb') as outgoing:shutil.copyfileobj(incoming,outgoing)
        if digest(target)!=checksum:raise ValueError('Copy failed verification: '+filename)
        manifest['files'][filename]=checksum
    with (stage/'manifest.json').open('x',encoding='utf-8') as output:json.dump(manifest,output,indent=2)
    assert load_components(stage) is not None
    # Same-volume no-overwrite rename publishes only the fully verified set.
    safe(stage,task);safe(root,ROOT/'runtime')
    os.rename(stage,root)
    assert load_components() is not None and load_s3() is not None
    proof=dict(installed=str(root),manifest_sha256=digest(root/'manifest.json'),files=manifest['files'],
        s3_pair_preserved=True,rollback='Set enabled=false in this additive manifest to select the existing routes')
    with (task/'s4-installation.json').open('x',encoding='utf-8') as stream:json.dump(proof,stream,indent=2)
    print(json.dumps(proof))


if __name__=='__main__':main()
