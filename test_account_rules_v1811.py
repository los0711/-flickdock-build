"""Password policy through the real account service and inline form, offscreen."""
import tempfile
import unittest
from unittest.mock import patch

import account_service
from account_panel import AccountPanel
from account_service import AccountService
from test_account_panel_v180 import APP,Api,MemorySessions,challenge,flush,wait


class AccountRules(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-account-rules-')
        self.api=Api();self.status=[]
        self.service=AccountService(self.temp.name,api=self.api,store=MemorySessions())
        self.service.status_changed.connect(self.status.append)

    def tearDown(self):
        self.service.close();self.assertTrue(wait(lambda:not self.service._auth_jobs));self.temp.cleanup()

    def test_registration_accepts_six_characters_unicode_symbols_and_long_password(self):
        for secret in ['123456','密码測試六字','!@#$%^','a'*129]:
            with self.subTest(length=len(secret)):
                self.assertTrue(self.service.register('fixture',secret),self.status)
                self.assertEqual(self.api.last()['path'],'/v1/auth/challenge')
                self.service.cancel_auth()

    def test_registration_rejects_short_or_any_unicode_whitespace_without_request(self):
        for secret in ['12345',' abcdefghij','abcdefghij ','abcde\tfghij','abcde\nfghij','abcde\u00a0fghij','abcde\u3000fghij']:
            with self.subTest(secret=repr(secret)):
                self.assertFalse(self.service.register('fixture',secret))
                self.assertFalse(self.api.requests)

    def test_old_password_login_remains_compatible_and_secret_never_sent(self):
        for secret in ['legacy password','short']:
            with self.subTest(secret=secret),patch.object(account_service,'derive_auth_key',return_value=bytearray(b'x'*32)):
                self.assertTrue(self.service.login('fixture',secret))
                self.api.finish(self.api.last('/v1/auth/challenge'),challenge())
                self.assertTrue(wait(lambda:any(r['path']=='/v1/auth/login' for r in self.api.requests)))
                data=self.api.last('/v1/auth/login')['options']['data']
                self.assertEqual(set(data),{'username','challenge_id','auth_key','device_name','platform'})
                self.assertNotIn(secret,str(data));self.service.cancel_auth();self.api.requests.clear()

    def test_change_password_uses_new_policy_but_accepts_legacy_old_secret(self):
        self.api.token='fixture';self.service.account={'id':'fixture','username':'fixture'}
        for secret in ['12345','123 456','123\t456','123\u2003456']:
            self.assertFalse(self.service.change_password('old password',secret,secret))
        self.assertFalse(self.api.requests)
        for secret in ['123456','密码ABC！','a'*129]:
            self.assertTrue(self.service.change_password('old password',secret,secret),self.status)
            self.assertEqual(self.api.last()['path'],'/v1/auth/password/challenge');self.service.cancel_auth()

    def test_inline_form_uses_the_same_policy_and_shows_alias_only_when_needed(self):
        self.api.token='fixture';self.service.account={'id':'fixture','username':'fixture'}
        panel=AccountPanel(self.service,email_binding=False)
        try:
            self.api.requests.clear()
            panel._toggle_password(True)
            panel.old_password_edit.setText('old password');panel.new_password_edit.setText('123 456')
            panel.confirm_password_edit.setText('123 456');panel._change_password()
            self.assertFalse(self.api.requests)
            panel.new_password_edit.setText('123456');panel.confirm_password_edit.setText('123456');panel._change_password()
            self.assertEqual(self.api.last()['path'],'/v1/auth/password/challenge')
            self.assertNotIn('128',panel.new_password_edit.placeholderText())
            self.assertGreater(panel.new_password_edit.maxLength(),128)
            self.assertFalse(panel.login_alias_edit.isVisibleTo(panel))
            account={'id':'fixture','username':'fixture','login_alias':'id:11111111-1111-4111-8111-111111111111'}
            self.service.account=account;self.service.account_changed.emit(account);flush()
            self.assertEqual(panel.login_alias_edit.text(),account['login_alias'])
            self.assertTrue(panel.login_alias_edit.isReadOnly());self.assertTrue(panel.login_alias_edit.isVisibleTo(panel))
        finally:panel.close();panel.deleteLater();APP.processEvents()


if __name__=='__main__':unittest.main(verbosity=2)
