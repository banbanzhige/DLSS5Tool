"""Build one HDR GPU host candidate and a standalone operator oracle/probe."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def main():
    from scripts.build_vsr_cuda import safe,digest
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task',type=Path,required=True)
    parser.add_argument('--sdk',type=Path,default=ROOT/'tmp/RTX_Video_SDK_v1.1.0')
    parser.add_argument('--label',default='build')
    args=parser.parse_args();task=safe(args.task,ROOT/'tmp');sdk=safe(args.sdk)
    if not args.label.replace('-','').isalnum():raise ValueError('Safe build label required')
    report_path=task/(args.label+'.json')
    if not (task/'TASK.md').is_file() or shutil.disk_usage(task).free<17*1024**3:
        raise ValueError('Registered task and disk reserve required')
    build=safe(task/'build',task);build.mkdir(exist_ok=True)
    if report_path.exists():raise FileExistsError('Fresh build report required')
    original=subprocess.run(['git','show','HEAD:native/vsr_host/vsr_host.cpp'],cwd=ROOT,
        check=True,capture_output=True,text=True).stdout
    current=(ROOT/'native/vsr_host/vsr_host.cpp').read_text(encoding='utf-8')
    def helpers(text):return text[text.index('float HalfToFloat(uint16_t half)'):text.index('\nvoid CopyInput(')]
    if helpers(original)!=helpers(current):raise ValueError('CPU oracle changed from checkpoint')
    reference=build/'cpu_hdr_reference.h';reference.write_text(helpers(original),encoding='utf-8')
    vswhere=Path(os.environ['ProgramFiles(x86)'])/'Microsoft Visual Studio/Installer/vswhere.exe'
    vcvars=safe(subprocess.run([str(vswhere),'-latest','-products','*','-requires',
        'Microsoft.VisualStudio.Component.VC.Tools.x86.x64','-find','VC/Auxiliary/Build/vcvars64.bat'],
        check=True,capture_output=True,text=True).stdout.strip())
    native=safe(ROOT/'native/vsr_host');lib=safe(sdk/'lib/Windows/x64/nvsdk_ngx_s.lib')
    commands=[f'@call "{vcvars}" >nul','@if errorlevel 1 exit /b 1']
    for name,source in [('vsr_host','vsr_host.cpp'),('hdr_operators','hdr_operator_probe.cpp')]:
        commands.append(f'@cl /nologo /std:c++17 /O2 /EHsc /MT /LD /I"{sdk}/include" /I"{build}" '
            f'/Fo"{build/name}.obj" "{native/source}" '
            +(f'"{lib}" ' if name=='vsr_host' else '')+
            f'D3D12.lib DXGI.lib D3DCompiler.lib Advapi32.lib User32.lib /link '
            f'/OUT:"{build/name}.dll" /PDB:"{build/name}.pdb" /IMPLIB:"{build/name}.lib"')
        commands.append('@if errorlevel 1 exit /b 1')
    batch=build/'build.bat';batch.write_text('\n'.join(commands)+'\n',encoding='utf-8')
    environment=dict(os.environ,TEMP=str(task),TMP=str(task))
    result=subprocess.run(['cmd.exe','/d','/c',str(batch)],cwd=build,env=environment,
        capture_output=True,text=True,errors='replace',timeout=120,creationflags=subprocess.CREATE_NO_WINDOW)
    (task/'build.log').write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
    print(result.stdout[-4000:]+result.stderr[-2000:]);result.check_returncode()
    record=dict(base_commit=subprocess.run(['git','rev-parse','HEAD'],cwd=ROOT,check=True,capture_output=True,text=True).stdout.strip(),
        source_files={str(p.relative_to(ROOT)):digest(p) for p in [native/'vsr_host.cpp',native/'hdr_gpu_conversion.h',native/'hdr_operator_probe.cpp']},
        reference_sha256=digest(reference),sdk_library_sha256=digest(lib),
        previous_host_sha256=digest(ROOT/'runtime/vsr_host.dll'),provider_sha256=digest(ROOT/'runtime/nvngx_vsr.dll'),
        outputs={name:digest(build/(name+'.dll')) for name in ('vsr_host','hdr_operators')},commands=commands)
    report_path.write_text(json.dumps(record,indent=2),encoding='utf-8')
    print(json.dumps(dict(outputs=record['outputs'],previous_host_sha256=record['previous_host_sha256'])))


if __name__=='__main__':
    import sys
    sys.path.insert(0,str(ROOT))
    main()
