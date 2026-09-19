"""Freeze final HDR/native-host/export-owner evidence without deleting artifacts."""
import hashlib
import json
from pathlib import Path
import shutil

ROOT=Path(__file__).resolve().parents[1];WORK=ROOT/'tmp/export-integration-20260917'
DEST=ROOT/'docs/development/gpu-export-upgrade-20260919'


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    manifest=DEST/'MANIFEST.json'
    if manifest.exists() or not (DEST/'README.md').is_file():raise RuntimeError('Fresh registered archive required')
    records=[]
    def copy(source,relative):
        target=DEST/relative;target.parent.mkdir(parents=True,exist_ok=True)
        if source.is_symlink() or target.exists():raise RuntimeError('Linked source/existing target rejected')
        shutil.copyfile(source,target)
        if sha(source)!=sha(target):raise RuntimeError('Archive copy mismatch')
        records.append(dict(source=str(source.relative_to(ROOT)),archive=relative.as_posix(),bytes=target.stat().st_size,sha256=sha(target)))
    for source in sorted(WORK.rglob('*')):
        if 'pytest' in str(source.relative_to(WORK)):continue
        if source.is_file() and source.suffix in ('.json','.mp4','.mkv','.mov','.log','.ptx'):
            copy(source,source.relative_to(WORK))
    names=('dlss5tool/cuda_hdr_yuv.py','dlss5tool/cuda_sdr_yuv.py','dlss5tool/gpu_video_export.py',
        'dlss5tool/gpu_export_process.py','dlss5tool/gpu_export_runtime.py','dlss5tool/dlss_engine.py',
        'dlss5tool/dlss_host_process.py','dlss5tool/frame_generation.py','dlss5tool/gui.py',
        'dlss5tool/render_cache.py','dlss5tool/parallel_export_worker.py','dlss5tool/guidance_export.py',
        'native/host_v2/dlssnr_host_v2.cpp','scripts/gpu_hdr_yuv.cu','scripts/build_sdr_yuv_ptx.py',
        'scripts/hdr_cuda_conversion_gate.py','scripts/hdr_conversion_contract_probe.py',
        'scripts/gpu_export_fg_gate.py','scripts/gpu_export_end_to_end_gate.py','scripts/gpu_export_cancel_gate.py',
        'scripts/gpu_export_regression_suite.py','scripts/install_gpu_export_components.py','scripts/archive_gpu_upgrade.py',
        'tests/test_gpu_export_routing.py','packaging/DLSS5Tool.spec')
    for name in names:copy(ROOT/name,Path('sources')/(name.replace('/','__')+'.txt'))
    copy(ROOT/'runtime/gpu-export/manifest.json',Path('registered-components.json'))
    references=[]
    for name,upstream in (('ffmpeg-output.c','output.c'),('ffmpeg-yuv2rgb.c','yuv2rgb.c'),('ffmpeg-swscale.c','swscale.c')):
        source=WORK/'hdr'/name
        references.append(dict(source=str(source.relative_to(ROOT)),sha256=sha(source),
            url='https://raw.githubusercontent.com/FFmpeg/FFmpeg/n7.1.1/libswscale/'+upstream))
    data=dict(records=records,total_bytes=sum(r['bytes'] for r in records),references=references,
        original_runtime={name:sha(ROOT/name) for name in ('runtime/dlssnr_host_v2.dll','runtime/dlssg_video_worker.exe')})
    with manifest.open('x',encoding='utf-8') as output:json.dump(data,output,ensure_ascii=False,indent=2)
    print(json.dumps(dict(files=len(records),bytes=data['total_bytes'],original_runtime=data['original_runtime'])))


if __name__=='__main__':main()
