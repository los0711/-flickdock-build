"""Shell ABI/ownership and bounded requests. Never opens real Explorer."""
import ctypes,json,os,subprocess,sys,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtWidgets import QApplication,QWidget
from shiboken6 import delete
import windows_reveal as reveal
APP=QApplication.instance() or QApplication([])


def until(check):
    end=time.monotonic()+3
    while not check() and time.monotonic()<end:APP.processEvents();time.sleep(.002)
    if not check():raise AssertionError('Bounded reveal condition not met')


class Function:
    def __init__(self,fn):self.fn=fn
    def __call__(self,*args):return self.fn(*args)


class API:
    def __init__(self,init=0,parse=0,opened=0,pointer=0x123456789ABC,cancel_parse=None):
        self.calls=[]
        def record(name,result):
            def call(*args):self.calls.append((name,args));return result
            return Function(call)
        def parse_call(path,context,pidl,flags,attrs):
            self.calls.append(('parse',(path,context,flags,attrs)))
            ctypes.cast(pidl,ctypes.POINTER(ctypes.c_void_p)).contents.value=pointer
            if cancel_parse is not None:cancel_parse.set()
            return parse
        self.ole=SimpleNamespace(CoInitializeEx=record('init',init),CoUninitialize=record('uninit',None),CoTaskMemFree=record('free',None))
        self.shell=SimpleNamespace(SHParseDisplayName=Function(parse_call),SHOpenFolderAndSelectItems=record('open',opened))
    def names(self):return [name for name,_ in self.calls]


class ShellTests(unittest.TestCase):
    def select(self,api,path=r'C:\original 中文, space\file.txt',cancel=None):
        with patch.object(reveal,'_load_api',return_value=(api.shell,api.ole)),patch.object(reveal,'_prepare_automation',return_value=('typed-client',)),patch.object(reveal,'_select_loaded_view',return_value=True):
            return reveal.select_file(path,cancel or threading.Event())
    def test_unicode_long_path_absolute_item_and_pointer_width_are_preserved(self):
        api=API();path='\\\\?\\C:\\Unicode 中文, space\\'+'x'*280+'.txt'
        self.assertTrue(self.select(api,path));self.assertEqual(api.names(),['init','parse','open','free','uninit'])
        self.assertEqual(api.calls[1][1][0],path)
        args=api.calls[2][1];self.assertEqual(args[0].value,0x123456789ABC);self.assertEqual(args[1:],(0,None,0))
        self.assertEqual(api.calls[3][1][0].value,0x123456789ABC)
    def test_com_s_false_is_balanced_but_changed_apartment_is_not_uninitialized(self):
        for code,uninit in ((1,1),(0x80010106,0),(-2147417850,0)):
            api=API(init=code);self.assertTrue(self.select(api));self.assertEqual(api.names().count('uninit'),uninit)
    def test_parse_and_open_failure_preserve_hresult_and_free_once(self):
        for operation,kwargs in (('SHParseDisplayName',dict(parse=0x80070002)),('SHOpenFolderAndSelectItems',dict(opened=-2147024891))):
            api=API(**kwargs)
            with self.assertRaisesRegex(OSError,operation+'.*HRESULT 0x8007'):self.select(api)
            self.assertEqual(api.names().count('free'),1);self.assertEqual(api.names().count('uninit'),1)
            if operation=='SHParseDisplayName':self.assertNotIn('open',api.names())
    def test_init_failure_does_not_parse_or_uninitialize(self):
        api=API(init=0x80004005)
        with self.assertRaisesRegex(OSError,'CoInitializeEx.*80004005'):self.select(api)
        self.assertEqual(api.names(),['init'])
    def test_null_pidl_and_cancellation_cannot_claim_selection_success(self):
        api=API(pointer=0)
        with self.assertRaisesRegex(OSError,'未返回'):self.select(api)
        self.assertNotIn('open',api.names());self.assertNotIn('free',api.names());self.assertEqual(api.names()[-1],'uninit')
        cancel=threading.Event();api=API(cancel_parse=cancel)
        self.assertFalse(self.select(api,cancel=cancel));self.assertEqual(api.names(),['init','parse','free','uninit'])
    def test_invalid_input_and_pre_cancel_never_load_shell(self):
        with patch.object(reveal,'_load_api',side_effect=AssertionError('native call')):
            for path in ('relative.txt','C:\\invalid\0name','\\root-relative.txt'):
                with self.assertRaises(OSError):reveal.select_file(path,threading.Event())
            cancel=threading.Event();cancel.set();self.assertFalse(reveal.select_file('C:\\original.txt',cancel))
    def test_declared_abi_uses_hresult32_dword32_and_native_pointer_width(self):
        api=API()
        with patch.object(reveal.sys,'platform','win32'),patch.object(ctypes,'WinDLL',side_effect=[api.shell,api.ole],create=True):reveal._load_api()
        self.assertEqual(ctypes.sizeof(api.ole.CoInitializeEx.restype),4)
        self.assertEqual(ctypes.sizeof(api.shell.SHOpenFolderAndSelectItems.argtypes[1]),4)
        self.assertIs(api.shell.SHParseDisplayName.argtypes[2]._type_,ctypes.c_void_p)
    def test_automation_prepare_only_caches_dynamic_client_without_creating_shell(self):
        calls=[];pythoncom=SimpleNamespace();dispatch=lambda *args:calls.append(args)
        modules={'pythoncom':pythoncom,'win32com':SimpleNamespace(),'win32com.client':SimpleNamespace(),
                 'win32com.client.dynamic':SimpleNamespace(Dispatch=dispatch)}
        with patch.dict('sys.modules',modules),patch.object(reveal,'_automation',None):
            first=reveal._prepare_automation();self.assertIs(reveal._prepare_automation(),first)
        self.assertEqual(first,(pythoncom,dispatch));self.assertEqual(calls,[])


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.prepare=patch.object(reveal,'_prepare_automation',return_value=('typed-client',));self.prepare.start();self.addCleanup(self.prepare.stop)
        self.owner=QWidget();self.owner.show();APP.processEvents()
    def tearDown(self):self.owner.hide();delete(self.owner);APP.processEvents()
    def test_hundred_different_clicks_keep_one_active_and_only_latest_pending(self):
        entered=threading.Event();release=threading.Event();starts=[];seen=[];gui=threading.get_ident()
        def selected(path,cancel):
            starts.append(path)
            if len(starts)==1:entered.set();release.wait(3)
            return not cancel.is_set()
        with patch.object(reveal,'select_file',side_effect=selected):
            reveal.request_reveal(self.owner,'C:\\first.txt',lambda e:seen.append(('obsolete',e)))
            self.assertTrue(entered.wait(1))
            for index in range(100):reveal.request_reveal(self.owner,f'C:\\next-{index}.txt',lambda e,index=index:seen.append((index,e,threading.get_ident())))
            lane=self.owner._windows_reveal_lane;self.assertEqual(len(starts),1);self.assertEqual(lane.pending['path'],'C:\\next-99.txt')
            release.set();until(lambda:lane.active is None and lane.pending is None)
        self.assertEqual(starts,['C:\\first.txt','C:\\next-99.txt']);self.assertEqual(seen,[(99,None,gui)])
    def test_missing_automation_dependency_reports_failure_without_starting_worker(self):
        seen=[]
        with patch.object(reveal,'_prepare_automation',side_effect=ImportError('component missing')):
            reveal.request_reveal(self.owner,'C:\\original.txt',seen.append)
        self.assertEqual(len(seen),1);self.assertIsInstance(seen[0],OSError);self.assertIn('无法加载',str(seen[0]))
        self.assertFalse(hasattr(self.owner,'_windows_reveal_lane'))
    def test_identical_clicks_share_active_request_and_last_completion(self):
        entered=threading.Event();release=threading.Event();seen=[];calls=[]
        def selected(path,cancel):calls.append(path);entered.set();release.wait(3);return True
        with patch.object(reveal,'select_file',side_effect=selected):
            for index in range(50):reveal.request_reveal(self.owner,'C:\\same.txt',lambda e,index=index:seen.append(index))
            self.assertTrue(entered.wait(1));self.assertIsNone(self.owner._windows_reveal_lane.pending)
            release.set();until(lambda:self.owner._windows_reveal_lane.active is None)
        self.assertEqual(calls,['C:\\same.txt']);self.assertEqual(seen,[49])
    def test_completion_reentrant_second_then_third_keeps_only_one_active_native_call(self):
        second=threading.Event();release=threading.Event();starts=[];seen=[];counts=dict(active=0,maximum=0);lock=threading.Lock()
        def selected(path,cancel):
            with lock:
                starts.append(path);counts['active']+=1;counts['maximum']=max(counts['maximum'],counts['active'])
            try:
                if path=='C:\\second.txt':second.set();release.wait(3)
                return not cancel.is_set()
            finally:
                with lock:counts['active']-=1
        def first_done(error):
            reveal.request_reveal(self.owner,'C:\\second.txt',lambda error:seen.append('obsolete-second'))
            self.assertTrue(second.wait(1))
            reveal.request_reveal(self.owner,'C:\\third.txt',lambda error:seen.append('latest-third'))
        with patch.object(reveal,'select_file',side_effect=selected):
            try:
                reveal.request_reveal(self.owner,'C:\\first.txt',first_done);until(lambda:second.is_set())
                self.assertEqual(starts,['C:\\first.txt','C:\\second.txt'])
                self.assertEqual(self.owner._windows_reveal_lane.pending['path'],'C:\\third.txt')
            finally:
                release.set();until(lambda:self.owner._windows_reveal_lane.active is None and self.owner._windows_reveal_lane.pending is None)
        self.assertEqual(counts['maximum'],1);self.assertEqual(starts,['C:\\first.txt','C:\\second.txt','C:\\third.txt'])
        self.assertEqual(seen,['latest-third'])
    def test_hidden_owner_cancels_pending_does_not_wait_for_hung_shell_or_emit_status(self):
        entered=threading.Event();release=threading.Event();starts=[];seen=[]
        def selected(path,cancel):starts.append(path);entered.set();release.wait(3);return True
        with patch.object(reveal,'select_file',side_effect=selected):
            reveal.request_reveal(self.owner,'C:\\hang.txt',seen.append);self.assertTrue(entered.wait(1))
            reveal.request_reveal(self.owner,'C:\\pending.txt',seen.append)
            self.owner.hide();lane=self.owner._windows_reveal_lane
            reveal.request_reveal(self.owner,'C:\\after-hide.txt',seen.append)
            self.assertTrue(lane.active['worker'].is_alive());self.assertTrue(lane.active['worker'].daemon);self.assertIsNone(lane.pending)
            release.set();until(lambda:lane.active is None)
        self.assertEqual(starts,['C:\\hang.txt']);self.assertEqual(seen,[])
    def test_real_hresult_error_reaches_callback_and_destroyed_owner_is_silent(self):
        error=OSError('SHOpenFolderAndSelectItems 失败 (HRESULT 0x80070005)');seen=[]
        with patch.object(reveal,'select_file',side_effect=error):
            reveal.request_reveal(self.owner,'C:\\original.txt',seen.append);until(lambda:len(seen)==1)
        self.assertIs(seen[0],error)
        spare=QWidget();spare.show();entered=threading.Event();release=threading.Event()
        def selected(path,cancel):entered.set();release.wait(3);return True
        with patch.object(reveal,'select_file',side_effect=selected):
            reveal.request_reveal(spare,'C:\\original.txt',seen.append);self.assertTrue(entered.wait(1));lane=spare._windows_reveal_lane
            delete(spare);release.set();until(lambda:lane.active is None)
        self.assertEqual(len(seen),1)
    def test_app_exit_does_not_join_a_stuck_shell_worker(self):
        script='''import threading
from PySide6.QtWidgets import QApplication,QWidget
import windows_reveal as w
app=QApplication([]);owner=QWidget();owner.show()
w._prepare_automation=lambda:None
started=threading.Event()
def stuck(path,cancel):started.set();threading.Event().wait(60)
w.select_file=stuck
w.request_reveal(owner,'C:\\\\offline.txt');assert started.wait(1)
owner.close();app.quit();print('EXIT_WITH_HUNG_DAEMON',flush=True)
'''
        result=subprocess.run([sys.executable,'-X','utf8','-c',script],cwd=Path(__file__).parent,
            capture_output=True,text=True,timeout=8,env=dict(os.environ,QT_QPA_PLATFORM='offscreen'))
        self.assertEqual(result.returncode,0,result.stderr);self.assertIn('EXIT_WITH_HUNG_DAEMON',result.stdout)


class ComError(Exception):
    def __init__(self,hresult):super().__init__('COM error');self.hresult=hresult


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.path=r'C:\folder 中文\target file.bin';self.clock=0.;self.calls=[];self.selected=[];self.cancelled=False;self.reads=0
        self.cancel=SimpleNamespace(is_set=lambda:self.cancelled,wait=self.wait)
        self.item=SimpleNamespace(Path=self.path)
        def select(item,flags):self.calls.append((item,flags));self.selected=[item.Path]
        def items():
            self.reads+=1;return SimpleNamespace(Count=len(self.selected),Item=lambda i:SimpleNamespace(Path=self.selected[i]))
        self.document=SimpleNamespace(Folder=SimpleNamespace(Self=SimpleNamespace(Path=r'C:\folder 中文'),ParseName=lambda _:self.item),
            SelectItem=select,SelectedItems=items)
        self.shell=SimpleNamespace(Windows=lambda:[SimpleNamespace(Document=self.document)])
        self.pumps=[];self.client=(SimpleNamespace(com_error=ComError,PumpWaitingMessages=lambda:self.pumps.append(True)),lambda _:self.shell)
        self.timer=patch.object(reveal.time,'monotonic',side_effect=lambda:self.clock);self.timer.start();self.addCleanup(self.timer.stop)
    def wait(self,_):self.clock+=1
    def run_view(self):return reveal._select_loaded_view(self.path,self.cancel,6.,self.client)
    def test_typed_folderitem_selected_once_and_exact_state_confirmed(self):
        self.assertTrue(self.run_view());self.assertEqual(self.calls,[(self.item,29)]);self.assertEqual(self.reads,1)
    def test_readiness_retries_but_selection_is_never_repeated_while_confirmation_waits(self):
        attempts=[]
        def windows():
            attempts.append(True)
            if len(attempts)==1:raise ComError(-2147483638)
            return [SimpleNamespace(Document=self.document)]
        self.shell.Windows=windows
        def items():
            self.reads+=1;return SimpleNamespace(Count=1 if self.reads>=3 else 0,Item=lambda i:SimpleNamespace(Path=self.path))
        self.document.SelectedItems=items
        self.assertTrue(self.run_view());self.assertEqual(len(self.calls),1);self.assertEqual(len(attempts),2);self.assertEqual(self.reads,3)
    def test_shell_acceptance_without_real_selection_is_timeout_not_success(self):
        self.document.SelectItem=lambda item,flags:self.calls.append((item,flags))
        with self.assertRaisesRegex(TimeoutError,'确认文件选中'):self.run_view()
        self.assertEqual(len(self.calls),1);self.assertLessEqual(self.clock,6.)
    def test_wrong_folder_wrong_parsed_path_and_duplicate_view_are_not_selected(self):
        self.document.Folder.ParseName=lambda _:SimpleNamespace(Path=r'C:\folder 中文\wrong.bin')
        with self.assertRaisesRegex(OSError,'不一致'):self.run_view()
        self.assertEqual(self.calls,[])
        self.document.Folder.ParseName=lambda _:self.item
        self.shell.Windows=lambda:[SimpleNamespace(Document=self.document)]*2
        with self.assertRaisesRegex(OSError,'多个'):self.run_view()
        self.assertEqual(self.calls,[])
        self.shell.Windows=lambda:[SimpleNamespace(Document=SimpleNamespace(Folder=SimpleNamespace(Self=SimpleNamespace(Path=r'C:\other'))))]
        with self.assertRaises(TimeoutError):self.run_view()
        self.assertEqual(self.calls,[])
    def test_selectitem_hresult_is_visible_and_never_retried(self):
        def fail(item,flags):self.calls.append((item,flags));raise ComError(-2147024891)
        self.document.SelectItem=fail
        with self.assertRaisesRegex(OSError,'0x80070005'):self.run_view()
        self.assertEqual(len(self.calls),1)
    def test_cancel_before_dispatch_and_during_parse_prevents_selection(self):
        self.cancelled=True
        with patch.dict('sys.modules',{}):self.assertFalse(self.run_view())
        self.cancelled=False
        def parse(_):self.cancelled=True;return self.item
        self.document.Folder.ParseName=parse;self.assertFalse(self.run_view());self.assertEqual(self.calls,[])
    def test_extended_path_spelling_compares_to_same_resolved_item(self):
        original=self.path;self.path='\\\\?\\'+original
        # The real Windows realpath resolves the prefix; avoid local filesystem
        # lookup differences while exercising the helper's explicit prefix rule.
        with patch.object(reveal.os.path,'realpath',side_effect=lambda value:value):self.assertTrue(self.run_view())


class EntryTests(unittest.TestCase):
    def test_actual_history_and_details_route_exact_original_to_shell_helper_and_show_errors(self):
        import test_html_native_workspace as ws
        from test_html_native_lan import FakeLan
        from app_settings import SettingsStore
        from lan_panel import LanDialog
        f=ws.WorkspaceTests();f.setUp();dialog=None
        try:
            dialog=LanDialog(FakeLan(),SettingsStore(f.base/'lan'));dialog.show();ws.flush();panel=dialog.panel
            record=dict(name='original.txt',peer='QA',state='完成',direction='send',when='now',paths=[f.paths[0]])
            panel._history=[record];panel._render_history();ws.flush()
            with patch.object(panel.file_metadata,'request',return_value={'exists':True}),patch('windows_reveal.request_reveal') as request,patch('subprocess.Popen') as launch,patch.object(sys,'platform','win32'):
                panel.history_rows[0].reveal_button.click();self.assertEqual(request.call_args.args[:2],(panel,f.paths[0]));launch.assert_not_called()
                request.call_args.args[2](OSError('HRESULT 0x80070005'));self.assertIn('0x80070005',panel.status_label.text())
            view=f.overview();view.select_item(f.paths[0]);ws.flush()
            with patch('windows_reveal.request_reveal') as request,patch('subprocess.Popen') as launch,patch.object(sys,'platform','win32'):
                f.shelf.reveal_ref(f.paths[0]);self.assertEqual(request.call_args.args[:2],(f.shelf,str(Path(f.paths[0]).resolve())));launch.assert_not_called()
                request.call_args.args[2](OSError('HRESULT 0x80070002'));self.assertIn('0x80070002',view.preview.info.accessibleName())
                self.assertTrue(view.preview.info.text().startswith('无法打开文件所在位置'))
            self.assertEqual(Path(f.paths[0]).read_bytes(),b'original 0')
        finally:
            if dialog is not None:dialog.hide();dialog.deleteLater();ws.flush()
            f.tearDown()


if __name__=='__main__':unittest.main(verbosity=2)
