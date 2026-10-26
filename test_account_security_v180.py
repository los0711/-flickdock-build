"""Real KDF lifecycle and transactional credential persistence, no OS access."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from PySide6.QtCore import QTimer
import account_service
from account_service import AccountService,derive_auth_key
from account_store import SessionStore
from test_account_panel_v180 import APP,Api,MemorySessions,PASSWORD,challenge,flush,wait


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-auth-security-')
        self.api=Api();self.store=MemorySessions();self.status=[]
        self.service=AccountService(self.temp.name,api=self.api,store=self.store)
        self.service.status_changed.connect(self.status.append)

    def tearDown(self):
        self.service.close();self.assertTrue(wait(lambda:not self.service._auth_jobs));self.temp.cleanup()

    def test_fixed_native_scrypt_vector(self):
        self.assertEqual(bytes(derive_auth_key(bytearray(b'frozen-qa-known-password'),b'\x01'*16)).hex(),
            '61a958177a02aa9f93c69ce6fbf35b66442e1bf16c556bd6405b89b86dd4a6fa')

    def test_untrusted_kdf_parameters_never_schedule_work(self):
        for mutate in [lambda x:x['kdf'].update(N=1048576),lambda x:x['kdf'].update(p=5000),
                       lambda x:x['kdf'].update(dkLen=128),lambda x:x.update(salt='bad'),
                       lambda x:x.update(expires_at='2000-01-01T00:00:00Z')]:
            self.service.login('fixture',PASSWORD);data=challenge();mutate(data)
            self.api.finish(self.api.last('/v1/auth/challenge'),data)
            self.assertFalse(self.service.busy);self.assertFalse(self.service._auth_jobs)
        self.assertFalse(any(r['path']=='/v1/auth/login' for r in self.api.requests))

    def test_background_kdf_heartbeat_and_cancel_discards_late_proof(self):
        entered,release=threading.Event(),threading.Event();heartbeats=[];passwords=[]
        def slow(password,salt):
            passwords.append(password);entered.set();release.wait(3);return bytearray(b'k'*32)
        timer=QTimer();timer.setInterval(5);timer.timeout.connect(lambda:heartbeats.append(1));timer.start()
        try:
            with patch.object(account_service,'derive_auth_key',slow):
                self.service.login('fixture',PASSWORD);self.api.finish(self.api.last('/v1/auth/challenge'),challenge())
                self.assertTrue(wait(entered.is_set));self.assertTrue(wait(lambda:len(heartbeats)>5))
                self.service.cancel_auth();release.set();self.assertTrue(wait(lambda:not self.service._auth_jobs))
            self.assertFalse(any(r['path']=='/v1/auth/login' for r in self.api.requests))
            self.assertTrue(passwords and not any(passwords[0]))
        finally:release.set();timer.stop()

    def test_password_not_sent_and_verifier_not_persisted(self):
        self.service.register('fixture',PASSWORD)
        request=self.api.last('/v1/auth/challenge');self.assertEqual(set(request['options']['data']),{'username','purpose'})
        self.api.finish(request,challenge());self.assertTrue(wait(lambda:any(r['path']=='/v1/auth/register' for r in self.api.requests)))
        payload=self.api.last('/v1/auth/register')['options']['data'];self.assertNotIn('password',payload);self.assertEqual(len(payload['auth_key']),64)
        self.api.finish(self.api.last('/v1/auth/register'),dict(token='session',device_id='device',expires_at='future',account={'id':'account-fixture','username':'fixture'}))
        self.assertIsNone(self.service._auth_state);self.assertNotIn('auth_key',json.dumps(self.store.value))
        self.assertNotIn(PASSWORD,json.dumps(self.store.value))

    def test_session_store_uses_only_injected_secure_backend(self):
        class Backend:
            value=None
            def get_password(self,*args):return self.value
            def set_password(self,service,key,value):self.value=value
            def delete_password(self,*args):self.value=None
        backend=Backend();store=SessionStore(self.temp.name,'https://fixture.invalid',backend=backend)
        session=dict(token='opaque-session-secret',device_id='device',expires_at='future',account={'id':'account'})
        store.save(session);disk=store.path.read_text(encoding='utf8')
        self.assertNotIn(session['token'],disk);self.assertEqual(store.load()['token'],session['token'])
        previous=backend.value
        with patch('account_store.atomic_json',side_effect=OSError('fixture write failure')):
            with self.assertRaises(OSError):store.save(dict(session,token='replacement'))
        self.assertEqual(backend.value,previous);store.clear();self.assertIsNone(backend.value);self.assertFalse(store.path.exists())


if __name__=='__main__':unittest.main(verbosity=2)
