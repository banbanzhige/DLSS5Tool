"""Exercise the real gated Job/token path without GPU work or global changes."""
import argparse
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def mp_child(connection):
    if connection.recv() != 'start':
        return
    from scripts.windows_process_context import current_context
    connection.send(current_context())
    connection.close()


def main(args):
    from dlss5tool.nvidia_token_compat import _Windows
    from dlss5tool.process_job import KillOnCloseJob, create_process_job
    from scripts.verify_p0_stability import checked_path
    from scripts.windows_process_context import current_context
    work = checked_path(args.task)
    if not work.is_relative_to(ROOT/'tmp') or not (work/'TASK.md').is_file():
        raise ValueError('Use a registered task')
    api = _Windows()
    own = api.open_token(api.k.GetCurrentProcess(), 0x20008)
    original = api.dacl(own)
    parent_state = api.state(own)
    results = []
    names = ['dlss5tool/nvidia_token_compat.py','dlss5tool/process_job.py',
             'scripts/verify_nvidia_token_compat.py','scripts/windows_process_context.py']
    def manifest():
        return {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names}
    before = manifest()
    try:
        for kind in ('popen', 'spawn'):
            for index in range(args.cycles):
                process = job = sender = receiver = None
                try:
                    if kind == 'popen':
                        process = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), '--child'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=subprocess.CREATE_NO_WINDOW, cwd=ROOT)
                        job = KillOnCloseJob(process, nvidia_compat=True)
                        output, errors = process.communicate(b'G', timeout=20)
                        assert process.returncode == 0, errors.decode(errors='replace')
                        context = json.loads(output)
                    else:
                        context_factory = multiprocessing.get_context('spawn')
                        receiver, sender = context_factory.Pipe()
                        process = context_factory.Process(target=mp_child, args=(sender,), daemon=True)
                        process.start()
                        sender.close(); sender = None
                        job = create_process_job(process, nvidia_compat=True)
                        receiver.send('start')
                        assert receiver.poll(20), 'Child context response timed out'
                        context = receiver.recv()
                        process.join(20)
                        assert process.exitcode == 0
                    assert context['pid'] == process.pid and context['query_complete']
                    assert context['primary_token_id'] != parent_state['id']
                    assert job.token_compatibility['status'] == args.expect, job.token_compatibility
                    assert api.dacl(own) == original and api.state(own) == parent_state
                    results.append(dict(kind=kind, cycle=index, context=context,
                        compatibility=job.token_compatibility, parent_token_unchanged=True))
                finally:
                    if job: job.close()
                    if process:
                        if kind == 'popen':
                            if process.poll() is None: process.wait(timeout=10)
                            for stream in (process.stdin, process.stdout, process.stderr):
                                if stream: stream.close()
                        else:
                            process.join(10)
                            assert not process.is_alive()
                            process.close()
                    if sender: sender.close()
                    if receiver: receiver.close()
        assert manifest() == before
        result = dict(status='passed', cycles=results, parent_context=current_context(),
            source_unchanged=True, source_manifest=before, no_gpu_work=True,
            parent_dacl_sha256=hashlib.sha256(original).hexdigest(), no_remaining_children=True)
        target = work/args.output
        if checked_path(target).parent != work or target.exists():
            raise ValueError('Output must be a fresh file inside the task')
        target.write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
        print(json.dumps(dict(status='passed', cycles=len(results), expected=args.expect,
                              parent_token_unchanged=True, output=str(target))))
    finally:
        api.close(own)


if __name__ == '__main__':
    if sys.argv[1:] == ['--child']:
        if sys.stdin.buffer.read(1) != b'G':
            raise SystemExit(2)
        from scripts.windows_process_context import current_context
        print(json.dumps(current_context()), flush=True)
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--task', type=Path, required=True)
        parser.add_argument('--cycles', type=int, default=3, choices=range(1, 6))
        parser.add_argument('--expect', choices=('applied','unchanged','disabled'), required=True)
        parser.add_argument('--output', default='token-context-verification.json')
        main(parser.parse_args())
