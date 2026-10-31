"""Build only the isolated probe DLL from installed MSVC/Windows SDK files."""
import argparse,hashlib,json,os,platform,re,struct,subprocess,sys
from pathlib import Path


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build(output_dir):
    if sys.platform!='win32' or os.environ.get('GITHUB_ACTIONS')!='true':raise RuntimeError('Only the approved disposable Windows CI runner may compile this probe')
    source=Path(__file__).with_name('native_material_probe.cpp').resolve();out=Path(output_dir).resolve();out.mkdir(parents=True,exist_ok=True)
    names=('native_material_probe.cpp','build_windows_material_probe.py','window_material_win.py','ci_windows_material_probe.py','test_windows_material_probe.py','ci_windows_desktop_fixture.py','test_windows_desktop_fixture.py')
    hashes={name:sha(source.with_name(name)) for name in names}
    report=dict(ok=False,source=str(source.name),source_sha256=sha(source),source_hashes=hashes,github_sha=os.environ.get('GITHUB_SHA',''),workflow_ref=os.environ.get('GITHUB_WORKFLOW_REF',''),dependencies='installed MSVC + Windows SDK only; no downloads or system installation',architecture=dict(target='x64',python_pointer_bits=struct.calcsize('P')*8,platform_machine=platform.machine(),environment={name:os.environ.get(name,'') for name in ('RUNNER_ARCH','PROCESSOR_ARCHITECTURE','PROCESSOR_ARCHITEW6432','ImageOS','ImageVersion')}))
    try:
        program_files=Path(os.environ.get('ProgramFiles(x86)',r'C:\Program Files (x86)'))
        vswhere=program_files/'Microsoft Visual Studio/Installer/vswhere.exe'
        installation_path=subprocess.check_output([str(vswhere),'-latest','-products','*','-requires','Microsoft.VisualStudio.Component.VC.Tools.x86.x64','-property','installationPath'],text=True,timeout=15).strip()
        if not installation_path:raise RuntimeError('Installed MSVC x86/x64 component not found; no automatic installation allowed')
        install=Path(installation_path)
        versions=sorted((install/'VC/Tools/MSVC').iterdir(),key=lambda p:tuple(int(x) for x in p.name.split('.')))
        tool=versions[-1];compiler=tool/'bin/Hostx64/x64/cl.exe'
        report['compiler']=str(compiler)
        if not compiler.is_file():raise RuntimeError('Installed Hostx64/x64 compiler not found; no automatic installation allowed')
        sdk=program_files/'Windows Kits/10'
        sdk_versions=sorted((p for p in (sdk/'Include').iterdir() if re.fullmatch(r'10\.0\.[0-9]+\.[0-9]+',p.name) and (p/'cppwinrt/winrt/Windows.UI.Composition.h').is_file()),key=lambda p:tuple(int(x) for x in p.name.split('.')))
        headers=sdk_versions[-1]
        if tuple(map(int,headers.name.split('.')))<(10,0,22621,0):raise RuntimeError('Installed SDK is older than 22621; no automatic installation allowed')
        env=dict(os.environ)
        env['INCLUDE']=';'.join(map(str,[tool/'include',headers/'ucrt',headers/'shared',headers/'um',headers/'winrt',headers/'cppwinrt']))
        env['LIB']=';'.join(map(str,[tool/'lib/x64',sdk/'Lib'/headers.name/'ucrt/x64',sdk/'Lib'/headers.name/'um/x64']))
        env['PATH']=str(compiler.parent)+os.pathsep+env.get('PATH','')
        target=out/'flickdock_material_probe.dll'
        command=[str(compiler),'/nologo','/std:c++20','/permissive-','/EHsc','/MT','/O2','/LD','/W4',str(source),'/link','/OUT:'+str(target),'windowsapp.lib','CoreMessaging.lib','dwmapi.lib','user32.lib','gdi32.lib','runtimeobject.lib']
        completed=subprocess.run(command,cwd=out,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=180)
        (out/'compiler.log').write_text(completed.stdout,encoding='utf-8')
        report.update(compiler=str(compiler),sdk=headers.name,compiler_exit=completed.returncode)
        if completed.returncode:raise RuntimeError('Native probe build failed; see compiler.log')
        report.update(ok=True,dll=target.name,dll_sha256=sha(target),dll_size=target.stat().st_size)
    except Exception as error:report['error']=type(error).__name__+': '+str(error)
    (out/'build.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    if not report['ok']:raise RuntimeError(report)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output-dir',required=True)
    print(json.dumps(build(parser.parse_args().output_dir),ensure_ascii=False))
