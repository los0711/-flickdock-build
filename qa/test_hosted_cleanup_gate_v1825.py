"""Pure/fake review of the hosted candidate; never invokes a native gate."""
import ast
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SOURCE=Path(__file__).with_name('hosted_cleanup_gate.py')
spec=importlib.util.spec_from_file_location('owned_hosted_cleanup_candidate',SOURCE)
gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)
NONCE='1'*32


class HostedCandidateTests(unittest.TestCase):
    def env(self,runner='Windows'):
        return dict(GITHUB_ACTIONS='true',RUNNER_ENVIRONMENT='github-hosted',RUNNER_OS=runner,
                    GITHUB_REPOSITORY=gate.REPOSITORY,GITHUB_JOB='frozen-normal-entry' if runner=='Windows' else 'build',
                    GITHUB_RUN_ID='123456',RUNNER_TEMP='C:/owned-fixture-base' if runner=='Windows' else '/tmp/owned-fixture-base')
    def test_exact_approved_job_and_platform_only(self):
        self.assertEqual(gate.guard(True,NONCE,self.env(),'win32'),'Windows')
        self.assertEqual(gate.guard(True,NONCE,self.env('macOS'),'darwin'),'macOS')
        for runner in ('Linux','windows','MacOS'):
            with self.subTest(runner=runner),self.assertRaises(RuntimeError):gate.guard(True,NONCE,self.env(runner),'linux')
    def test_missing_wrong_flag_environment_or_nonce_is_rejected(self):
        for key in self.env():
            for value in (None,'',False,'false','unexpected'):
                env=self.env()
                if value is None:env.pop(key)
                else:env[key]=value
                with self.subTest(key=key,value=value),self.assertRaises(RuntimeError):gate.guard(True,NONCE,env,'win32')
        for flag in (False,0,1,None,'true'):
            with self.subTest(flag=flag),self.assertRaises(RuntimeError):gate.guard(flag,NONCE,self.env(),'win32')
        for nonce in ('',None,False,'A'*32,'1'*31,'1'*33):
            with self.subTest(nonce=nonce),self.assertRaises(RuntimeError):gate.guard(True,nonce,self.env(),'win32')
    def test_platform_mismatch_or_other_real_jobs_do_not_run(self):
        for platform in ('linux','darwin','win64'):
            with self.assertRaises(RuntimeError):gate.guard(True,NONCE,self.env(),platform)
        for job in ('clipboard','passed-installer','cloud-contract','build','text-profile'):
            env=self.env();env['GITHUB_JOB']=job
            with self.assertRaises(RuntimeError):gate.guard(True,NONCE,env,'win32')
        for runner,platform in (('Windows','win32'),('macOS','darwin')):
            env=self.env(runner);env['GITHUB_JOB']='cleanup-boundaries'
            self.assertEqual(gate.guard(True,NONCE,env,platform),runner)
    def test_main_rejects_local_before_path_or_runtime_or_process_use(self):
        with patch.dict(gate.os.environ,{},clear=True),patch.object(gate,'sha',side_effect=AssertionError('Unexpected file read')),patch.object(gate,'own_root',side_effect=AssertionError('Unexpected owned fixture creation')):
            with self.assertRaisesRegex(RuntimeError,'HOSTED_CLEANUP_SCOPE_REQUIRED'):
                gate.main(['--hosted-cleanup','--nonce',NONCE,'--repo','nonexistent','--exe','nonexistent','--exe-sha256','a'*64])
        self.assertNotIn('clipboard_cleanup',sys.modules)
        with patch.dict(gate.os.environ,{},clear=True),patch.object(gate,'sha',side_effect=AssertionError('Unexpected file read')):
            with self.assertRaisesRegex(RuntimeError,'HOSTED_CLEANUP_SCOPE_REQUIRED'):
                gate.main(['--hosted-cleanup','--nonce',NONCE,'--repo','nonexistent','--source-only'])
    def fake_wait(self,*,reaped=True,receipts=1):
        done=SimpleNamespace(is_set=lambda:True)
        operation=SimpleNamespace(done=done,reaped=reaped,cancel=lambda:None)
        controller=SimpleNamespace(active=None,quarantined=None)
        state={}
        def submit(value,callback):controller.active=operation;state['callback']=callback;return 'fixture-token'
        def events():
            for unused in range(receipts):state['callback']({'status':'ready'})
            controller.active=None
        controller.submit=submit
        return SimpleNamespace(processEvents=events),controller,operation
    def test_only_real_terminal_and_one_callback_accepted(self):
        app,controller,operation=self.fake_wait();owned=[]
        self.assertEqual(gate.await_operation(app,controller,{},owned),{'status':'ready'})
        self.assertEqual(owned,[operation])
        for count in (0,2):
            app,controller,unused=self.fake_wait(receipts=count)
            with self.assertRaisesRegex(RuntimeError,'RECEIPT_COUNT'):gate.await_operation(app,controller,{},[])
    def test_unreaped_failure_cannot_become_passing_receipt(self):
        app,controller,unused=self.fake_wait(reaped=False)
        with self.assertRaisesRegex(RuntimeError,'CHILD_NOT_REAPED'):gate.await_operation(app,controller,{},[])
    def test_runtime_hash_binding_and_native_free_local_review(self):
        self.assertEqual(len(gate.RUNTIME),3)
        self.assertTrue(all(gate.HEX.fullmatch(value) for value in gate.RUNTIME.values()))
        self.assertEqual({name:gate.sha(SOURCE.parent.parent/name) for name in gate.RUNTIME},gate.RUNTIME,
            'An intentional runtime edit requires an explicitly reviewed source pin; never bypass the gate')
        tree=ast.parse(SOURCE.read_text(encoding='utf-8'))
        imports=[node.module or '' for node in ast.walk(tree) if isinstance(node,ast.ImportFrom)]
        self.assertNotIn('qa.clipboard_cleanup_synthetic',imports)
        self.assertFalse(any('QClipboard' in name for name in imports))
        text=SOURCE.read_text(encoding='utf-8')
        for forbidden in ('process_factory=', 'synthetic_scope(', 'SetClipboardData(', 'EmptyClipboard(', 'QApplication(', 'pyautogui', 'WinDLL('):
            self.assertNotIn(forbidden,text)
        main=next(node for node in tree.body if isinstance(node,ast.FunctionDef)and node.name=='main')
        calls=[node for node in main.body if isinstance(node,ast.Assign)]
        guard_index=next(i for i,node in enumerate(calls) if isinstance(node.value,ast.Call)and isinstance(node.value.func,ast.Name)and node.value.func.id=='guard')
        root_index=next(i for i,node in enumerate(calls) if isinstance(node.value,ast.Call)and isinstance(node.value.func,ast.Name)and node.value.func.id=='own_root')
        self.assertLess(guard_index,root_index)
        self.assertIn("'observation_only':True,'installer_acceptance':False",text)
        self.assertIn("'frozen_entry_tested':exe is not None",text)


if __name__=='__main__':unittest.main(verbosity=2)
