"""Archive this phase's bounded evidence, no deletion or external writes."""
import hashlib
import json
from pathlib import Path
import shutil

ROOT=Path(__file__).resolve().parents[1]
WORK=ROOT/'tmp/encode-integration-20260917'
DEST=ROOT/'docs/development/encoder-integration-20260917'


def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    if not (WORK/'TASK.md').is_file() or not (DEST/'README.md').is_file():raise RuntimeError('Registered work/archive required')
    manifest=DEST/'MANIFEST.json'
    if manifest.exists():raise FileExistsError('Archive already finalized')
    records=[]
    def copy(src,rel):
        target=DEST/rel;target.parent.mkdir(parents=True,exist_ok=True)
        if src.is_symlink() or target.is_symlink():raise RuntimeError('Linked evidence rejected')
        if target.exists() and sha(target)!=sha(src):raise FileExistsError(target)
        if not target.exists():shutil.copyfile(src,target)
        if sha(src)!=sha(target):raise RuntimeError('Copy differs')
        records.append(dict(source=str(src.relative_to(ROOT)),archive=str(rel).replace('\\','/'),bytes=target.stat().st_size,sha256=sha(target)))
    for src in sorted(WORK.rglob('*')):
        if src.is_file() and src.suffix in ('.json','.mp4','.h264','.hevc'):
            copy(src,src.relative_to(WORK))
    for name in ('dlss5tool/encoding_contract.py','dlss5tool/nvenc_yuv.py','dlss5tool/video_export.py',
        'scripts/gpu_nvenc_ring.cpp','scripts/encoder_contract_gate.py','scripts/encoder_lifecycle_gate.py',
        'scripts/build_nvenc_ring_probe.bat','scripts/encode_acceptance_nvenc_ring.py',
        'tests/test_encoding_contract.py','tests/test_nvenc_yuv.py'):
        copy(ROOT/name,Path('sources')/(Path(name).name+'.txt'))
    data=dict(records=records,total_bytes=sum(r['bytes'] for r in records),
        candidate_sha256=sha(WORK/'native-encoder/ring.dll'),
        runtime_sha256={name:sha(ROOT/name) for name in ('runtime/dlssnr_host_v2.dll','runtime/dlssg_video_worker.exe')})
    with manifest.open('x',encoding='utf-8') as out:json.dump(data,out,ensure_ascii=False,indent=2)
    print(json.dumps(dict(files=len(records),bytes=data['total_bytes'],candidate_sha256=data['candidate_sha256'])))


if __name__=='__main__':main()
