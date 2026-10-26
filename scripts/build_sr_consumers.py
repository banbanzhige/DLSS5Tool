"""Build one isolated D3D enhancement/FG device-input candidate set."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from scripts.build_vsr_cuda import safe, digest
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True, type=Path)
    parser.add_argument('--label', default='consumer-build')
    args = parser.parse_args()
    task = safe(args.task, ROOT/'tmp')
    if not args.label.replace('-', '').isalnum(): raise ValueError('Safe report label required')
    report = task/(args.label+'.json')
    if not (task/'TASK.md').is_file() or report.exists() or shutil.disk_usage(task).free < 18*1024**3:
        raise ValueError('Fresh registered work and disk reserve required')
    build = safe(task/'consumer-build', task)
    build.mkdir(exist_ok=True)
    vswhere = Path(os.environ['ProgramFiles(x86)'])/'Microsoft Visual Studio/Installer/vswhere.exe'
    vcvars = safe(subprocess.run([str(vswhere), '-latest', '-products', '*', '-requires',
        'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-find', 'VC/Auxiliary/Build/vcvars64.bat'],
        check=True, capture_output=True, text=True).stdout.strip())
    sdk = safe(ROOT/'third_party/NVIDIA-DLSS')
    library = safe(sdk/'lib/Windows_x86_64/x64/nvsdk_ngx_s.lib')
    commands = [f'@call "{vcvars}" >nul', '@if errorlevel 1 exit /b 1']
    sources = ['native/host_v2/dlssnr_host_v2.cpp', 'scripts/dlssg_video_worker.cpp']
    for name, source, options, extension in (
            ('enhancement', sources[0], '/LD', '.dll'),
            ('frame-worker', sources[1], '/DGPU_EXPORT', '.exe')):
        commands.extend((f'@cl /nologo /std:c++17 /O2 /EHsc /MT {options} /I"{sdk}/include" '
            f'/Fo"{build/name}.obj" "{ROOT/source}" "{library}" '
            f'D3D12.lib DXGI.lib Advapi32.lib User32.lib /link /OUT:"{build/(name+extension)}" '
            f'/PDB:"{build/name}.pdb" /IMPLIB:"{build/name}.lib"', '@if errorlevel 1 exit /b 1'))
    batch = build/'build.bat'
    batch.write_text('\n'.join(commands)+'\n', encoding='utf-8')
    result = subprocess.run(['cmd.exe', '/d', '/c', str(batch)], cwd=build,
        env=dict(os.environ, TEMP=str(task), TMP=str(task)), capture_output=True, text=True,
        errors='replace', timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)
    (task/(args.label+'.log')).write_text(result.stdout+'\n'+result.stderr, encoding='utf-8')
    print(result.stdout[-4000:]+result.stderr[-2000:])
    result.check_returncode()
    sources += [str(path.relative_to(ROOT)) for path in (ROOT/'native/host_v2').glob('*.h')]
    sources += ['scripts/dlssg_probe.cpp', 'scripts/gpu_dlssg_bridge.h', 'scripts/dlssg_ada_gate.h',
                'scripts/rtxmfg_temporal/midpoint_fix.cpp', 'scripts/rtxmfg_temporal/dlssg_provider_policy.cpp']
    record = dict(commands=commands, sources={name:digest(ROOT/name) for name in sources},
                  outputs={name:digest(build/name) for name in ('enhancement.dll', 'frame-worker.exe')},
                  sdk_library_sha256=digest(library))
    report.write_text(json.dumps(record, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
