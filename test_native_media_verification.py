"""Native Mac media checks are opt-in and do not enable normal native services."""
import os
import unittest
from unittest.mock import patch
import offscreen_verify as subject


class NativeMediaVerificationTests(unittest.TestCase):
    def allowed(self,platform,**environment):
        with patch.object(subject.sys,'platform',platform),patch.dict(os.environ,environment,clear=True):
            return subject.media_verification_allowed()

    def test_offscreen_remains_allowed_on_both_platforms(self):
        for platform in ('win32','darwin'):
            with self.subTest(platform=platform):
                self.assertTrue(self.allowed(platform,QT_QPA_PLATFORM='offscreen'))

    def test_cocoa_requires_all_explicit_ci_conditions(self):
        required=dict(QT_QPA_PLATFORM='cocoa',GITHUB_ACTIONS='true',FLICKDOCK_VERIFY_NATIVE_MEDIA='1')
        self.assertTrue(self.allowed('darwin',**required))
        for key in required:
            with self.subTest(missing=key):
                environment=dict(required);environment.pop(key)
                self.assertFalse(self.allowed('darwin',**environment))
        self.assertFalse(self.allowed('win32',**required))

    def test_default_native_desktop_and_other_plugins_stay_rejected(self):
        for mode in ('','cocoa','windows','minimal'):
            with self.subTest(mode=mode):
                self.assertFalse(self.allowed('darwin',QT_QPA_PLATFORM=mode))


if __name__=='__main__':unittest.main()
