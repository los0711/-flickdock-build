"""Actual signaling, original kernels, consent and verified R2 fallback."""
import hashlib
import json
from pathlib import Path
import tempfile
import time
import threading
import unittest
from unittest.mock import patch

from wan_transfer import WanTransferService,RECEIVE_CODE
from syncthing_jobs import DirectUnavailable
from lan_transfer import _Transfer
from test_manual_cloud import LocalCloudFixture,until,APP
from test_syncthing_engine import BINARY
from syncthing_engine import EngineError

PROFILE_RELEASE_TIMEOUT_SECONDS=2
PROFILE_RELEASE_POLL_SECONDS=.025


@unittest.skipUnless(BINARY.is_file(),'Official original kernel is required')
class NativeWanTests(LocalCloudFixture,unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='native-wan-');self.root=Path(self.temporary.name).resolve()
        self.services=[WanTransferService(self.root/name,device_name=name,origin=self.origin,binary=BINARY,
            allow_test_origin=True,loopback=True) for name in ('a','b')]
        self.completed=[];self.errors=[];self.offers=[];self.progress=[]
        for side,service in enumerate(self.services):
            service.completed.connect(lambda value,side=side:self.completed.append((side,value)))
            service.progress.connect(lambda value,side=side:self.progress.append((side,value)))
            service.offer_received.connect(lambda value,side=side:self.offers.append((side,value)))
            service.status_changed.connect(lambda value:self.errors.append(value))

    def tearDown(self):
        for service in self.services:service.close()
        until(lambda:all(service._shutdown and not service._shutdown.is_alive() for service in self.services),20)
        # Windows filesystem filters can retain the last SQLite I/O handle
        # briefly after the owned native job has fully exited.
        deadline=time.monotonic()+PROFILE_RELEASE_TIMEOUT_SECONDS
        while True:
            try:self.temporary.cleanup();break
            except PermissionError as error:
                if getattr(error,'winerror',None)!=32 or time.monotonic()>=deadline:raise
                time.sleep(PROFILE_RELEASE_POLL_SECONDS)

    def connect(self,account=False):
        if account:
            for service,identity in zip(self.services,self.accounts):service.attach_account(identity)
        for service in self.services:service.start()
        until(lambda:all(RECEIVE_CODE.fullmatch(s.code) for s in self.services),25)
        if account:until(lambda:all(s._bound_identity==s._account_identity() for s in self.services),25)

    def wait_completed(self,identifier,seconds=60):
        def terminal():
            bad=[row for side,row in self.progress if row['id']==identifier and row['state']=='error']
            if bad:raise AssertionError(json.dumps(bad,ensure_ascii=False))
            return len([row for side,row in self.completed if row['id']==identifier])==2
        until(terminal,seconds)

    def test_four_character_consent_and_original_kernel_both_directions(self):
        self.connect();original=self.root/'original.bin';original.write_bytes(bytes(range(256))*24576)
        expected=hashlib.sha256(original.read_bytes()).hexdigest()
        for sender,receiver in ((0,1),(1,0)):
            target=self.root/f'target-{sender}';target.mkdir();(target/original.name).write_bytes(b'existing')
            identifier=self.services[sender].send(self.services[receiver].code,[original])
            until(lambda:any(side==receiver and offer['id']==identifier for side,offer in self.offers),30)
            self.assertEqual(list(target.iterdir()),[target/original.name]);self.assertFalse(self.services[receiver]._transfers[identifier].active)
            self.services[receiver].accept(identifier,target);self.wait_completed(identifier)
            self.assertEqual(hashlib.sha256((target/'original (2).bin').read_bytes()).hexdigest(),expected)
            self.assertEqual((target/original.name).read_bytes(),b'existing')
        self.assertTrue(original.is_file());self.assertTrue(all(row.get('route')!='cloud' for side,row in self.progress))

    def test_receiver_accepts_before_large_snapshot_io_starts(self):
        self.connect();source=self.root/'large.bin';source.write_bytes(bytes(range(256))*32768)
        target=self.root/'early-received';target.mkdir();entered=threading.Event();release=threading.Event()
        from manual_payload import prepare as snapshot
        def held(*args,**options):
            entered.set()
            if not release.wait(15):raise TimeoutError('QA snapshot was not released')
            return snapshot(*args,**options)
        try:
            with patch('wan_transfer.prepare',side_effect=held):
                identifier=self.services[0].send(self.services[1].code,[source])
                until(lambda:any(side==1 and offer['id']==identifier for side,offer in self.offers),3)
                self.assertFalse(entered.is_set(),'Disk preparation must follow real receiver consent')
                self.services[1].accept(identifier,target);until(entered.is_set,3)
                until(lambda:any(side==0 and row['id']==identifier and row.get('accepted') for side,row in self.progress),3)
                until(lambda:all(service.engine.connections().get(other.engine.device_id,{}).get('connected')
                      for service,other in ((self.services[0],self.services[1]),(self.services[1],self.services[0]))),8)
                for service in self.services:self.assertEqual(service.engine.request('GET','/rest/config/folders'),[])
                self.assertEqual(list(target.iterdir()),[]);release.set();self.wait_completed(identifier)
        finally:release.set()
        self.assertEqual((target/source.name).read_bytes(),source.read_bytes())

    def test_replayed_peer_preflight_cannot_replace_the_pinned_native_identity(self):
        service=self.services[0];identifier='4'*32
        state=service._state('send',dict(id='5'*64,name='receiver'));state['generation']=1
        service._pending[identifier]=state;service._transfers[identifier]=_Transfer(identifier,'send','receiver')
        device='-'.join(['AAAAAAA']*8);other='-'.join(['BBBBBBB']*8)
        message=dict(generation=1,value=dict(kind='peer',device_id=device,addresses=['tcp://127.0.0.1:22000']))
        service._transfer_message('native_signal',identifier,state,message)
        self.assertEqual(state['native_peer']['device_id'],device);self.assertNotIn('native',state)
        with self.assertRaisesRegex(ValueError,'身份'):
            service._transfer_message('native_signal',identifier,state,dict(generation=1,value=dict(kind='native',device_id=other,addresses=['tcp://127.0.0.1:22000'])))
        state['peer_out']=dict(type='native_signal',id=identifier,generation=1,value=dict(kind='peer',device_id=device,addresses=['tcp://127.0.0.1:22001']))
        sent=[];service._send_signal=lambda value:sent.append(value) or True
        service._recover_signaling();self.assertIn(state['peer_out'],sent)

    def test_unsupported_receiver_fails_the_waiting_worker_immediately(self):
        service=self.services[0];identifier='c'*32
        state=service._state('send',dict(id='d'*64,name='receiver'));service._pending[identifier]=state
        service._transfers[identifier]=_Transfer(identifier,'send','receiver')
        service._message(json.dumps(dict(schema_version=1,type='prepared',id=identifier,peer=dict(id='d'*64,name='old'))))
        self.assertTrue(state['prepared'].is_set());self.assertTrue(state['ready'].is_set());self.assertIn('更新',state['error'])

    def test_r2_handoff_waits_for_rebound_account_without_losing_the_accepted_offer(self):
        service=self.services[0];sent=[];service._send_signal=lambda value:sent.append(value) or True
        service.attach_account(self.accounts[0]);sent.clear();identifier='e'*32
        state=service._state('send',dict(id='f'*64,name='receiver'));state['account_device']=True;state['generation']=1
        service._pending[identifier]=state;service._transfers[identifier]=_Transfer(identifier,'send','receiver')
        message=dict(type='native_signal',id=identifier,generation=1,value=dict(kind='r2',id='fixture'))
        service._handle_command(message);self.assertEqual(sent,[]);self.assertEqual(state['cloud_out'],message)
        service._transfer_message('resumed',identifier,state,dict(accepted=True,generation=1,account_device=False))
        self.assertTrue(state['account_device']);self.assertFalse(service._same_account(state))
        service._bound_identity=service._account_identity();service._recover_signaling()
        self.assertIn(message,sent);self.assertTrue(service._same_account(state))

    def test_blocked_direct_route_uses_real_r2_and_only_confirms_after_save(self):
        self.connect(account=True);source=self.root/'fallback.bin';source.write_bytes(bytes(range(256))*32768)
        target=self.root/'received';target.mkdir();receiver=self.services[1]
        receiver.offer_received.connect(lambda offer:receiver.accept(offer['id'],target) if offer.get('account_device') else None)
        with patch('wan_transfer.ReceiveJob.receive',side_effect=DirectUnavailable('QA blocked direct route')):
            identifier=self.services[0].send(receiver.device_id,[source]);self.wait_completed(identifier,90)
        self.assertEqual((target/source.name).read_bytes(),source.read_bytes())
        self.assertTrue(any(row.get('route')=='cloud' for side,row in self.progress if row['id']==identifier))
        until(lambda:not self.services[0]._transfers[identifier].active,20)
        feed=self.call(self.accounts[1].api,'GET','/v1/sync/changes?cursor=0')
        self.assertEqual(feed['changes'],[]);self.assertEqual(feed['transfers'],[])

    def test_engine_error_remains_visible_after_server_cancel_echo(self):
        self.connect();source=self.root/'failure.bin';source.write_bytes(b'failure fixture')
        target=self.root/'received';target.mkdir();receiver=self.services[1]
        receiver.offer_received.connect(lambda offer:receiver.accept(offer['id'],target))
        with patch.object(receiver.engine,'share',side_effect=EngineError('QA native listener failure')):
            identifier=self.services[0].send(receiver.code,[source])
            until(lambda:any(side==1 and row['id']==identifier and row['state']=='error' for side,row in self.progress),30)
            until(lambda:all(identifier in s._transfers and not s._transfers[identifier].active for s in self.services),15)
            for _ in range(10):APP.processEvents();time.sleep(.02)
        rows=[row for side,row in self.progress if side==1 and row['id']==identifier]
        self.assertEqual(rows[-1]['state'],'error');self.assertIn('native listener failure',rows[-1]['message'])
        self.assertTrue(receiver._transfers[identifier].cancel.is_set());self.assertTrue(source.is_file())
        self.assertTrue(receiver._pending[identifier]['failed'])


if __name__=='__main__':unittest.main()
