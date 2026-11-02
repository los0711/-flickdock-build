"""Private cleanup helpers never construct a GUI, relocate or start updates."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parent
HARNESS=r'''
import importlib.abc,json,runpy,sys,types
path=sys.argv[1];arguments=json.loads(sys.argv[2]);sys.argv=[path,*arguments]
helper=types.ModuleType('clipboard_cleanup_worker')
def main(args):
 print(json.dumps(dict(cleaner=True,args=args)));return 47
helper.main=main;sys.modules['clipboard_cleanup_worker']=helper
class NoGUI(importlib.abc.MetaPathFinder):
 def find_spec(self,name,path=None,target=None):
  if name.startswith(('PySide6','platform_native','surface_morph','mac_updates')):
   raise AssertionError('GUI/update/relocation import before private dispatch: '+name)
sys.meta_path.insert(0,NoGUI())
runpy.run_path(path,run_name='__main__')
'''


class ClipboardCleanupEntryTests(unittest.TestCase):
    def entry(self,name,args):
        return subprocess.run([sys.executable,'-c',HARNESS,str(ROOT/name),json.dumps(args)],
            stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=8,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))

    def test_both_private_entries_forward_exact_arguments_before_gui_import(self):
        arguments=['--request','owned-request.json','--token','a'*32,'--parent-pid','123','--deadline-ms','2000']
        for entry in ('flickdock.py','mac_entry.py'):
            with self.subTest(entry=entry):
                result=self.entry(entry,['--clipboard-cleaner',*arguments])
                self.assertEqual(result.returncode,47,result.stderr)
                self.assertEqual(json.loads(result.stdout),dict(cleaner=True,args=arguments))

    def test_mixed_private_purpose_cannot_fall_through_to_other_helpers_or_gui(self):
        for entry in ('flickdock.py','mac_entry.py'):
            for prefix in ('--clipboard-reader','--apply-mac-update','--verify-platform'):
                with self.subTest(entry=entry,prefix=prefix):
                    result=self.entry(entry,[prefix,'--clipboard-cleaner'])
                    self.assertEqual(result.returncode,2,result.stderr)
                    self.assertEqual(result.stdout,'')

    def test_empty_private_arguments_are_owned_by_worker_parser(self):
        for entry in ('flickdock.py','mac_entry.py'):
            with self.subTest(entry=entry):
                result=self.entry(entry,['--clipboard-cleaner'])
                self.assertEqual(result.returncode,47,result.stderr)
                self.assertEqual(json.loads(result.stdout)['args'],[])


if __name__=='__main__':unittest.main()
