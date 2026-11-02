"""Helper dispatch precedes GUI imports; native clipboard is never accessed."""
import json,os,tempfile
from pathlib import Path
import subprocess,sys,unittest

ROOT=Path(__file__).resolve().parent
class EarlyEntry(unittest.TestCase):
    def run_entry(self,platform,args):
        harness='''
import runpy,sys,types
root,platform,args=sys.argv[1],sys.argv[2],__import__('json').loads(sys.argv[3])
from pathlib import Path
sys.path.insert(0,str(Path(root).parent))
from win_clipboard_native import parse_cache_arguments
sys.platform=platform
sys.argv=[root,*args]
poison=types.ModuleType('surface_morph')
def fail(name):raise AssertionError('GUI import reached before helper dispatch')
poison.__getattr__=fail
sys.modules['surface_morph']=poison
helper=types.ModuleType('win_clipboard_native')
def main(args):
    try:parse_cache_arguments(args)
    except (ValueError,OSError):return 2
    return 37
helper.main=main
sys.modules['win_clipboard_native']=helper
runpy.run_path(root,run_name='__main__')
'''
        return subprocess.run([sys.executable,'-c',harness,str(ROOT/'flickdock.py'),platform,json.dumps(args)],
            capture_output=True,timeout=10,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    def test_windows_exact_reader_dispatch_does_not_construct_gui(self):
        result=self.run_entry('win32',['--clipboard-reader'])
        self.assertEqual(result.returncode,37,result.stderr)
        self.assertEqual(result.stdout,b'')
    def test_other_platform_and_extra_arguments_do_not_reach_helper_or_gui(self):
        for platform,args in [('darwin',['--clipboard-reader']),('win32',['--clipboard-reader','--verify-cloud'])]:
            with self.subTest(platform=platform,args=args):
                result=self.run_entry(platform,args)
                self.assertEqual(result.returncode,2,result.stderr)
                self.assertEqual(result.stdout,b'')

    def test_private_cache_arguments_reach_pure_helper_without_gui(self):
        nonce='a'*32
        root=Path(tempfile.gettempdir())/'FlickDock-native-originals-v1'/nonce
        args=['--clipboard-reader','--native-cache-root',str(root),
              '--native-cache-token',nonce,'--native-cache-instance','b'*32]
        result=self.run_entry('win32',args)
        self.assertEqual(result.returncode,37,result.stderr)
        self.assertEqual(result.stdout,b'')
        for extra in (['--verify-cloud'],['--native-cache-token',nonce]):
            result=self.run_entry('win32',args+extra)
            self.assertEqual(result.returncode,2,result.stderr)
            self.assertEqual(result.stdout,b'')

if __name__=='__main__':unittest.main()
