"""Build one isolated CUDA VSR candidate using the existing SDK and CUDA 12 wheel.

No environment installation or runtime replacement. Register --task/TASK.md and
extract the verified nvidia-cuda-runtime-cu12 12.8.90 wheel into that task first.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def safe(path, parent=None):
    path = Path(path).absolute()
    if path.resolve() != path or (parent and not path.is_relative_to(parent)):
        raise ValueError(f'Unexpected path: {path}')
    for item in (path, *path.parents):
        if item.exists() and item.lstat().st_file_attributes & 0x400:
            raise ValueError(f'Reparse point: {item}')
    if any(char in str(path) for char in '\r\n"%!&|<>^'):
        raise ValueError(f'Unsupported build path: {path}')
    return path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True, type=Path)
    parser.add_argument('--sdk', type=Path, default=ROOT/'tmp/RTX_Video_SDK_v1.1.0')
    parser.add_argument('--cuda-headers', type=Path, help='Reuse an existing CUDA include directory')
    parser.add_argument('--cudart', type=Path, help='Reuse the verified runtime DLL without copying it')
    args = parser.parse_args()
    task = safe(args.task, ROOT/'tmp')
    sdk = safe(args.sdk)
    if not (task/'TASK.md').is_file() or shutil.disk_usage(task).free < 17*1024**3:
        raise ValueError('Registered task and >=17 GiB free required')
    cuda = safe(args.cuda_headers) if args.cuda_headers else safe(task/'cuda-runtime-12.8/nvidia/cuda_runtime/include', task)
    build = safe(task/'build', task)
    build.mkdir(exist_ok=True)
    vswhere = Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'))/'Microsoft Visual Studio/Installer/vswhere.exe'
    found = subprocess.run([str(vswhere), '-latest', '-products', '*', '-requires',
        'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-find', 'VC/Auxiliary/Build/vcvars64.bat'],
        check=True, capture_output=True, text=True).stdout.strip()
    vcvars = safe(found)
    driver = safe(Path(os.environ['SystemRoot'])/'System32/nvcuda.dll')
    runtime = safe(args.cudart) if args.cudart else safe(cuda.parent/'bin/cudart64_12.dll', task)
    # The wheel ships headers/DLL, no .lib. Generate ordinary import libraries
    # from their actual exports rather than shipping another CUDA installation.
    commands = [f'@call "{vcvars}" >nul', '@if errorlevel 1 exit /b 1']
    for name, dll in (('nvcuda', driver), ('cudart64_12', runtime)):
        commands.extend((f'@dumpbin /nologo /exports "{dll}" > "{build/name}.exports"',
                         '@if errorlevel 1 exit /b 1'))
    exports_bat = build/'exports.bat'
    exports_bat.write_text('\n'.join(commands)+'\n', encoding='utf-8')
    env = dict(os.environ, TEMP=str(task), TMP=str(task))
    subprocess.run(['cmd.exe', '/d', '/c', str(exports_bat)], check=True, env=env, cwd=build)
    commands = [f'@call "{vcvars}" >nul', '@if errorlevel 1 exit /b 1']
    for name in ('nvcuda', 'cudart64_12'):
        exports = re.findall(r'^\s+\d+\s+[0-9A-F]+\s+[0-9A-F]+\s+(\w+)',
                            (build/f'{name}.exports').read_text(), re.MULTILINE)
        if not exports:
            raise ValueError(f'No DLL exports: {name}')
        definition = build/f'{name}.def'
        definition.write_text(f'LIBRARY {name}.dll\nEXPORTS\n'+'\n'.join(exports)+'\n')
        commands.extend((f'@lib /nologo /machine:x64 /def:"{definition}" /out:"{build/name}.lib"',
                         '@if errorlevel 1 exit /b 1'))
    source = ROOT/'native/vsr_cuda/vsr_cuda.cpp'
    output = build/'vsr_cuda.dll'
    commands.extend((f'@cl /nologo /std:c++17 /O2 /EHsc /MT /LD '
        f'/I"{sdk}/include" /I"{cuda}" /Fo"{build}/vsr_cuda.obj" "{source}" '
        f'"{sdk}/lib/Windows/x64/nvsdk_ngx_s.lib" "{build}/nvcuda.lib" "{build}/cudart64_12.lib" '
        f'Advapi32.lib User32.lib /link /OUT:"{output}" /PDB:"{build}/vsr_cuda.pdb" '
        f'/IMPLIB:"{build}/vsr_cuda.lib"', '@exit /b %errorlevel%'))
    batch = build/'build.bat'
    batch.write_text('\n'.join(commands)+'\n', encoding='utf-8')
    result = subprocess.run(['cmd.exe', '/d', '/c', str(batch)], env=env, cwd=build,
                            capture_output=True, text=True, errors='replace', timeout=120)
    (task/'build.log').write_text(result.stdout+'\n'+result.stderr, encoding='utf-8')
    print(result.stdout[-6000:]+result.stderr[-2000:])
    result.check_returncode()
    record = dict(output=str(output), source_sha256=digest(source), output_sha256=digest(output),
        ngx_library_sha256=digest(sdk/'lib/Windows/x64/nvsdk_ngx_s.lib'),
        cuda_header_sha256=digest(cuda/'cuda.h'), cudart_sha256=digest(runtime),
        driver_sha256=digest(driver), vcvars=str(vcvars), command=commands[-2])
    (task/'build.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
