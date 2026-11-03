"""Guard-only checks, never importing Qt or running a desktop/native entry."""
import os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import native_text_plain as subject

class GuardTests(unittest.TestCase):
    def test_guard_precedes_output_and_platform_imports(self):
        with tempfile.TemporaryDirectory(prefix='fd-plain-guard-') as directory:
            output=Path(directory)/'must-not-exist'
            with patch.dict(os.environ,{},clear=True),self.assertRaisesRegex(RuntimeError,'HOSTED_MAC'):
                subject.main(['--output',str(output)])
            self.assertFalse(output.exists());self.assertFalse(any(name.startswith('PySide6') for name in sys.modules))
    def test_each_required_flag_and_wrong_values_rejected(self):
        env=dict(GITHUB_ACTIONS='true',CI='true',RUNNER_ENVIRONMENT='github-hosted',RUNNER_OS='macOS',
                 GITHUB_REPOSITORY='los0711/-flickdock-build',GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_JOB='text-profile',FD_QA_TEXT_PROFILE='1')
        with patch.object(subject.sys,'platform','darwin'),patch.dict(os.environ,env,clear=True):subject.hosted_guard()
        for key in env:
            for value in (None,'other'):
                changed=dict(env)
                if value is None:changed.pop(key)
                else:changed[key]=value
                with self.subTest(key=key,value=value),patch.object(subject.sys,'platform','darwin'),patch.dict(os.environ,changed,clear=True),self.assertRaises(RuntimeError):subject.hosted_guard()
        with patch.object(subject.sys,'platform','win32'),patch.dict(os.environ,env,clear=True),self.assertRaises(RuntimeError):subject.hosted_guard()

if __name__=='__main__':unittest.main()
