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

if __name__=='__main__':unittest.main()
