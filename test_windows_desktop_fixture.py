"""Desktop lifecycle/ABI models only; no real desktop API is invoked."""
import copy,ctypes,unittest
from types import SimpleNamespace
from unittest.mock import patch
from ci_windows_desktop_fixture import DesktopApi,OWN_ACCESS,READ,SWITCH,execute_fixture,fixture_verified,require_hosted_fixture


class FakeDesktop:
    def __init__(self):
        self.calls=[];self.input='Default';self.thread='Default';self.current_session=2;self.station='WinSta0';self.active_session=2
        self.input_access=True;self.switch_ok=True;self.restore_ok=True;self.launch_ok=True;self.waits=[True];self.close_handles=True;self.child_code=0
    def evidence(self,input_handle=None):
        self.calls.append(('evidence',input_handle))
        return dict(pid=50,tid=51,session=self.current_session,active_console_session=self.active_session,station=dict(name=self.station,visible=True),thread_desktop=dict(name=self.thread,receiving_input=self.thread==self.input),
                    input_desktop=dict(name=self.input,receiving_input=True),foreground=dict(pid=40,session=2),windows_product_type=1,windows_build=26200)
    def open_input(self,access):
        self.calls.append(('open_input',access))
        if not self.input_access:raise OSError(5,'No switch access')
        return 20
    def thread_desktop(self):self.calls.append(('thread_desktop',));return 10
    def create_owned(self,name):self.calls.append(('create',name));self.name=name;self.thread=name;return 30
    def desktop(self,handle):return dict(name=self.name,receiving_input=self.input==self.name)
    def switch(self,handle):
        self.calls.append(('switch',handle))
        ok=self.switch_ok if handle==30 else self.restore_ok
        if ok:self.input=self.name if handle==30 else 'Default'
        return ok
    def last_error(self):return 5
    def launch(self,args,desktop):
        self.calls.append(('launch',tuple(args),desktop))
        if not self.launch_ok:raise OSError(5,'CreateProcess refused')
        return dict(process=100,thread=101,pid=102,tid=103)
    def wait(self,child,timeout):self.calls.append(('wait',child['process'],timeout));return self.waits.pop(0) if self.waits else True
    def terminate(self,child):self.calls.append(('terminate',child['process']));return True
    def exit_code(self,child):return self.child_code
    def restore_thread(self,handle):self.calls.append(('restore_thread',handle));self.thread='Default';return True
    def close_desktop(self,handle):self.calls.append(('close_desktop',handle));return True
    def close_child(self,child):self.calls.append(('close_child',child['process'],child['thread']));return dict(thread=self.close_handles,process=self.close_handles)


class DesktopFixtureTests(unittest.TestCase):
    def run_fixture(self,api):
        return execute_fixture(api,['trusted-python','trusted-probe','--child'],{'github_sha':'test-sha'},desktop_name='FlickDockMaterialQA_model')

    def test_hosted_guard_requires_all_explicit_scope_fields(self):
        env=dict(GITHUB_ACTIONS='true',GITHUB_REPOSITORY='los0711/-flickdock-build',GITHUB_REF='refs/heads/qa/material-probe-1823-20261031',RUNNER_ENVIRONMENT='github-hosted',RUNNER_OS='Windows',RUNNER_ARCH='ARM64',FLICKDOCK_MATERIAL_RUNNER_LABEL='windows-11-arm',ImageOS='win11-vs2026-arm64')
        require_hosted_fixture(env,'win32')
        for key in env:
            altered=dict(env);altered[key]='wrong'
            with self.subTest(key=key),self.assertRaises(RuntimeError):require_hosted_fixture(altered,'win32')
        with self.assertRaises(RuntimeError):require_hosted_fixture(env,'linux')

    def test_missing_restore_access_never_creates_or_switches(self):
        api=FakeDesktop();api.input_access=False;report=self.run_fixture(api)
        self.assertEqual(report['status'],'environment-blocked')
        self.assertFalse(any(call[0] in ('create','switch','launch') for call in api.calls))
        self.assertEqual(api.calls[-1],('open_input',READ|SWITCH))

    def test_noninteractive_wrong_session_and_secure_desktop_never_mutate(self):
        for changes in (dict(station='Service-0x0'),dict(active_session=3),dict(input='Winlogon'),dict(input='OOBEDesktop')):
            api=FakeDesktop()
            for key,value in changes.items():setattr(api,key,value)
            report=self.run_fixture(api)
            with self.subTest(changes=changes):
                self.assertEqual(report['status'],'environment-blocked')
                self.assertFalse(any(call[0] in ('create','switch','launch') for call in api.calls))
                self.assertEqual(api.calls[-1],('close_desktop',20))

    def test_normal_lifecycle_restores_both_input_and_thread_then_closes_owned_handles(self):
        api=FakeDesktop();report=self.run_fixture(api)
        self.assertEqual(report['status'],'completed-native-result');self.assertTrue(report['restore_verified'])
        self.assertEqual([call for call in api.calls if call[0]=='switch'],[('switch',30),('switch',20)])
        close=[call for call in api.calls if call[0]=='close_desktop'];self.assertEqual(close,[('close_desktop',30),('close_desktop',20)])
        self.assertIn(('restore_thread',10),api.calls);self.assertNotIn(('close_desktop',10),api.calls)
        self.assertEqual(api.calls[-1],('close_child',100,101))
        self.assertEqual(api.input,'Default');self.assertEqual(api.thread,'Default')
        launch=next(call for call in api.calls if call[0]=='launch');self.assertEqual(launch[1][-2:],('--expected-desktop','FlickDockMaterialQA_model'))
        self.assertFalse(report['verified'])  # native pixels are a separate gate

    def test_child_timeout_terminates_only_created_child_before_restoring(self):
        api=FakeDesktop();api.waits=[False,True];report=self.run_fixture(api)
        self.assertTrue(report['child_timeout']);self.assertTrue(report['restore_verified']);self.assertFalse(report['verified'])
        self.assertEqual([call for call in api.calls if call[0]=='terminate'],[('terminate',100)])
        self.assertLess(api.calls.index(('terminate',100)),api.calls.index(('switch',20)))
        self.assertIn(('wait',100,40000),api.calls);self.assertIn(('wait',100,3000),api.calls)

    def test_createprocess_failure_restores_and_closes_desktop_without_foreign_termination(self):
        api=FakeDesktop();api.launch_ok=False;report=self.run_fixture(api)
        self.assertTrue(report['restore_verified']);self.assertEqual(api.input,'Default')
        self.assertFalse(any(call[0] in ('terminate','close_child') for call in api.calls))

    def test_switch_failure_does_not_launch_but_restores_original_identity(self):
        api=FakeDesktop();api.switch_ok=False;report=self.run_fixture(api)
        self.assertTrue(report['restore_verified']);self.assertFalse(any(call[0]=='launch' for call in api.calls))

    def test_restore_failure_is_not_success_even_if_child_succeeded(self):
        api=FakeDesktop();api.restore_ok=False;report=self.run_fixture(api)
        self.assertEqual(report['status'],'failed-fixture-cleanup');self.assertFalse(report['restore_verified']);self.assertFalse(report['verified'])

    def test_restore_success_return_without_identity_readback_still_fails(self):
        api=FakeDesktop();original=api.switch
        api.switch=lambda handle:True if handle==20 else original(handle)
        report=self.run_fixture(api)
        self.assertTrue(report['restore_switch_ok']);self.assertFalse(report['restore_verified']);self.assertEqual(report['status'],'failed-fixture-cleanup')

    def test_failed_handle_cleanup_blocks_an_otherwise_completed_result(self):
        api=FakeDesktop();api.close_handles=False;report=self.run_fixture(api)
        self.assertTrue(report['restore_verified']);self.assertEqual(report['status'],'failed-fixture-cleanup');self.assertFalse(report['verified'])

    def test_verified_gate_rejects_stale_desktop_pixels_and_missing_handle_cleanup(self):
        report=self.run_fixture(FakeDesktop());name=report['own_desktop_name']
        pixels=dict(verified=True,github_sha='test-sha',desktop_context_before_qt=dict(thread_desktop=dict(name=name),input_desktop=dict(name=name,receiving_input=True)))
        self.assertTrue(fixture_verified(report,pixels))
        stale=copy.deepcopy(pixels);stale['desktop_context_before_qt']['thread_desktop']['name']='Default';self.assertFalse(fixture_verified(report,stale))
        for closed in ({},dict(process=True),dict(process=True,thread=False)):
            altered=dict(report,close_child_handles=closed);self.assertFalse(fixture_verified(altered,pixels))

    def test_real_createprocess_abi_carries_lpdesktop_and_never_inherits_handles(self):
        calls=[]
        class Function:
            def __init__(self,name):self.name=name
            def __call__(self,*args):
                if self.name!='CreateProcessW':raise AssertionError('No real/native call expected: '+self.name)
                application,command,_,_,inherit,flags,environment,cwd,startup,process=args
                calls.append(dict(application=application,command=command.value,inherit=inherit,flags=flags,desktop=startup._obj.lpDesktop))
                process._obj.hProcess=100;process._obj.hThread=101;process._obj.dwProcessId=102;process._obj.dwThreadId=103
                return 1
        class Library:
            def __getattr__(self,name):value=Function(name);setattr(self,name,value);return value
        with patch('ci_windows_desktop_fixture.ctypes.WinDLL',side_effect=lambda *args,**kwargs:Library(),create=True):
            api=DesktopApi();child=api.launch([r'C:\trusted\python.exe',r'C:\trusted\probe.py'],'FlickDockMaterialQA_abi')
        self.assertEqual(ctypes.sizeof(api.Startup),104 if ctypes.sizeof(ctypes.c_void_p)==8 else 68)
        self.assertEqual(api.Startup.lpDesktop.offset,16 if ctypes.sizeof(ctypes.c_void_p)==8 else 8)
        self.assertEqual(calls[0]['desktop'],'WinSta0\\FlickDockMaterialQA_abi');self.assertFalse(calls[0]['inherit']);self.assertEqual(calls[0]['flags'],0x08000000)
        self.assertEqual(child,dict(process=100,thread=101,pid=102,tid=103))
        self.assertEqual(OWN_ACCESS & (0x8|0x10|0x20|0x40000|0x80000),0)


if __name__=='__main__':unittest.main()
