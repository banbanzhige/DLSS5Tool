"""Read-only code and input verification for a newly built portable executable."""
import argparse
import hashlib
import json
import marshal
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MODULES = ('gui', 'app_version', 'residual_composition', 'shared_cache_budget',
           'video_export', 'preview_decoder', 'local_preview_composition',
           'memory_budget', 'frame_generation', 'render_cache', 'device_cached',
           'resident_preview', 'resident_hdr', 'resident_sr', 'cuda_vsr', 'device_sr_plan')


def identity(value):
    if isinstance(value, types.CodeType):
        # PyInstaller rewrites source filenames. All executable instructions,
        # constants, flags, exception and line tables must remain identical.
        fields = ('co_code', 'co_argcount', 'co_posonlyargcount', 'co_kwonlyargcount',
                  'co_nlocals', 'co_stacksize', 'co_flags', 'co_names', 'co_varnames',
                  'co_freevars', 'co_cellvars', 'co_exceptiontable', 'co_linetable',
                  'co_firstlineno', 'co_name', 'co_qualname')
        return tuple(getattr(value, field) for field in fields) + (identity(value.co_consts),)
    if isinstance(value, tuple):
        return tuple(identity(item) for item in value)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists():
        raise FileExistsError(args.report)
    from PyInstaller.archive.readers import CArchiveReader
    from dlss5tool.app_version import APP_VERSION

    expected = json.loads(args.snapshot.read_text(encoding='utf-8-sig'))
    for name, record in expected['inputs'].items():
        path = ROOT / name
        actual = {'size': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        if actual != record:
            raise ValueError('Build input changed: ' + name)
    executable = CArchiveReader(str(args.exe.resolve(strict=True)))
    launcher = marshal.loads(executable.extract('gui'))
    if identity(launcher) != identity(compile((ROOT / 'gui.py').read_bytes(), 'gui.py',
                                            'exec', dont_inherit=True, optimize=0)):
        raise ValueError('Frozen launcher differs from source: gui.py')
    archive = executable.open_embedded_archive('PYZ.pyz')
    verified = []
    for name in MODULES:
        module = 'dlss5tool.' + name
        actual = archive.extract(module)
        source = compile((ROOT / 'dlss5tool' / (name + '.py')).read_bytes(), module,
                         'exec', dont_inherit=True, optimize=0)
        if identity(actual) != identity(source):
            raise ValueError('Frozen code differs from source: ' + module)
        verified.append(module)
    manifests = []
    for name, record in expected['inputs'].items():
        if name.startswith('runtime/') and name.endswith('/manifest.json'):
            relative = Path(name).relative_to('runtime')
            path = args.exe.parent / '_internal' / relative
            if hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256']:
                raise ValueError('Frozen runtime manifest differs: ' + str(relative))
            manifests.append(relative.as_posix())
    report = {'version': APP_VERSION, 'source_commit': expected['commit'],
              'inputs_unchanged': len(expected['inputs']), 'modules_verified': verified,
              'launcher_verified': 'gui.py',
              'runtime_manifests_verified': manifests, 'passed': True}
    args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
