"""Portable, injected handles only; no native window/process access."""
import ast,inspect,json,unittest
from types import SimpleNamespace
from unittest.mock import Mock
from fixture_editor_lifecycle import FixtureEditorLease,WindowsEditorAPI


class API:
    def __init__(self):
        self.ident=(123,456,r'c:\windows\notepad.exe');self.live=True;self.exists=True
        self.owner=123;self.opened=[];self.closed=[];self.posted=[];self.close_on_post=True
        self.title=b'initial fixture title stamp'
    def open(self,pid):self.opened.append(pid);return 88
    def close(self,handle):self.closed.append(handle)
    def identity(self,handle):return self.ident
    def alive(self,handle):return self.live
    def window_exists(self,hwnd):return self.exists and hwnd==321
    def window_pid(self,hwnd):return self.owner
    def title_stamp(self,hwnd):return self.title
    def post_close(self,hwnd):
        self.posted.append(hwnd)
        if self.close_on_post:self.exists=False


class FixtureEditorTests(unittest.TestCase):
    def make(self,api=None):
        api=api or API();state=dict(guard=True,command=True)
        def guard():
            if not state['guard']:raise RuntimeError('private environment and path')
        lease=FixtureEditorLease.capture(api,pid=123,hwnd=321,image='notepad.exe',
            verify=lambda:state['command'],guard=guard)
        return lease,api,state
    def test_one_verified_window_closes_and_handle_released_once(self):
        lease,api,state=self.make();result=lease.cleanup()
        self.assertEqual(result,dict(status='closed',close_requested=True,handle_released=True))
        self.assertEqual(api.posted,[321]);self.assertEqual(api.closed,[88])
        self.assertEqual(lease.cleanup()['status'],'already_released');self.assertEqual(api.closed,[88])
    def test_identity_birth_image_window_and_command_changes_are_refused(self):
        for change in ('pid','birth','image','owner','title','command','guard'):
            with self.subTest(change=change):
                lease,api,state=self.make()
                if change=='pid':api.ident=(999,456,api.ident[2])
                elif change=='birth':api.ident=(123,999,api.ident[2])
                elif change=='image':api.ident=(123,456,r'c:\private\other.exe')
                elif change=='owner':api.owner=999
                elif change=='title':api.title=b'other document or unsaved title'
                else:state[change]=False
                result=lease.cleanup()
                self.assertIn(result['status'],('identity_changed','unavailable'))
                self.assertFalse(result['close_requested']);self.assertTrue(result['handle_released'])
                self.assertFalse(api.posted);self.assertEqual(api.closed,[88])
                self.assertNotIn('private',json.dumps(result))
    def test_guard_capture_failure_does_not_open_process(self):
        api=API()
        with self.assertRaisesRegex(RuntimeError,'CAPTURE_REFUSED'):
            FixtureEditorLease.capture(api,pid=123,hwnd=321,image='notepad.exe',verify=lambda:True,
                guard=Mock(side_effect=RuntimeError('private token')))
        self.assertFalse(api.opened);self.assertFalse(api.closed);self.assertFalse(api.posted)
    def test_capture_rejection_releases_opened_original_handle(self):
        for change in ('command','owner','image','hwnd'):
            with self.subTest(change=change):
                api=API()
                if change=='owner':api.owner=999
                if change=='image':api.ident=(123,456,r'c:\private\other.exe')
                with self.assertRaisesRegex(RuntimeError,'CAPTURE_REFUSED'):
                    FixtureEditorLease.capture(api,pid=123,hwnd=322 if change=='hwnd' else 321,image='notepad.exe',
                        verify=lambda:change!='command',guard=lambda:None)
                self.assertEqual(api.closed,[88]);self.assertFalse(api.posted)
    def test_already_closed_or_exited_never_posts(self):
        for change in ('window','process'):
            lease,api,state=self.make()
            if change=='window':api.exists=False
            else:api.live=False
            self.assertIn(lease.cleanup()['status'],('already_closed','already_exited'))
            self.assertFalse(api.posted);self.assertEqual(api.closed,[88])
    def test_timeout_never_kills_or_reposts_and_releases_handle(self):
        lease,api,state=self.make();api.close_on_post=False;now=[0.]
        def sleep(seconds):now[0]+=seconds
        result=lease.cleanup(timeout=.1,clock=lambda:now[0],sleep=sleep)
        self.assertEqual(result['status'],'timeout');self.assertEqual(api.posted,[321])
        self.assertEqual(api.closed,[88]);self.assertTrue(result['handle_released'])
        self.assertLessEqual(now[0],.100001)
    def test_command_delay_or_new_identity_during_query_never_posts(self):
        for change in ('elapsed','owner','title'):
            lease,api,state=self.make();now=[0.]
            def verify():
                if change=='elapsed':now[0]=8.
                elif change=='owner':api.owner=999
                else:api.title=b'changed while verifying command'
                return True
            lease.verify=verify;result=lease.cleanup(clock=lambda:now[0])
            self.assertIn(result['status'],('timeout','identity_changed'))
            self.assertFalse(api.posted);self.assertTrue(result['handle_released'])
    def test_query_exception_does_not_leak_path_and_releases_handle(self):
        lease,api,state=self.make();lease.verify=Mock(side_effect=RuntimeError('private path token'))
        result=lease.cleanup();self.assertEqual(result['status'],'unavailable')
        self.assertTrue(result['handle_released']);self.assertFalse(api.posted)
        self.assertNotIn('private',json.dumps(result))
    def test_native_handle_has_only_query_and_synchronize_rights(self):
        api=WindowsEditorAPI.__new__(WindowsEditorAPI);api.k=SimpleNamespace(OpenProcess=Mock(return_value=88))
        self.assertEqual(api.open(123),88);api.k.OpenProcess.assert_called_once_with(0x101000,False,123)
        self.assertFalse(0x101000&1)
    def test_native_title_empty_oversized_or_incomplete_read_is_not_a_proof(self):
        api=WindowsEditorAPI.__new__(WindowsEditorAPI)
        for length in (0,32769):
            api.u=SimpleNamespace(GetWindowTextLengthW=Mock(return_value=length),GetWindowTextW=Mock())
            with self.assertRaises(OSError):api.title_stamp(321)
            api.u.GetWindowTextW.assert_not_called()
        api.u=SimpleNamespace(GetWindowTextLengthW=Mock(return_value=10),GetWindowTextW=Mock(return_value=9))
        with self.assertRaises(OSError):api.title_stamp(321)
    def test_no_editor_termination_keyboard_or_foreground_calls_exist(self):
        source=inspect.getsource(__import__('fixture_editor_lifecycle'))
        for name in ('TerminateProcess','SetForegroundWindow','send_keys','mouse.click','process.kill'):
            self.assertNotIn(name,source)
    def test_actual_finally_keeps_primary_error_and_continues_worker_cleanup(self):
        import ci_packaged_windows_live as qa
        main=next(n for n in ast.parse(inspect.getsource(qa)).body if isinstance(n,ast.FunctionDef) and n.name=='main')
        final=next(n.finalbody for n in main.body if isinstance(n,ast.Try) and n.finalbody)
        block=next(n for n in final if isinstance(n,ast.If) and "'driver'" in ast.unparse(n.test))
        # Execute the real finally block against fake resources, not a rewritten
        # imitation of its primary-error handling or cleanup ordering.
        code=compile(ast.fix_missing_locations(ast.Module(body=block.body,type_ignores=[])),'<actual-QA-cleanup>','exec')
        for failure in ('exception','timeout'):
            report=dict(ok=False,error='existing primary failure',profile_cleanup_error='PermissionError')
            cleanup=Mock(side_effect=RuntimeError('private token')) if failure=='exception' else Mock(return_value=dict(status='timeout',close_requested=True,handle_released=True))
            driver=SimpleNamespace(cleanup_fixture_editor=cleanup,close=Mock())
            exec(code,dict(report=report,driver=driver,phase=lambda label,action:action()))
            self.assertEqual(report['error'],'existing primary failure')
            self.assertEqual(report['profile_cleanup_error'],'PermissionError');self.assertFalse(report['ok'])
            self.assertIn('fixture_editor_cleanup_error',report);driver.close.assert_called_once_with()
            self.assertNotIn('private',json.dumps(report))


if __name__=='__main__':unittest.main()
