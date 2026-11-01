"""Portable protocol/scope tests; no Qt, socket, public service or native UI."""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC=importlib.util.spec_from_file_location('recent_seed_qa',Path(__file__).with_name('seed_recent_peer.py'))
qa=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(qa)


class RecentSeedTests(unittest.TestCase):
    def setUp(self):self.p=qa.SeedProtocol('a'*64,'b'*64)

    def identity(self,role):
        return self.p.message(role,dict(schema_version=1,type='identity',device=dict(id=self.p.ids[role]),code='23456789'))

    def consent(self):
        self.identity('primary');actions=self.identity('peer')
        self.assertEqual(len(actions),1);request=actions[0][2]
        self.assertEqual((request['total'],request['count'],request['names']),('0',0,[]))
        delivered=dict(schema_version=1,**request);delivered['peer']={'id':self.p.ids['primary']}
        delivered.pop('code');accepted=self.p.message('peer',delivered)
        self.assertEqual(accepted,[('send','peer',dict(type='accept',id=self.p.session))])
        return request

    def begin_cancel(self):
        request=self.consent()
        begin=dict(schema_version=1,type='begin',id=self.p.session,generation=1,initiator=self.p.ids['primary'])
        self.assertEqual(self.p.message('primary',begin),[])
        self.assertEqual(self.p.message('peer',begin),[('send','primary',dict(type='cancel',id=self.p.session))])
        cancelled=dict(schema_version=1,type='cancelled',id=self.p.session)
        self.assertEqual(self.p.message('primary',cancelled),[])
        self.assertEqual(self.p.message('peer',cancelled),[('close','peer',None)])
        return request

    def test_only_exact_loopback_origin_is_accepted(self):
        self.assertEqual(qa.validate_origin('http://127.0.0.1:12345'),'ws://127.0.0.1:12345/v1/wan/connect')
        for value in ('https://127.0.0.1:12','http://localhost:12','http://127.0.0.1.evil:12',
                      'http://' + 'user:secret@127.0.0.1:12','http://127.0.0.1:12/?x=1','http://127.0.0.1:12/other',
                      'http://127.0.0.1','http://127.0.0.1:0','http://127.0.0.1:65536'):
            with self.subTest(value=value),self.assertRaises(ValueError):qa.validate_origin(value)

    def test_key_creation_is_exclusive_and_only_in_prelaunch_disposable_profile(self):
        with tempfile.TemporaryDirectory(prefix='flickdock-frozen-normal-ci-') as name:
            profile=Path(name)/'FlickDock';profile.mkdir()
            with self.assertRaises(ValueError):qa.primary_credential(profile)
            for entry in ('settings.json','shelves.json'):(profile/entry).write_text('{}')
            key=qa.primary_credential(profile);self.assertRegex(key,'^[a-f0-9]{64}$')
            path=profile/'wan-device.key';before=path.stat().st_mtime_ns
            self.assertEqual(qa.primary_credential(profile),key);self.assertEqual(path.stat().st_mtime_ns,before)
            path.write_text('damaged')
            with self.assertRaisesRegex(ValueError,'invalid'):qa.primary_credential(profile)
            self.assertEqual(path.read_text(),'damaged')
        with tempfile.TemporaryDirectory(prefix='not-owned-') as name:
            profile=Path(name)/'FlickDock';profile.mkdir()
            for entry in ('settings.json','shelves.json'):(profile/entry).write_text('{}')
            with self.assertRaises(ValueError):qa.primary_credential(profile)
            self.assertFalse((profile/'wan-device.key').exists())

    def test_both_hello_and_code_identities_precede_single_real_request(self):
        for role in ('primary','peer'):
            action=self.p.connected(role)
            self.assertEqual(action[0][:2],('send',role));self.assertEqual(action[0][2]['type'],'hello')
        self.assertEqual(self.identity('primary'),[])
        self.assertEqual(self.identity('primary'),[])
        actions=self.identity('peer');self.assertEqual(len(actions),1)
        self.assertEqual(actions[0][:2],('send','primary'));self.assertEqual(actions[0][2]['type'],'request')
        self.assertEqual(self.identity('peer'),[])

    def test_only_matching_delivered_request_can_confirm_receiver(self):
        self.identity('primary');self.identity('peer')
        request=dict(schema_version=1,type='request',id=self.p.session,peer=dict(id='a'*64),token=self.p.token,
                     digest=self.p.digest,total='0',count=0,names=[])
        for change in (dict(token='c'*64),dict(total='1'),dict(names=['file.bin']),dict(count=1)):
            with self.subTest(change=change),self.assertRaisesRegex(ValueError,'did not match'):
                self.p.message('peer',{**request,**change})
        self.assertFalse(self.p.accepted)

    def test_accept_begin_cancel_then_real_offline_recent_proof(self):
        request=self.begin_cancel()
        self.assertEqual(self.p.disconnected('peer'),[('send','primary',dict(type='recent'))])
        rows=[dict(id='b'*64,name=qa.RECENT_NAME,online=False)]
        self.assertEqual(self.p.message('primary',dict(schema_version=1,type='peers',peers=rows)),[('close','primary',None)])
        self.assertEqual(self.p.disconnected('primary'),[('done','primary',None)])
        proof=self.p.proof();self.assertTrue(proof['consent_confirmed']);self.assertFalse(proof['online'])
        serialized=json.dumps(proof)
        for secret in (request['token'],self.p.session,self.p.ids['primary'],self.p.ids['peer']):self.assertNotIn(secret,serialized)
        self.assertEqual(proof['payload_bytes'],0);self.assertFalse(proof['provider_requests']);self.assertFalse(proof['ice_started'])

    def test_online_account_peer_missing_or_duplicate_recent_cannot_pass(self):
        self.begin_cancel();self.p.disconnected('peer')
        rows=[dict(id='b'*64,name=qa.RECENT_NAME,online=False)]
        variants=[[],rows*2,[{**rows[0],'online':True}],[{**rows[0],'account_device':True}],
                  [{**rows[0],'name':'wrong'}]]
        for peers in variants:
            with self.subTest(peers=peers),self.assertRaises(ValueError):
                self.p.message('primary',dict(schema_version=1,type='peers',peers=peers))
        self.assertFalse(self.p.complete)

    def test_early_disconnect_begin_or_wrong_identity_preserves_failure(self):
        with self.assertRaisesRegex(ValueError,'before real cancellation'):self.p.disconnected('peer')
        with self.assertRaisesRegex(ValueError,'before actual offline'):self.p.disconnected('primary')
        with self.assertRaisesRegex(ValueError,'identity mismatch'):
            self.p.message('primary',dict(schema_version=1,type='identity',device=dict(id='c'*64),code='23456789'))
        with self.assertRaisesRegex(ValueError,'accepted session'):
            self.p.message('primary',dict(schema_version=1,type='begin',id=self.p.session,generation=1,initiator='a'*64))
        with self.assertRaises(ValueError):self.p.proof()

    def test_schema_or_real_service_error_is_not_silently_ignored(self):
        with self.assertRaisesRegex(ValueError,'schema'):self.p.message('primary',dict(type='identity'))
        with self.assertRaisesRegex(ValueError,'rejected'):self.p.message('primary',dict(schema_version=1,type='error',message='private'))

    def test_failure_proof_retains_exact_controlled_predicate_without_private_payload(self):
        self.identity('primary')
        proof=self.p.failure_proof(ValueError('Real signaling identity mismatch'))
        self.assertFalse(proof['ok']);self.assertEqual(proof['stage'],'received')
        self.assertEqual(proof['failure_class'],'ValueError');self.assertEqual(proof['reason'],'Real signaling identity mismatch')
        secret='private-token-'+self.p.token
        proof=self.p.failure_proof(ValueError(secret));self.assertEqual(proof['reason'],'fixture callback failed')
        self.assertNotIn(secret,json.dumps(proof));self.assertNotIn(self.p.token,json.dumps(proof))
        self.p.message('primary',dict(schema_version=1,type=secret))
        self.assertEqual(self.p.events[-1]['type'],'unknown')
        self.assertNotIn(secret,json.dumps(self.p.failure_proof(RuntimeError(secret))))

    def test_no_relay_ice_or_fake_storage_instructions_are_emitted(self):
        request=self.begin_cancel()
        event_types=[e['type'] for e in self.p.events if e['stage']=='received']
        self.assertEqual(event_types,['identity','identity','request','begin','begin','cancelled','cancelled'])
        self.assertFalse(any(name in request for name in ('iceServers','relay','peer_id','storage')))

    def test_host_guard_is_required_before_qt_or_key_access(self):
        markers={'GITHUB_ACTIONS':'true','RUNNER_OS':'Windows','RUNNER_ENVIRONMENT':'github-hosted'}
        for name in markers:
            with patch.object(qa.sys,'platform','win32'),patch.dict(qa.os.environ,{**markers,name:'no'}):
                with self.assertRaises(RuntimeError):qa.require_hosted()
        with patch.object(qa.sys,'platform','win32'),patch.dict(qa.os.environ,markers):qa.require_hosted()


if __name__=='__main__':unittest.main()
