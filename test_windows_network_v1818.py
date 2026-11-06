"""Firewall launch contract with a fake shell; never edits machine policy."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import os,subprocess,tempfile,unittest,sys
import windows_network

class WindowsNetworkTests(unittest.TestCase):
    def test_consent_cancel_and_success_use_only_frozen_install_paths(self):
        with tempfile.TemporaryDirectory(prefix='FlickDock 网络 test ') as directory:
            root=Path(directory);(root/'windows-network.ps1').write_text('# fixture',encoding='utf8')
            for status in (5,42):
                execute=Mock(return_value=status)
                with patch.object(windows_network.sys,'platform','win32'),patch.object(windows_network.sys,'frozen',True,create=True),patch.object(windows_network.sys,'_MEIPASS',directory,create=True),patch.object(windows_network.sys,'executable',str(root/'FlickDock.exe')),patch.object(windows_network.ctypes,'windll',SimpleNamespace(shell32=SimpleNamespace(ShellExecuteW=execute)),create=True),patch.dict('os.environ',{'SystemRoot':'C:/Windows'}):
                    self.assertEqual(windows_network.repair_network_access(),status>32)
                args=execute.call_args.args
                self.assertEqual(args[1],'runas');self.assertEqual(args[-1],0)
                self.assertEqual(args[3],subprocess.list2cmdline(['-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(root/'windows-network.ps1'),'-InstallDirectory',str(root)]))

    def test_source_and_non_windows_never_launch_or_elevate(self):
        for platform,frozen in (('win32',False),('darwin',True)):
            with patch.object(windows_network.sys,'platform',platform),patch.object(windows_network.sys,'frozen',frozen,create=True),patch.object(windows_network.ctypes,'windll',None,create=True):
                self.assertFalse(windows_network.repair_network_access())

    @unittest.skipUnless(sys.platform=='win32','Windows PowerShell 5.1 parser')
    def test_exact_shipped_script_parses_in_windows_powershell_51(self):
        script=Path(__file__).with_name('windows-network.ps1')
        self.assertTrue(script.read_bytes().startswith(b'\xef\xbb\xbf'),'PowerShell 5.1 requires BOM for Chinese source')
        executable=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
        command="$tokens=$null;$problems=$null;[System.Management.Automation.Language.Parser]::ParseFile('"+str(script).replace("'","''")+"',[ref]$tokens,[ref]$problems)>$null;if($problems){exit 1}"
        result=subprocess.run([str(executable),'-NoProfile','-NonInteractive','-Command',command],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))

    @unittest.skipUnless(sys.platform=='win32','Windows PowerShell 5.1 with isolated fake policy')
    def test_shipped_rules_cover_native_engine_and_retire_old_transport_without_touching_other_apps(self):
        script=Path(__file__).with_name('windows-network.ps1')
        with tempfile.TemporaryDirectory(prefix='FlickDock network rules ') as directory:
            root=Path(directory);kernel=root/'_internal/syncthing/runtime/syncthing.exe';kernel.parent.mkdir(parents=True)
            kernel.touch();(root/'FlickDock.exe').touch()
            command=r'''
class IsolatedRules : System.Collections.IEnumerable {
    [System.Collections.ArrayList]$items=[System.Collections.ArrayList]::new()
    [void] Add([object]$value) { [void]$this.items.Add($value) }
    [void] Remove([string]$name) { foreach($r in @($this.items.ToArray())) { if($r.Name -eq $name) { [void]$this.items.Remove($r) } } }
    [System.Collections.IEnumerator] GetEnumerator() { return $this.items.GetEnumerator() }
}
$global:isolatedPolicy=[pscustomobject]@{Rules=[IsolatedRules]::new()}
function New-Object {
    param([string]$ComObject)
    if($ComObject -eq 'HNetCfg.FwPolicy2') { return $global:isolatedPolicy }
    if($ComObject -ne 'HNetCfg.FWRule') { throw 'No real COM objects are permitted in this test' }
    return [pscustomobject]@{Name='';Grouping='';Description='';ApplicationName='';Protocol=0;Direction=0;RemoteAddresses='';RemotePorts='';Profiles=0;Action=0;Enabled=$false}
}
$install='__ROOT__';$script='__SCRIPT__'
$unrelated=New-Object -ComObject HNetCfg.FWRule
$unrelated.Name='other-application';$unrelated.ApplicationName='E:\unrelated.exe';$unrelated.Enabled=$true;$unrelated.Action=0
$isolatedPolicy.Rules.Add($unrelated)
& $script -InstallDirectory $install -Quiet
if($LASTEXITCODE -ne 0) { throw 'Initial repair failed' }
$first=@($isolatedPolicy.Rules | Where-Object Grouping -Like 'FlickDock-*')
$old=New-Object -ComObject HNetCfg.FWRule
$old.Name=$first[0].Grouping+'-P2P-UDP-In';$old.Grouping=$first[0].Grouping
$old.ApplicationName=Join-Path $install '_internal\p2p\runtime\node.exe';$isolatedPolicy.Rules.Add($old)
& $script -InstallDirectory $install -Quiet
if($LASTEXITCODE -ne 0) { throw 'Repeated repair failed' }
$repaired=@($isolatedPolicy.Rules | ForEach-Object { $_ | Select-Object Name,ApplicationName,Protocol,Direction,Action,RemoteAddresses })
& $script -InstallDirectory $install -Action Remove -Quiet
if($LASTEXITCODE -ne 0) { throw 'Removal failed' }
[pscustomobject]@{repaired=$repaired;remaining=@($isolatedPolicy.Rules | ForEach-Object Name)} | ConvertTo-Json -Depth 5 -Compress
'''.replace('__ROOT__',str(root).replace("'","''")).replace('__SCRIPT__',str(script).replace("'","''"))
            wrapper=root/'verify.ps1';wrapper.write_text(command,encoding='utf-8-sig')
            executable=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
            result=subprocess.run([str(executable),'-NoProfile','-NonInteractive','-File',str(wrapper)],capture_output=True,
                timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
            import json
            value=json.loads(result.stdout.decode('utf-8-sig'))
            rules=[r for r in value['repaired'] if r['ApplicationName']==str(kernel)]
            self.assertEqual({(r['Protocol'],r['Direction']) for r in rules},{(6,1),(6,2),(17,1),(17,2)})
            self.assertTrue(all(r['Action']==1 and r['RemoteAddresses']=='*' for r in rules))
            self.assertFalse(any(r['ApplicationName'].endswith('node.exe') for r in value['repaired']))
            self.assertEqual(value['remaining'],['other-application'])

if __name__=='__main__':unittest.main()
