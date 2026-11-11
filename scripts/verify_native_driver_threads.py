"""Bounded installed-driver/thread controls with parent-owned Windows Jobs.

Build native_driver_thread_probe.cpp and native_heap_trace.cpp inside a
registered tmp task first. No GPU queries, driver changes, or remote injection.
Tracing and untraced controls are separate processes; raw heap metadata stays
inside the task. Results do not certify an application memory plateau.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dlss5tool.memory_budget import system_memory
from dlss5tool.process_job import KillOnCloseJob
from scripts.ngx_updater_lifecycle_probe import Processes
from scripts.verify_p0_stability import Memory, checked_path
from scripts.verify_native_worker_recycling import job_pids


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, default=str) + '\n', encoding='utf-8')


def run_one(args, name, target, tracing):
    work = checked_path(args.task / ('driver-threads-' + args.label + '-' + name))
    work.mkdir(exist_ok=False)
    command = [str(args.probe), str(target), str(args.threads),
               str(args.trace) if tracing else '-', str(work / 'heap')]
    sources = [Path(__file__).resolve(), ROOT / 'scripts/native_driver_thread_probe.cpp',
               ROOT / 'scripts/native_heap_trace.cpp', ROOT / 'dlss5tool/process_job.py']
    record = dict(name=name, target=str(target), target_sha256=digest(target),
        exe_sha256=digest(args.probe), trace_sha256=digest(args.trace) if tracing else None,
        source_sha256={str(path.relative_to(ROOT)): digest(path) for path in sources},
        command=command, gpu_queries=False, trace_enabled=tracing,
        requested_threads=args.threads, processing_and_exit_passed=False,
        long_term_memory_plateau_verified=False, samples=[])
    processes = Processes()
    memory = Memory(processes)
    began = time.monotonic()
    child = job = None
    try:
        with (work / 'console.jsonl').open('wb') as output:
            child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=output,
                stderr=subprocess.STDOUT, cwd=work,
                env=dict(os.environ, TEMP=str(work), TMP=str(work)),
                creationflags=subprocess.CREATE_NO_WINDOW)
            job = KillOnCloseJob(child)
            row, = [r for r in processes.snapshot() if r['pid'] == child.pid]
            identity = processes.identity(row)
            if not identity.get('created'):
                raise RuntimeError('Could not verify owned process identity')
            record['identity'] = identity
            child.stdin.write(b'G')
            child.stdin.close()
            while child.poll() is None:
                status = system_memory()
                sample = dict(seconds=time.monotonic() - began,
                              memory=memory.read(identity), system=status)
                record['samples'].append(sample)
                if (sample['seconds'] > 120 or sample['memory'].get('private_bytes', 0) > 1024**3
                        or not status or min(status['free_commit_bytes'], status['free_physical_bytes']) < 8 * 1024**3):
                    raise RuntimeError('Diagnostic time/private/headroom limit')
                time.sleep(.25)
            child.wait(timeout=10)
            deadline = time.monotonic() + 10
            while job_pids(job) and time.monotonic() < deadline:
                time.sleep(.1)
            record['remaining_before_cleanup'] = job_pids(job)
            if child.returncode or record['remaining_before_cleanup']:
                raise RuntimeError('Native probe failed or its Job did not retire')
        rows = [json.loads(line) for line in (work / 'console.jsonl').read_text(encoding='utf-8').splitlines()]
        outcome = rows[-1]
        if (outcome.get('kind') != 'result' or outcome.get('error') != 0
                or outcome.get('completed') != args.threads
                or outcome.get('restored') != outcome.get('patches')
                or outcome.get('trace_enabled') is not tracing):
            raise RuntimeError('Incomplete native outcome or IAT restoration')
        record['native_result'] = outcome
        record['source_unchanged'] = all(digest(path) == record['source_sha256'][str(path.relative_to(ROOT))]
                                         for path in sources)
        record['target_unchanged'] = digest(target) == record['target_sha256']
        if not record['source_unchanged'] or not record['target_unchanged']:
            raise RuntimeError('Measured source or installed target changed during the run')
        record['processing_and_exit_passed'] = True
    except BaseException:
        record['error'] = traceback.format_exc()
    finally:
        if child and child.stdin and not child.stdin.closed:
            child.stdin.close()
        if job:
            job.close()
        if child:
            if child.poll() is None:
                child.terminate()  # Only this Popen child, including failed Job assignment.
            child.wait(timeout=10)
            record['returncode'] = child.returncode
        record['elapsed_seconds'] = time.monotonic() - began
        save(work / 'verification.json', record)
    print(json.dumps(dict(name=name, passed=record['processing_and_exit_passed'],
                         elapsed=record['elapsed_seconds'], error=record.get('error'))), flush=True)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--driver', type=Path, required=True)
    parser.add_argument('--probe', type=Path, required=True)
    parser.add_argument('--trace', type=Path, required=True)
    parser.add_argument('--threads', type=int, default=30000)
    parser.add_argument('--label', default='r1')
    args = parser.parse_args()
    args.task = checked_path(args.task)
    args.driver, args.probe, args.trace = (checked_path(path) for path in (args.driver, args.probe, args.trace))
    system = checked_path(Path(os.environ['SystemRoot']) / 'System32')
    if (not args.task.is_relative_to(ROOT / 'tmp') or not (args.task / 'TASK.md').is_file()
            or not args.driver.is_relative_to(system) or args.driver.name.lower() != 'nvwgf2umx.dll'
            or not args.driver.is_file() or not 1000 <= args.threads <= 100000
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', args.label)):
        parser.error('Registered task, installed System32 driver and bounded arguments required')
    for path, suffix in ((args.probe, '.exe'), (args.trace, '.dll')):
        if not path.is_relative_to(args.task) or path.suffix.lower() != suffix or not path.is_file():
            parser.error('Probe/tracer must be regular task-local files')
    control = checked_path(system / 'version.dll')
    summary_path = checked_path(args.task / ('driver-threads-summary-' + args.label + '.json'))
    if summary_path.exists():
        parser.error('Choose a fresh label; never overwrite measured results')
    results = []
    for tracing in (False, True):
        for target_name, target in (('control', control), ('nvidia', args.driver)):
            name = target_name + ('-traced' if tracing else '-untraced')
            result = run_one(args, name, target, tracing)
            results.append(result)
            save(summary_path, results)
            if not result['processing_and_exit_passed']:
                return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
