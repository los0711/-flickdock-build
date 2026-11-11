"""Disposable Windows CI: real Restart Manager before/after tray shutdown.

Never run installers on a development desktop. Only a generated fixture and
an unregistered test installer in one temporary directory are used here.
"""
import argparse,hashlib,json,os,re,shutil,subprocess,tempfile,time
from pathlib import Path
from uuid import uuid4


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
    if os.name!='nt' or os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('FD_QA_NATIVE')!='1':
        raise RuntimeError('Run only in the disposable Windows native CI job.')
    root=Path(__file__).resolve().parent;script=(root/'FlickDock.iss').read_text(encoding='utf8')
    mode=re.search(r'^CloseApplications=(\w+)$',script,re.M).group(1)
    filter=re.search(r'^CloseApplicationsFilter=(.+)$',script,re.M).group(1).strip()
    assert mode=='force' and set(filter.split(','))=={'FlickDock.exe','node.exe','syncthing.exe'}
    candidates=[shutil.which('ISCC.exe'),str(Path(os.environ.get('ProgramFiles(x86)','C:/Program Files (x86)'))/'Inno Setup 6/ISCC.exe'),
                str(Path(os.environ['LOCALAPPDATA'])/'Programs/Inno Setup 6/ISCC.exe')]
    for base in (Path(os.environ.get('ProgramFiles(x86)','C:/Program Files (x86)')),Path(os.environ['LOCALAPPDATA'])/'Programs'):
        if base.is_dir():candidates.extend(str(path) for path in base.glob('Inno Setup*/ISCC.exe'))
    compiler=next((str(Path(value)) for value in candidates if value and Path(value).is_file()),None)
    if not compiler:raise RuntimeError('Inno Setup 6 is required for the native installer regression.')
    csc=Path(os.environ['WINDIR'])/'Microsoft.NET/Framework64/v4.0.30319/csc.exe';assert csc.is_file()
    report=dict(ok=False,scope='Native Restart Manager and isolated tray-like WinForms fixture; no real FlickDock installation',checks=[])
    output=Path(args.output).resolve();output.parent.mkdir(parents=True,exist_ok=True)
    digest=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()
    processes=[]
    with tempfile.TemporaryDirectory(prefix='flickdock-installer-close-') as directory:
        folder=Path(directory).resolve();assert folder.name.startswith('flickdock-installer-close-')
        target=folder/'installed';target.mkdir();ready=folder/'ready.txt'
        source='''using System;using System.IO;using System.Windows.Forms;
[assembly:System.Reflection.AssemblyVersion("VERSION")]
class TrayWindow:Form {
 protected override void WndProc(ref Message m) {
  if(m.Msg==0x11){m.Result=IntPtr.Zero;return;}
  if(m.Msg==0x10){Hide();m.Result=IntPtr.Zero;return;}
  base.WndProc(ref m);
 }
 [STAThread]static void Main(string[] args){Application.EnableVisualStyles();var f=new TrayWindow();
  f.Text="FlickDock Content Shelf QA";f.Shown+=(s,e)=>File.WriteAllText(args[0],System.Diagnostics.Process.GetCurrentProcess().Id+Environment.NewLine+"ready "+String.Join(" ",args));Application.Run(f);}
}'''
        for version in ('1.0.0.0','1.0.1.0'):
            cs=folder/(version+'.cs');cs.write_text(source.replace('VERSION',version),encoding='utf8')
            subprocess.run([str(csc),'/nologo','/target:winexe','/r:System.Windows.Forms.dll','/r:System.Drawing.dll',
                            '/out:'+str(folder/(version+'.exe')),str(cs)],check=True,capture_output=True,timeout=30)
        fixture=target/'FlickDock.exe';shutil.copy2(folder/'1.0.0.0.exe',fixture)
        child=subprocess.Popen([str(fixture),str(ready)]);processes.append(child)
        def stop(process):
            if process.poll() is None:
                subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,timeout=15)
                process.wait(timeout=15)
        try:
            deadline=time.monotonic()+10
            while not ready.exists() and time.monotonic()<deadline:
                if child.poll() is not None:raise RuntimeError('Tray fixture exited before readiness')
                time.sleep(.05)
            assert ready.exists()
            setups={}
            for value in ('yes',mode):
                destination=folder/value;destination.mkdir()
                definition=folder/(value+'.iss')
                definition.write_text(f'''[Setup]
AppName=FlickDock isolated shutdown QA
AppVersion=1.0.1
AppId=FlickDock-Installer-QA-{uuid4().hex}
DefaultDirName={target}
PrivilegesRequired=lowest
Uninstallable=no
CreateUninstallRegKey=no
DisableDirPage=yes
DisableProgramGroupPage=yes
UsePreviousAppDir=no
CloseApplications={value}
CloseApplicationsFilter={filter}
RestartApplications=no
OutputDir={destination}
OutputBaseFilename=close-test
[Files]
Source: "{folder/'1.0.1.0.exe'}"; DestDir: "{{app}}"; DestName: "FlickDock.exe"; Flags: ignoreversion
''',encoding='utf8')
                compiled=subprocess.run([compiler,str(definition)],capture_output=True,timeout=40)
                assert compiled.returncode==0,compiled.stdout.decode(errors='replace')[-1500:]
                setups[value]=destination/'close-test.exe'
            legacy=subprocess.Popen([str(setups['yes']),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/LOG='+str(folder/'legacy.log')])
            processes.append(legacy)
            try:legacy_code=legacy.wait(timeout=12)
            except subprocess.TimeoutExpired:legacy_code=None;stop(legacy)
            assert child.poll() is None,'Legacy behavior did not reproduce the tray process remaining alive'
            report['checks'].append(dict(name='legacy close keeps tray process alive',ok=True,installer_exit=legacy_code))
            fixed=subprocess.Popen([str(setups[mode]),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/LOG='+str(folder/'fixed.log')])
            processes.append(fixed)
            try:code=fixed.wait(timeout=45)
            except subprocess.TimeoutExpired:
                raise AssertionError('Force-close installer remained blocked: '+(folder/'fixed.log').read_text(errors='replace')[-2500:])
            assert code==0,(code,(folder/'fixed.log').read_text(errors='replace')[-2500:])
            child.wait(timeout=10)
            assert digest(fixture)==digest(folder/'1.0.1.0.exe'),'The actual executable was not replaced'
            report['checks'].append(dict(name='confirmed close terminates leftover process and replaces executable',ok=True))
            ready.unlink();shutil.copy2(folder/'1.0.0.0.exe',fixture)
            parent=subprocess.Popen([str(fixture),str(ready)]);processes.append(parent)
            deadline=time.monotonic()+10
            while not ready.exists() and time.monotonic()<deadline:
                if parent.poll() is not None:raise RuntimeError('Online-update fixture exited before readiness')
                time.sleep(.05)
            assert ready.exists()
            online_folder=folder/'online';online_folder.mkdir()
            definition=folder/'online.iss'
            online=(folder/(mode+'.iss')).read_text(encoding='utf8').replace(str(folder/mode),str(online_folder))
            online+='\n[Code]\n'+script.split('[Code]',1)[1]
            definition.write_text(online,encoding='utf8')
            compiled=subprocess.run([compiler,str(definition)],capture_output=True,timeout=40)
            assert compiled.returncode==0,compiled.stdout.decode(errors='replace')[-1500:]
            started=time.monotonic()
            installer=subprocess.Popen([str(online_folder/'close-test.exe'),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',
                '/FDOnlineUpdate=1','/FDParentPid='+str(parent.pid),'/LOG='+str(folder/'online.log')]);processes.append(installer)
            code=installer.wait(timeout=75);elapsed=time.monotonic()-started
            assert code==0,(code,(folder/'online.log').read_text(errors='replace')[-2500:])
            parent.wait(timeout=10)
            assert elapsed>=29 and 'continuing to Restart Manager' in (folder/'online.log').read_text(errors='replace')
            assert digest(fixture)==digest(folder/'1.0.1.0.exe')
            report['checks'].append(dict(name='production online-update guard lets Restart Manager replace a stuck older parent',
                                         ok=True,grace_period_elapsed=True,installer_exit=code))
            # With all pre-install pages disabled, only the production final
            # page decision remains. No /SILENT flag may hide a broken Finish.
            assert 'Parameters: "--updated"; Flags: nowait; Check: IsOnlineUpdate' in script
            automatic=folder/'automatic';automatic.mkdir()
            launched=folder/'automatic-relaunched.txt'
            body=(folder/'online.iss').read_text(encoding='utf8').replace(str(online_folder),str(automatic))
            body=body.replace('DisableDirPage=yes','DisableDirPage=yes\nDisableWelcomePage=yes\nDisableReadyPage=yes')
            body=body.replace('[Code]',f'[Run]\nFilename: "{{app}}\\FlickDock.exe"; Parameters: """{launched}"" --updated"; Flags: nowait; Check: IsOnlineUpdate\n\n[Code]')
            page_state=folder/'automatic-page.txt'
            body+=f'''\nprocedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpFinished then
    SaveStringToFile('{page_state}', 'FINISHED', False)
  else if (CurPageID <> wpPreparing) and (CurPageID <> wpInstalling) then
    SaveStringToFile('{page_state}', IntToStr(CurPageID) + #13#10 + IntToStr(WizardForm.NextButton.Handle), False);
end;
'''
            automatic_script=folder/'automatic.iss';automatic_script.write_text(body,encoding='utf8')
            compiled=subprocess.run([compiler,str(automatic_script)],capture_output=True,timeout=40)
            assert compiled.returncode==0,compiled.stdout.decode(errors='replace')[-1500:]
            finish=subprocess.Popen([str(automatic/'close-test.exe'),'/NORESTART','/SP-',
                '/FDOnlineUpdate=1','/FDParentPid=2147483647','/LOG='+str(folder/'automatic.log')]);processes.append(finish)
            import ctypes
            from ctypes import wintypes
            ctypes.windll.user32.PostMessageW.argtypes=[wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]
            ctypes.windll.user32.PostMessageW.restype=wintypes.BOOL
            advanced=set();deadline=time.monotonic()+25
            while finish.poll() is None and time.monotonic()<deadline:
                if page_state.exists():
                    state=page_state.read_text();assert state!='FINISHED','Online update displayed a Finish page'
                    if state not in advanced:
                        advanced.add(state);ctypes.windll.user32.PostMessageW(int(state.splitlines()[1]),0x00F5,0,0)
                time.sleep(.05)
            assert finish.wait(timeout=1)==0,'Online update waited for a Finish click'
            deadline=time.monotonic()+8
            while not launched.exists() and time.monotonic()<deadline:time.sleep(.05)
            assert launched.exists() and '--updated' in launched.read_text()
            report['checks'].append(dict(name='online update skips Finish and launches the replacement automatically',ok=True))
            # The launched fixture is the only process owning this exact file.
            fixture_pid=int(launched.read_text().splitlines()[0])
            subprocess.run(['taskkill','/PID',str(fixture_pid),'/T','/F'],capture_output=True,timeout=15)
            report['ok']=True
        finally:
            for process in reversed(processes):stop(process)
            report['logs']={name:(folder/name).read_text(errors='replace')[-5000:] for name in ('legacy.log','fixed.log','online.log','automatic.log') if (folder/name).exists()}
            output.write_text(json.dumps(report,indent=2),encoding='utf8')
    print(json.dumps({key:value for key,value in report.items() if key!='logs'},indent=2))


if __name__=='__main__':main()
