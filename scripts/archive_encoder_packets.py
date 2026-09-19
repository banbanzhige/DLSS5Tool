"""Preserve packet gate evidence and source snapshots; never deletes files."""
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'tmp/encode-integration-20260917'
DEST = ROOT / 'docs/development/encoder-integration-20260917/packet-timing'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    manifest = DEST / 'MANIFEST.json'
    if manifest.exists() or not (DEST / 'README.md').is_file():
        raise RuntimeError('Fresh registered archive required')
    records = []
    def copy(source, relative):
        target = DEST / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink() or target.exists():
            raise RuntimeError('Linked source or existing target rejected')
        shutil.copyfile(source, target)
        if sha(source) != sha(target):
            raise RuntimeError('Archive verification failed')
        records.append(dict(source=str(source.relative_to(ROOT)), archive=relative.as_posix(),
                            bytes=target.stat().st_size, sha256=sha(target)))
    for source in sorted((WORK / 'packet-timing').iterdir()):
        if source.suffix in ('.json', '.mp4'):
            copy(source, Path(source.name))
    copy(WORK / 'lifecycle.json', Path('lifecycle-abi2.json'))
    for name in ('scripts/gpu_nvenc_ring.cpp', 'dlss5tool/nvenc_yuv.py',
                 'scripts/encoder_packet_gate.py', 'scripts/encoder_contract_gate.py',
                 'scripts/encoder_lifecycle_gate.py', 'tests/test_nvenc_yuv.py',
                 'scripts/archive_encoder_packets.py'):
        copy(ROOT / name, Path('sources') / (Path(name).name + '.txt'))
    data = dict(records=records, total_bytes=sum(row['bytes'] for row in records),
        candidate_sha256=sha(WORK / 'native-encoder/ring.dll'),
        runtime_sha256={name:sha(ROOT/name) for name in
            ('runtime/dlssnr_host_v2.dll', 'runtime/dlssg_video_worker.exe')})
    with manifest.open('x', encoding='utf-8') as out:
        json.dump(data, out, ensure_ascii=False, indent=2)
    print(json.dumps(dict(files=len(records), bytes=data['total_bytes'], candidate=data['candidate_sha256'])))


if __name__ == '__main__':
    main()
