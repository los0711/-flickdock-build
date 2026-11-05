"""P2P establishment, cloud recovery and late-result lifecycle boundaries."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from uuid import uuid4
from PySide6.QtCore import QObject,Signal,QCoreApplication,QEvent
from PySide6.QtWidgets import QApplication
from peer_sync import PeerSync,P2P_CONNECT_TIMEOUT_SECONDS
from wan_transfer import WanTransferService,DIRECT_CONNECT_GRACE_MS,P2P_CONNECT_TIMEOUT_MS
from webrtc_stream import WebRTCStream
import hashlib

APP=QApplication.instance() or QApplication([])

class Account(QObject):
    account_changed=Signal(object)
    def __init__(self):
        super().__init__();self.signed_in=True;self.account={'id':uuid4().hex};self.device_id=uuid4().hex
        self.api=SimpleNamespace(token=uuid4().hex)

class P2PPriorityTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.base=Path(self.temp.name);self.identifier=uuid4().hex
        self.service=WanTransferService(self.base,enabled=False);self.sent=[];self.jobs=[];self.mesh=None
        self.service._send_signal=lambda value:self.sent.append(value) or True
        self.service._thread=lambda action,*args:self.jobs.append((action,args))
        self.service.cloud_routes=Mock()
        self.bridge=Mock();self.bridge.opened=threading.Event();self.bridge.closed=False;self.bridge.route='direct'
        self.state=dict(role='send',peer={'id':'f'*64,'name':'QA peer'},generation=1,ready=threading.Event(),
                        bridge=self.bridge,adaptive_allowed=True,relay_allowed=True)
        self.service._pending[self.identifier]=self.state

    def tearDown(self):
        if self.mesh:self.mesh.close();self.mesh.deleteLater()
        self.service.close();self.service.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);APP.processEvents();self.temp.cleanup()

    def native(self,**event):self.service._handle_native(dict(id=self.identifier,generation=self.state['generation'],event=event))

    def test_direct_negotiation_has_no_early_cloud_timer(self):
        with patch('wan_transfer.WebRTCStream',return_value=self.bridge),patch('wan_transfer.QTimer.singleShot') as timer:
            self.service._begin(self.identifier,self.state,dict(generation=2,iceServers=[],relayAllowed=True,adaptive=True))
        self.assertEqual([call.args[0] for call in timer.call_args_list],[DIRECT_CONNECT_GRACE_MS,P2P_CONNECT_TIMEOUT_MS])
        self.assertFalse(self.jobs);self.service.cloud_routes.open.assert_not_called()

    def test_direct_failure_attempts_turn_before_cloud_and_turn_rejection_recovers(self):
        self.native(type='state',state='failed')
        self.assertEqual(self.sent[-1]['type'],'relay');self.assertFalse(self.jobs)
        self.service._message(__import__('json').dumps(dict(schema_version=1,type='error',id=self.identifier,
            generation=1,requestType='relay',message='Relay unavailable')))
        self.assertTrue(self.state['cloud_starting']);self.assertEqual(len(self.jobs),1)
        self.assertFalse(self.state['ready'].is_set())

    def test_timeout_is_bounded_and_does_not_replace_a_late_native_connection(self):
        self.service._p2p_timed_out(self.identifier,0);self.assertFalse(self.jobs)
        self.service._p2p_timed_out(self.identifier,1);self.assertEqual(len(self.jobs),1)
        stream=Mock();descriptor=dict(id=uuid4().hex,key='a'*64)
        self.bridge.opened.set();self.native(type='open',route='direct')
        with patch.object(self.service,'_attach_cloud') as attach:
            self.native(type='cloud_ready',stream=stream,descriptor=descriptor)
        stream.shutdown.assert_called_once();attach.assert_not_called()
        self.assertFalse(any(value['type']=='cloud_path' for value in self.sent))

    def test_cancelled_and_replaced_generations_never_start_cloud(self):
        self.state['cancelled']=True;self.service._p2p_timed_out(self.identifier,1)
        self.service._bootstrap_cloud(self.identifier,1);self.assertFalse(self.jobs)
        self.state.pop('cancelled');self.state['generation']=2
        self.service._bootstrap_cloud(self.identifier,1);self.assertFalse(self.jobs)
        stream=Mock();self.service._handle_native(dict(id=self.identifier,generation=1,event=dict(type='cloud_ready',stream=stream)))
        stream.shutdown.assert_called_once()

    def make_mesh(self):
        account=Account();self.account=account;self.service._connected=True
        self.service._bound_identity=(account.device_id,account.api.token)
        mesh=PeerSync(account,self.service);self.mesh=mesh;mesh.timer.stop()
        now=time.monotonic();state=dict(id=uuid4().hex,peer='f'*64,epoch=mesh.epoch,started=now,leased=now,last_lease=now,
            cancel=threading.Event(),raw=self.bridge,rpc=None,route='connecting',initiator=True)
        mesh._states[state['peer']]=state;mesh._submit=lambda action,**options:self.jobs.append((action,())) or True
        return mesh,state

    def test_account_preconnection_waits_for_p2p_before_cloud(self):
        mesh,state=self.make_mesh();state['started']-=4;mesh._tick();self.assertFalse(self.jobs)
        state['started']=time.monotonic()-P2P_CONNECT_TIMEOUT_SECONDS-1;mesh._tick()
        self.assertEqual(len(self.jobs),1);self.assertTrue(state['cloud_starting'])

    def test_account_cloud_result_cannot_race_an_open_native_stream(self):
        mesh,state=self.make_mesh();self.bridge.opened.set();mesh._start_cloud(state);self.assertFalse(self.jobs)
        stream=Mock()
        with patch.object(mesh,'_attach_cloud') as attach:
            mesh._handle_native(dict(state=state,event=dict(type='cloud_ready',stream=stream,descriptor={})))
        stream.shutdown.assert_called_once();attach.assert_not_called()

class NativeCandidateOrderTests(unittest.TestCase):
    def test_real_ice_connects_when_candidate_arrives_before_remote_description(self):
        peers=[];offer=[];reordered=[];errors=[];closing=False
        def event(index,value):
            if closing:return
            kind=value.get('type')
            if kind=='error':errors.append(value.get('message'))
            if kind=='description' and index==0 and not offer:
                offer.append(value);return
            if kind in ('description','candidate'):
                try:
                    peers[1-index].control(value)
                    if kind=='candidate' and index==0 and offer:
                        reordered.append(value);peers[1].control(offer.pop())
                except OSError as error:errors.append(str(error))
        try:
            peers.extend((WebRTCStream(lambda value:event(0,value)),WebRTCStream(lambda value:event(1,value))))
            peers[1].control(dict(type='init',iceServers=[],initiator=False));peers[0].control(dict(type='init',iceServers=[],initiator=True))
            self.assertTrue(all(peer.opened.wait(8) for peer in peers),errors)
            self.assertTrue(reordered);self.assertFalse(errors)
            body=os.urandom(1024*1024+19);peers[0].sendall(body);received=bytearray();peers[1].settimeout(2)
            while len(received)<len(body):
                chunk=peers[1].recv(len(body)-len(received));self.assertTrue(chunk);received.extend(chunk)
            self.assertEqual(hashlib.sha256(received).digest(),hashlib.sha256(body).digest())
        finally:
            closing=True
            for peer in peers:peer.close(abort=True)

if __name__=='__main__':unittest.main()
