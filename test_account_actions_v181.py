"""Authenticated password/device actions with real queued worker lifecycle."""
import copy
import threading
import tempfile
import unittest
from unittest.mock import patch
import account_service
from account_service import AccountService
from cloud_api import CloudError
from test_account_panel_v180 import APP,Api,MemorySessions,PASSWORD,challenge,wait


class AccountActions(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-account-actions-')
        self.api=Api();self.store=MemorySessions();self.status=[];self.changed=[]
        self.service=AccountService(self.temp.name,api=self.api,store=self.store)
        self.api.token='unchanged-current-session';self.service.account={'id':'fixture','username':'fixture'}
        self.service.device_id='11111111-1111-4111-8111-111111111111'
        self.service.status_changed.connect(self.status.append)
        self.service.password_changed.connect(lambda:self.changed.append(True))

    def tearDown(self):
        self.service.close();self.assertTrue(wait(lambda:not self.service._auth_jobs));self.temp.cleanup()

    def password_challenge(self):
        value=challenge();value['new_salt']='02'*16
        self.api.finish(self.api.last('/v1/auth/password/challenge'),value)

    def test_old_and_new_secrets_are_derived_off_thread_and_only_proofs_are_sent(self):
        secrets=[]
        def derive(password,salt):
            self.assertNotEqual(threading.current_thread(),threading.main_thread())
            secrets.append(password);return bytearray(salt[0:1]*32)
        with patch.object(account_service,'derive_auth_key',derive):
            self.assertTrue(self.service.change_password(PASSWORD,'next-fixture-password','next-fixture-password'))
            first=self.api.last();self.assertEqual(first['path'],'/v1/auth/password/challenge');self.assertEqual(first['options']['data'],{})
            self.password_challenge();self.assertTrue(wait(lambda:any(r['path']=='/v1/auth/password' for r in self.api.requests)))
        request=self.api.last('/v1/auth/password');data=request['options']['data']
        self.assertEqual(set(data),{'challenge_id','old_auth_key','new_auth_key'})
        self.assertNotEqual(data['old_auth_key'],data['new_auth_key']);self.assertTrue(all(not any(secret) for secret in secrets))
        self.api.finish(request,{'schema_version':1,'changed':True,'other_devices_revoked':True})
        self.assertEqual(self.changed,[True]);self.assertEqual(self.api.token,'unchanged-current-session')
        self.assertIsNone(self.store.value);self.assertFalse(self.service.busy);self.assertEqual(self.api.last()['path'],'/v1/devices')

    def test_confirmation_and_untrusted_new_salt_do_not_schedule_password_work(self):
        self.assertFalse(self.service.change_password(PASSWORD,'next-fixture-password','different'))
        self.assertFalse(self.service.change_password(PASSWORD,PASSWORD,PASSWORD));self.assertEqual(self.api.requests,[])
        for new_salt in ['01'*16,'invalid',None]:
            self.service.change_password(PASSWORD,'next-fixture-password')
            data=challenge();data['new_salt']=new_salt
            self.api.finish(self.api.last('/v1/auth/password/challenge'),data)
            self.assertFalse(self.service.busy);self.assertFalse(self.service._auth_jobs)
        self.assertFalse(any(r['path']=='/v1/auth/password' for r in self.api.requests))

    def test_cancel_between_derivations_discards_proof_and_wipes_both_passwords(self):
        entered,release=threading.Event(),threading.Event();secrets=[]
        def derive(password,salt):
            secrets.append(password);entered.set();release.wait(3);return bytearray(b'x'*32)
        with patch.object(account_service,'derive_auth_key',derive):
            self.service.change_password(PASSWORD,'next-fixture-password');self.password_challenge()
            self.assertTrue(wait(entered.is_set));self.service.cancel_auth();release.set()
            self.assertTrue(wait(lambda:not self.service._auth_jobs))
        self.assertEqual(len(secrets),1);self.assertFalse(any(secrets[0]))
        self.assertFalse(any(r['path']=='/v1/auth/password' for r in self.api.requests))
        self.assertEqual(self.api.token,'unchanged-current-session')

    def test_incorrect_password_leaves_current_session_and_only_success_closes_form(self):
        with patch.object(account_service,'derive_auth_key',return_value=bytearray(b'x'*32)):
            self.service.change_password(PASSWORD,'next-fixture-password');self.password_challenge()
            self.assertTrue(wait(lambda:any(r['path']=='/v1/auth/password' for r in self.api.requests)))
        self.api.finish(self.api.last('/v1/auth/password'),error=CloudError('hidden provider detail',403,'INVALID_OLD_PASSWORD'))
        self.assertEqual(self.api.token,'unchanged-current-session');self.assertFalse(self.changed);self.assertFalse(self.service.busy)
        self.assertEqual(self.status[-1],'原密码不正确，请重新输入。')

    def test_device_rename_validates_then_refreshes_and_ignores_late_account_reply(self):
        for value in ['', '  ', 'a'*81,'bad\x00name',None]:self.assertFalse(self.service.rename_device(self.service.device_id,value))
        self.assertEqual(self.api.requests,[])
        self.assertTrue(self.service.rename_device(self.service.device_id,'  我的电脑  '))
        request=self.api.last();self.assertEqual(request['method'],'PATCH');self.assertEqual(request['options']['data'],{'name':'我的电脑'})
        self.api.finish(request,{'schema_version':1,'name':'我的电脑'})
        self.assertEqual(self.service.device_name,'我的电脑');self.assertEqual(self.api.last()['path'],'/v1/devices')
        self.service.rename_device(self.service.device_id,'late');late=self.api.last()
        self.service._clear('fixture signout');self.api.finish(late,{'schema_version':1,'name':'late'})
        self.assertEqual(self.service.device_name,'我的电脑');self.assertIsNone(self.service.account)


if __name__=='__main__':unittest.main(verbosity=2)
