"""Archive only this registered retest's small evidence, with byte verification."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[1]
WORK=ROOT/'tmp/encode-recheck-20260916'
DEST=ROOT/'docs/development/encode-recheck-20260916'


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    if not (WORK/'TASK.md').is_file() or not (DEST/'README.md').is_file():raise RuntimeError('Registered task/archive required')
    records=[]
    def copy(source,target):
        if source.is_symlink() or target.is_symlink():raise RuntimeError('No symlink evidence')
        if target.exists():
            if sha(source)!=sha(target):raise RuntimeError('Refusing overwrite '+str(target))
        else:shutil.copyfile(source,target)
        if sha(source)!=sha(target):raise RuntimeError('Archive verification failed')
        records.append(dict(source=str(source.relative_to(ROOT)),archive=str(target.relative_to(DEST)),bytes=target.stat().st_size,sha256=sha(target)))
    for source in sorted(WORK.iterdir()):
        if source.is_file() and source.suffix in ('.json','.mp4','.h264','.log'):
            copy(source,DEST/source.name)
    for name in ('scripts/encode_recheck.py','scripts/encode_acceptance_fg.py','scripts/gpu_nvenc_probe.cpp',
        'scripts/gpu_nvenc_ring.cpp','scripts/gpu_pipeline_candidates.py','scripts/encode_acceptance_nvenc_contract.py',
        'scripts/encode_acceptance_nvenc_ring.py','dlss5tool/video_export.py','tests/test_encode_recheck.py'):
        copy(ROOT/name,DEST/(Path(name).name+'.snapshot.txt'))
    old=DEST/'encode_recheck.v1.py'
    records.append(dict(source='initial retest script snapshot',archive=old.name,bytes=old.stat().st_size,sha256=sha(old)))
    driver=subprocess.run(['nvidia-smi','--query-gpu=name,driver_version,memory.total','--format=csv'],capture_output=True,text=True,timeout=10)
    data=dict(scope='2026-09-16 targeted retest; old reports unchanged; no production deployment',
        records=records,total_bytes=sum(r['bytes'] for r in records),
        environment=driver.stdout.strip(),git_head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        runtime_hashes={p:sha(ROOT/p) for p in ('runtime/dlssnr_host_v2.dll','runtime/dlssg_video_worker.exe')})
    with (DEST/'MANIFEST.json').open('x',encoding='utf-8') as out:json.dump(data,out,ensure_ascii=False,indent=2)
    print(json.dumps({'files':len(records),'bytes':data['total_bytes'],'archive':str(DEST)},ensure_ascii=False))


if __name__=='__main__':main()
