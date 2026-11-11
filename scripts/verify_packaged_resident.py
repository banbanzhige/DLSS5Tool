"""Run production resident export with actual packaged, isolated GPU workers.

The Python controller supplies the existing inherited-pipe protocol. All GPU
worker code and components come from the new onedir package, without Python
development paths. Does not modify the package or user installations.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.verify_p0_stability import checked_path

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packages',type=Path,required=True)
    parser.add_argument('--task',type=Path,required=True)
    parser.add_argument('--reference-result',type=Path)
    parser.add_argument('--edition',choices=('lite','full'),required=True)
    parser.add_argument('--label',required=True)
    parser.add_argument('--hdr',action='store_true')
    parser.add_argument('--enhance',action='store_true')
    parser.add_argument('--cancel-at',type=int)
    args=parser.parse_args()
    assert args.label.replace('-','').isalnum()
    task=checked_path(args.task)
    assert task.is_relative_to(ROOT/'tmp') and (task/'TASK.md').is_file()
    work=checked_path(task/('resident-'+args.label))
    from dlss5tool.app_version import APP_VERSION
    package=checked_path(args.packages/f'DLSS5Tool-{APP_VERSION}-win64-{args.edition}')
    exe=checked_path(package/'DLSS5Tool.exe')
    internal=checked_path(package/'_internal')
    from dlss5tool import paths
    paths.runtime_root=lambda:internal
    paths.resource_root=lambda:internal
    paths.app_root=lambda:package
    from dlss5tool import resident_sr,process_job
    from scripts import verify_resident_acceptance as acceptance

    launches=[];compatibility=[]
    original_job=process_job.KillOnCloseJob
    class RecordingJob(original_job):
        def __init__(self,*values,**options):
            super().__init__(*values,**options)
            compatibility.append(self.token_compatibility)

    def spawn(sender,nonce,log):
        handle=sender.fileno()
        environment=dict(os.environ,PATH=str(Path(os.environ['SystemRoot'])/'System32'),
                         PYTHONDONTWRITEBYTECODE='1')
        environment.pop('PYTHONPATH',None);environment.pop('PYTHONHOME',None)
        startup=subprocess.STARTUPINFO()
        startup.lpAttributeList={'handle_list':[handle]}
        previous=os.get_handle_inheritable(handle)
        command=[str(exe),resident_sr.WORKER_FLAG,str(handle),nonce]
        try:
            os.set_handle_inheritable(handle,True)
            child=subprocess.Popen(command,env=environment,stdin=subprocess.DEVNULL,
                stdout=log,stderr=log,close_fds=True,startupinfo=startup,
                creationflags=subprocess.CREATE_NO_WINDOW,cwd=internal)
        finally:
            os.set_handle_inheritable(handle,previous)
        launches.append(dict(pid=child.pid,executable=str(exe),cwd=str(internal),
            child_path=environment['PATH'],pythonpath_present='PYTHONPATH' in environment))
        return child

    with mock.patch.object(resident_sr,'_spawn',spawn),mock.patch.object(process_job,'KillOnCloseJob',RecordingJob):
        code=acceptance.main(SimpleNamespace(task=task,
            source=task/('source-pq96.mp4' if args.hdr else 'source-sdr96.mp4'),
            label=args.label,hdr=args.hdr,enhance=args.enhance,audit=True,cancel_at=args.cancel_at))
    metadata=dict(version=APP_VERSION,
        executable_sha256=hashlib.sha256(exe.read_bytes()).hexdigest(),launches=launches,
        compatibility=compatibility,gpu_worker_is_frozen_executable=True,
        scope='Actual packaged GPU worker, production _internal CWD and verified packaged components; Python test controller; no GUI automation')
    if args.reference_result and code==0:
        reference=json.loads(checked_path(args.reference_result).read_text(encoding='utf-8'))
        actual=json.loads((work/'verification.json').read_text(encoding='utf-8'))
        metadata['pixel_reference_passed']=(reference['source_sha256']==actual['source_sha256'] and reference['result']['frame_hashes']==actual['result']['frame_hashes'])
        metadata['reference_result_sha256']=hashlib.sha256(args.reference_result.read_bytes()).hexdigest()
        if not metadata['pixel_reference_passed']:code=1
    (work/'frozen-launch.json').write_text(json.dumps(metadata,indent=2)+'\n',encoding='utf-8')
    return code

if __name__=='__main__':
    raise SystemExit(main())
