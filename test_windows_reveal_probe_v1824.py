"""Portable probe validation. All DLLs/COM are fake; no Explorer or desktop."""
import ctypes,importlib.util,json,os,tempfile,threading,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('reveal_probe',Path(__file__).parent/'qa/packaged-live/ci_windows_reveal_probe.py')
probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)


class Fn:
    def __init__(self,fn):self.fn=fn
    def __call__(self,*args):return self.fn(*args)


class API:
    def __init__(self,open_hr=0):
        self.calls=[];self.original=0x123456789ABC;self.parent=0x23456789ABCD;self.child=0x123456789AC0
        def record(name,result):
            def call(*args):self.calls.append((name,args));return result
            return Fn(call)
        def parse(path,context,out,flags,attrs):
            self.calls.append(('parse',(path,)));ctypes.cast(out,ctypes.POINTER(ctypes.c_void_p)).contents.value=self.original;return 0
        self.shell=SimpleNamespace(SHParseDisplayName=Fn(parse),SHOpenFolderAndSelectItems=record('open',open_hr),
            ILClone=record('clone',self.parent),ILFindLastID=record('child',self.child),ILRemoveLastID=record('remove',1))
        self.ole=SimpleNamespace(CoInitializeEx=record('init',0),CoUninitialize=record('uninit',None),CoTaskMemFree=record('free',None))
    def names(self):return [n for n,_ in self.calls]


class ProbeTests(unittest.TestCase):
    def run_native(self,mode,api,pump=None):
        release=threading.Event();returned=threading.Event();done=threading.Event();state={}
        if pump is None:release.set()
        pythoncom=SimpleNamespace(PumpWaitingMessages=lambda:(pump(),release.set()))
        with patch.object(ctypes,'WinDLL',side_effect=[api.shell,api.ole],create=True),patch.dict('sys.modules',{'pythoncom':pythoncom}):
            probe._native_call(mode,Path('C:/Unicode 空格目录/目标 tiny 样本.bin'),release,returned,done,state,threading.Lock())
        self.assertTrue(returned.is_set());self.assertTrue(done.is_set());return state
    def test_nonhosted_environment_is_rejected_before_loading_any_native_library(self):
        with patch.dict(os.environ,{},clear=True),patch.object(ctypes,'WinDLL',side_effect=AssertionError('native call'),create=True):
            with self.assertRaises(RuntimeError):probe.require_hosted()
    def test_zero_mode_uses_absolute_item_and_releases_before_com_shutdown(self):
        api=API();state=self.run_native('absolute-zero',api);self.assertNotIn('error',state)
        args=next(args for name,args in api.calls if name=='open');self.assertEqual(args[0].value,api.original);self.assertEqual(args[1:],(0,None,0))
        self.assertEqual(api.names(),['init','parse','open','free','uninit'])
    def test_parent_mode_clones_parent_borrows_child_and_supplies_one_child(self):
        api=API();state=self.run_native('parent-child-one',api);self.assertNotIn('error',state)
        args=next(args for name,args in api.calls if name=='open');self.assertEqual(args[0].value,api.parent)
        self.assertEqual(args[1],1);self.assertEqual(args[2][0],api.child);self.assertEqual(args[3],0)
        self.assertEqual([args[0].value for name,args in api.calls if name=='free'],[api.parent,api.original])
        self.assertNotIn(api.child,[args[0].value for name,args in api.calls if name=='free'])
    def test_pump_mode_keeps_only_com_alive_after_pidls_have_been_freed(self):
        api=API();names_at_pump=[]
        state=self.run_native('absolute-zero-sta-pump',api,pump=lambda:names_at_pump.append(api.names()))
        self.assertEqual(names_at_pump,[['init','parse','open','free']]);self.assertEqual(state['pump_iterations'],1)
        self.assertEqual(api.names()[-1],'uninit')
    def test_hresult_failure_is_reported_without_visual_selection_claim(self):
        api=API(open_hr=0x80070005);state=self.run_native('parent-child-one',api)
        self.assertIn('0x80070005',state['error']);self.assertNotIn('selection_observed',state)
        self.assertEqual(api.names().count('free'),2);self.assertEqual(api.names().count('uninit'),1)
    def test_snapshot_reads_exact_folder_and_never_selects_or_exports_foreign_folder(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)/'Unicode 空格目录';folder.mkdir();target=folder/'目标 tiny 样本.bin';target.write_bytes(b'original')
            def item(path,selected,hwnd):
                selected_items=SimpleNamespace(Count=len(selected),Item=lambda i:SimpleNamespace(Path=selected[i]))
                return SimpleNamespace(HWND=hwnd,Document=SimpleNamespace(Folder=SimpleNamespace(Self=SimpleNamespace(Path=path)),SelectedItems=lambda:selected_items))
            shell=SimpleNamespace(Windows=lambda:[item('C:/foreign/private',[],1),item(str(folder),[str(target)],2)])
            rows=probe.snapshot(shell,folder,target,{1});self.assertEqual(len(rows),1);self.assertTrue(rows[0]['exact_target_selected']);self.assertTrue(rows[0]['new_hwnd'])
            self.assertNotIn('foreign',json.dumps(rows));self.assertEqual(target.read_bytes(),b'original')


if __name__=='__main__':unittest.main(verbosity=2)
