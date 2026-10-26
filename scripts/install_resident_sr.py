"""Activate the locally verified optional S3 route; never replace an existing pair."""
import argparse
import json
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task',required=True,type=Path)
    args=parser.parse_args()
    from scripts.build_vsr_cuda import safe,digest
    from dlss5tool.cuda_vsr_runtime import load_components as load_vsr
    from dlss5tool.gpu_export_runtime import load_components as load_encoder
    from dlss5tool.resident_sr_runtime import load_components
    task=safe(args.task,ROOT/'tmp')
    evidence=safe(task/'verification-summary.json',task)
    report=json.loads(evidence.read_text(encoding='utf-8'))
    source=safe(task/'nv12-rgba.ptx',task)
    if (not (task/'TASK.md').is_file() or not report['accepted'] or
            digest(source)!=report['kernel_build']['ptx_sha256'] or
            digest(ROOT/'scripts/gpu_nv12_rgba.cu')!=report['kernel_build']['source_sha256']):
        raise ValueError('Current kernel and complete accepted evidence required')
    if not load_vsr() or not load_encoder():raise ValueError('Verified VSR/encoder dependencies required')
    root=safe(ROOT/'runtime/resident-sr',ROOT/'runtime')
    if root.exists():raise FileExistsError('Existing resident runtime retained: '+str(root))
    if shutil.disk_usage(root.parent).free<15*1024**3+source.stat().st_size:
        raise ValueError('15 GiB reserve required')
    record=dict(schema=1,enabled=True,format='nv12-sdr-cfr',quality=4,max_output_pixels=3840*2160,
        verified_devices=[[report['adapter']['vendor_id'],report['adapter']['device_id']]],
        pyav=report['pyav'],av_libraries=report['av_libraries'],
        cuda_vsr_manifest=digest(ROOT/'runtime/vsr-cuda/manifest.json'),
        encoder_manifest=digest(ROOT/'runtime/gpu-export/manifest.json'),
        files={'nv12-rgba.ptx':digest(source)},evidence_sha256=digest(evidence),
        acceptance='SDR CFR pure SR, >=96 frames, 1080p..4K output, P5 high/balanced; bounded worker retirement',
        packaging_license_review_complete=False)
    root.mkdir()
    target=safe(root/'nv12-rgba.ptx',root)
    with source.open('rb') as incoming,target.open('xb') as outgoing:
        shutil.copyfileobj(incoming,outgoing)
    assert digest(target)==record['files']['nv12-rgba.ptx']
    with (root/'manifest.json').open('x',encoding='utf-8') as out:json.dump(record,out,indent=2)
    assert load_components() is not None
    print(json.dumps(dict(installed=str(root),ptx_sha256=digest(target))))


if __name__=='__main__':main()
