"""Startup must leave optional native runtimes unloaded and avoid Windows WMI."""
import json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QSysInfo
from update_client import update_architecture

ROOT=Path(__file__).parent

class StartupResourceTests(unittest.TestCase):
    def test_idle_import_does_not_load_media_runtime_or_other_platform_updater(self):
        code="import sys,json;import flickdock;print(json.dumps([n for n in sys.modules if n.startswith('PySide6.QtMultimedia') or n=='mac_updates']))"
        with tempfile.TemporaryDirectory(prefix='flickdock-idle-import-') as folder:
            result=subprocess.run([sys.executable,'-c',code],cwd=ROOT,capture_output=True,text=True,timeout=20,
                env=dict(os.environ,QT_QPA_PLATFORM='offscreen',APPDATA=folder,LOCALAPPDATA=folder))
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),[])

    def test_update_selection_uses_native_cpu_information_without_wmi(self):
        import platform
        with patch.object(platform,'machine',side_effect=AssertionError('WMI must not run on startup')):
            self.assertEqual(update_architecture(),QSysInfo.currentCpuArchitecture().lower())
            self.assertEqual(update_architecture('AMD64'),'x86_64')
            self.assertEqual(update_architecture('aarch64'),'arm64')


if __name__=='__main__':unittest.main()
