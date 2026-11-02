"""Hosted guards and fake-kernel ownership. No actual Win32/process/clipboard."""
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('reader_job_gate',Path(__file__).with_name('native_reader_job_gate.py'))
gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)


class HostedScopeTests(unittest.TestCase):
    def test_local_or_unrelated_runner_rejected_before_fixture_io(self):
        for changes in ({'GITHUB_ACTIONS':'false'}, {'GITHUB_REPOSITORY':'other/repo'}, {'RUNNER_TEMP':''}):
            environment={'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':'los0711/-flickdock-build','RUNNER_TEMP':'unused'}
            environment.update(changes)
            with self.subTest(changes=changes),patch.dict(os.environ,environment,clear=True),patch.object(gate.sys,'platform','win32'),patch.object(Path,'mkdir',side_effect=AssertionError('guard bypass')):
                with self.assertRaisesRegex(RuntimeError,'HOSTED_RUNNER_REQUIRED'):
                    gate.hosted_root('unused','a'*32,create=True)

    def test_exact_owned_root_nonce_and_marker_required(self):
        with tempfile.TemporaryDirectory() as directory:
            environment={'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':'los0711/-flickdock-build','RUNNER_TEMP':directory}
            nonce='a'*32;root=Path(directory)/('flickdock-reader-job-'+nonce)
            with patch.dict(os.environ,environment,clear=True),patch.object(gate.sys,'platform','win32'):
                with self.assertRaisesRegex(RuntimeError,'OWNED_PROFILE_REQUIRED'):
                    gate.hosted_root(root.with_name('other'),nonce,create=True)
                self.assertFalse(root.exists())
                self.assertEqual(gate.hosted_root(root,nonce,create=True),root)
                self.assertEqual(gate.hosted_root(root,nonce),root)
                with self.assertRaises(FileExistsError):gate.hosted_root(root,nonce,create=True)
                (root/'fixture-owner.json').write_text('{}')
                with self.assertRaisesRegex(RuntimeError,'WRONG_FIXTURE_OWNER'):gate.hosted_root(root,nonce)

    def test_platform_nonce_and_reparse_are_rejected_before_creation(self):
        environment={'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':'los0711/-flickdock-build','RUNNER_TEMP':'unused'}
        for platform,nonce in (('linux','a'*32),('win32','A'*32),('win32','../other'),('win32','a'*31)):
            with self.subTest(platform=platform,nonce=nonce),patch.dict(os.environ,environment,clear=True),patch.object(gate.sys,'platform',platform),patch.object(Path,'mkdir',side_effect=AssertionError('guard bypass')):
                with self.assertRaisesRegex(RuntimeError,'HOSTED_RUNNER_REQUIRED'):gate.hosted_root('unused',nonce,create=True)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/('flickdock-reader-job-'+'a'*32)
            environment['RUNNER_TEMP']=directory
            with patch.dict(os.environ,environment,clear=True),patch.object(gate.sys,'platform','win32'),patch.object(Path,'is_junction',new=lambda path:path==root),patch.object(Path,'mkdir',side_effect=AssertionError('reparse creation')):
                with self.assertRaisesRegex(RuntimeError,'REPARSE_PROFILE_REJECTED'):gate.hosted_root(root,'a'*32,create=True)


class FakeFunction:
    """Allows ctypes signature assignment without calling a kernel DLL."""
    def __init__(self,action):self.action=action
    def __call__(self,*args):return self.action(*args)


class OwnedKernelTests(unittest.TestCase):
    def run_fake(self,mode):
        events=[]
        class Parent:
            _handle=900
            returncode=None
            def poll(self):return self.returncode
            def kill(self):events.append(('parent-kill',));self.returncode=1
            def wait(self,timeout=None):events.append(('parent-wait',timeout));return self.returncode
        def duplicate(source_parent,source_child,target,out,access,inherit,flags):
            events.append(('duplicate',source_parent,source_child,target,access,inherit,flags))
            if mode=='duplicate-failure':return False
            out._obj.value=987
            return True
        waits=[]
        def wait(handle,timeout):
            events.append(('wait',handle,timeout));waits.append(timeout)
            if mode=='cleanup-wait-failure' and len(waits)==3:raise RuntimeError('synthetic wait failure')
            if mode=='exited':return 0
            if mode in ('cleanup-terminate-failure','cleanup-wait-failure'):return 258
            return 258 if len(waits)==1 else 0
        def terminate(handle,code):
            events.append(('terminate',handle,code))
            return mode!='cleanup-terminate-failure'
        def close(handle):events.append(('close',handle));return mode!='cleanup-close-failure'
        kernel=SimpleNamespace(
            GetCurrentProcess=FakeFunction(lambda:444),
            GetProcessId=FakeFunction(lambda handle:999 if mode=='wrong-identity' else 111),
            DuplicateHandle=FakeFunction(duplicate),WaitForSingleObject=FakeFunction(wait),
            TerminateProcess=FakeFunction(terminate),CloseHandle=FakeFunction(close),
            OpenProcess=FakeFunction(lambda *args:self.fail('Must not reopen a possibly recycled PID')))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);nonce='a'*32
            (root/'parent-ready.json').write_text(json.dumps(dict(nonce=nonce,pid=111,in_job=True,source_handle=222)))
            with patch.object(ctypes,'WinDLL',return_value=kernel,create=True),patch.object(ctypes,'WinError',return_value=OSError('synthetic safe native failure'),create=True),patch.object(ctypes,'get_last_error',return_value=5,create=True),patch.object(gate.subprocess,'Popen',return_value=Parent()):
                code=gate.run_gate(root,nonce)
            report=json.loads((root/'report.json').read_text())
        return code,report,events

    def test_only_exact_parent_child_kernel_object_can_pass(self):
        code,report,events=self.run_fake('success')
        self.assertEqual(code,0);self.assertTrue(report['ok'])
        self.assertIn(('duplicate',900,222,444,0,False,2),events)
        self.assertEqual([row for row in events if row[0]=='close'],[('close',987)])
        self.assertFalse(any(row[0]=='terminate' for row in events))
        self.assertEqual(report['checks'],['actual_atomic_job_membership','child_reaped_after_forced_parent_death'])

    def test_wrong_identity_and_already_exited_object_are_not_terminated(self):
        for mode,reason in (('wrong-identity','CHILD_KERNEL_IDENTITY_MISMATCH'),('exited','CHILD_NOT_ALIVE_BEFORE_PARENT_EXIT')):
            with self.subTest(mode=mode):
                code,report,events=self.run_fake(mode)
                self.assertEqual(code,1);self.assertFalse(report['ok']);self.assertEqual(report['error']['reason'],reason)
                self.assertFalse(any(row[0]=='terminate' for row in events))
                self.assertEqual([row for row in events if row[0]=='close'],[('close',987)])

    def test_cleanup_wait_or_terminate_failure_still_closes_the_owned_handle(self):
        for mode in ('cleanup-wait-failure','cleanup-terminate-failure'):
            with self.subTest(mode=mode):
                code,report,events=self.run_fake(mode)
                self.assertEqual(code,1);self.assertFalse(report['ok']);self.assertTrue(report['cleanup_errors'])
                self.assertEqual([row for row in events if row[0]=='close'],[('close',987)])
                self.assertTrue(all(row[1]==987 for row in events if row[0]=='terminate'))

    def test_duplicate_or_close_failure_never_claims_native_gate_passed(self):
        for mode in ('duplicate-failure','cleanup-close-failure'):
            with self.subTest(mode=mode):
                code,report,events=self.run_fake(mode)
                self.assertEqual(code,1);self.assertFalse(report['ok'])
                if mode=='duplicate-failure':
                    self.assertFalse(any(row[0] in ('close','terminate') for row in events))
                else:self.assertTrue(report['cleanup_errors'])


if __name__=='__main__':unittest.main()
