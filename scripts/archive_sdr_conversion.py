"""Archive bounded SDR conversion evidence; no deletes or deployed DLL writes."""
import hashlib
import json
from pathlib import Path
import shutil

ROOT=Path(__file__).resolve().parents[1]
WORK=ROOT/'tmp/encode-integration-20260917/sdr-conversion'
DEST=ROOT/'docs/development/encoder-integration-20260917/sdr-conversion'


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    manifest=DEST/'MANIFEST.json'
    if manifest.exists() or not (DEST/'README.md').is_file():raise RuntimeError('Fresh registered archive required')
    records=[]
    def copy(source,relative):
        target=DEST/relative;target.parent.mkdir(parents=True,exist_ok=True)
        if source.is_symlink() or target.exists():raise RuntimeError('Linked source or existing target rejected')
        shutil.copyfile(source,target)
        if sha(source)!=sha(target):raise RuntimeError('Copy differs')
        records.append(dict(source=str(source.relative_to(ROOT)),archive=relative.as_posix(),
                            bytes=target.stat().st_size,sha256=sha(target)))
    for source in sorted(WORK.iterdir()):
        if source.suffix in ('.json','.mp4','.ptx'):copy(source,Path(source.name))
    for name in ('scripts/gpu_sdr_yuv.cu','scripts/build_sdr_yuv_ptx.py',
        'scripts/sdr_yuv_conversion_probe.py','scripts/sdr_cuda_encode_gate.py',
        'scripts/archive_sdr_conversion.py','dlss5tool/cuda_sdr_yuv.py',
        'tests/test_cuda_sdr_yuv.py','dlss5tool/nvenc_yuv.py','dlss5tool/encoding_contract.py'):
        copy(ROOT/name,Path('sources')/(Path(name).name+'.txt'))
    data=dict(records=records,total_bytes=sum(row['bytes'] for row in records),
        native_sha256=sha(WORK.parent/'native-encoder/ring.dll'),
        runtime_sha256={name:sha(ROOT/name) for name in ('runtime/dlssnr_host_v2.dll','runtime/dlssg_video_worker.exe')})
    with manifest.open('x',encoding='utf-8') as output:json.dump(data,output,ensure_ascii=False,indent=2)
    print(json.dumps(dict(files=len(records),bytes=data['total_bytes'],runtime=data['runtime_sha256'])))


if __name__=='__main__':main()
