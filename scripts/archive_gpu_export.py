"""Archive registered GPU export evidence; no deletion or runtime replacement."""
import hashlib
import json
from pathlib import Path
import shutil

ROOT=Path(__file__).resolve().parents[1]
WORK=ROOT/'tmp/export-integration-20260917'
DEST=ROOT/'docs/development/gpu-export-integration-20260919'


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    manifest=DEST/'MANIFEST.json'
    if not (WORK/'TASK.md').is_file() or not (DEST/'README.md').is_file() or manifest.exists():
        raise RuntimeError('Fresh registered archive required')
    records=[]
    def copy(source,relative):
        target=DEST/relative;target.parent.mkdir(parents=True,exist_ok=True)
        if source.is_symlink() or target.exists():raise RuntimeError('Linked source or existing target rejected')
        shutil.copyfile(source,target)
        if sha(source)!=sha(target):raise RuntimeError('Hash mismatch')
        records.append(dict(source=str(source.relative_to(ROOT)),archive=relative.as_posix(),
            bytes=target.stat().st_size,sha256=sha(target)))
    for source in sorted(WORK.rglob('*')):
        if source.is_file() and source.suffix in ('.json','.mp4','.mkv','.mov','.wav','.m4a','.log'):
            copy(source,source.relative_to(WORK))
    for name in ('dlss5tool/frame_generation.py','dlss5tool/gpu_video_export.py',
        'dlss5tool/cuda_session.py','dlss5tool/cuda_interop.py','dlss5tool/packet_mux.py',
        'scripts/dlssg_video_worker.cpp','scripts/gpu_dlssg_bridge.h','scripts/build_gpu_export_worker.bat',
        'scripts/gpu_export_fg_gate.py','scripts/gpu_export_mux_gate.py','scripts/gpu_export_cancel_gate.py',
        'scripts/archive_gpu_export.py','tests/test_gpu_export_integration.py','requirements-gpu-export.txt'):
        copy(ROOT/name,Path('sources')/(Path(name).name+'.txt'))
    data=dict(records=records,total_bytes=sum(r['bytes'] for r in records),
        candidate_sha256=sha(WORK/'gpu-export-worker.exe'),
        runtime_sha256={name:sha(ROOT/name) for name in ('runtime/dlssnr_host_v2.dll','runtime/dlssg_video_worker.exe')})
    with manifest.open('x',encoding='utf-8') as output:json.dump(data,output,ensure_ascii=False,indent=2)
    print(json.dumps(dict(files=len(records),bytes=data['total_bytes'],candidate=data['candidate_sha256'])))


if __name__=='__main__':main()
