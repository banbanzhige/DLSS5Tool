"""Register accepted small GPU export binaries; never replaces legacy runtimes."""
import hashlib
import json
from pathlib import Path
import shutil

ROOT=Path(__file__).resolve().parents[1]


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    work=ROOT/'tmp/export-integration-20260917'
    if not (work/'TASK.md').is_file():raise RuntimeError('Unregistered task')
    gates=('final-writers.json','isolated-hdr-fg2.json','isolated-enhance-hdr.json')
    for name in gates:
        if json.loads((work/name).read_text(encoding='utf-8'))['status']!='passed':raise RuntimeError('Gate failed: '+name)
    destination=ROOT/'runtime/gpu-export'
    if destination.exists():raise FileExistsError('Do not overwrite an existing component set')
    mapping={'encoder.dll':ROOT/'tmp/encode-integration-20260917/native-encoder/ring.dll',
        'sdr-yuv.ptx':ROOT/'tmp/encode-integration-20260917/sdr-conversion/sdr-yuv.ptx',
        'hdr-yuv.ptx':work/'hdr/hdr-yuv-strict.ptx','frame-worker.exe':work/'gpu-export-worker.exe',
        'enhancement.dll':work/'native-host/candidate.dll'}
    destination.mkdir()
    for name,source in mapping.items():
        if source.is_symlink():raise RuntimeError('Linked candidate rejected')
        shutil.copyfile(source,destination/name)
        if sha(source)!=sha(destination/name):raise RuntimeError('Copy mismatch')
    import sys
    sys.path.insert(0,str(ROOT))
    from dlss5tool.video_export import find_ffmpeg
    data=dict(schema=1,enabled=True,automatic_route='unscaled-bt2020-hdr-at-least-1080p-p5-high-balanced',
        files={name:sha(destination/name) for name in mapping},ffmpeg_sha256=sha(Path(find_ffmpeg())),
        gates={name:sha(work/name) for name in gates},packaging_license_review_complete=False)
    with (destination/'manifest.json').open('x',encoding='utf-8') as out:json.dump(data,out,indent=2)
    print(json.dumps(dict(path=str(destination),bytes=sum(p.stat().st_size for p in destination.iterdir()),files=data['files'])))


if __name__=='__main__':main()
