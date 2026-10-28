"""WAN recovery/security contracts and actual bounded native host-ICE stream."""
import hashlib,json,os,sys,tempfile,threading,time,unittest
from pathlib import Path
from uuid import uuid4
from PySide6.QtWidgets import QApplication
from wan_transfer import WanTransferService
from webrtc_stream import WebRTCStream

APP=QApplication.instance() or QApplication([])

class WanRecovery(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.root=Path(self.temporary.name)
        self.service=WanTransferService(self.root,enabled=False);self.sent=[]
        self.service._send_signal=lambda value:self.sent.append(dict(value)) or True
        self.identifier=uuid4().hex
    def tearDown(self):self.service.close();self.temporary.cleanup()
    def state(self,role='send'):
        value=dict(role=role,peer=dict(id='a'*64,name='测试设备'),ready=threading.Event(),generation=1,requested=True,total=0,
            request=dict(type='request',id=self.identifier,token='b'*64,digest='c'*64,total='0',count=0,names=[]))
        self.service._pending[self.identifier]=value;return value
    def message(self,**value):self.service._message(json.dumps(dict(schema_version=1,id=self.identifier,**value)))
    def test_identity_survives_restart_and_requires_no_login(self):
        identity=self.service.device_id;other=WanTransferService(self.root,enabled=False)
        try:self.assertEqual(identity,other.device_id);self.assertEqual(hashlib.sha256((self.root/'wan-device.key').read_bytes()).hexdigest(),identity)
        finally:other.close()
    def test_request_not_acknowledged_by_local_queue(self):
        state=self.state();state.pop('requested');request=dict(state['request'])
        self.service._handle_command(dict(_connect=True,**request))
        self.assertFalse(state.get('requested',False))
        self.service._recover_signaling();self.assertEqual([row['type'] for row in self.sent],['request','request'])
        self.message(type='waiting',peer=state['peer']);self.assertTrue(state['requested'])
    def test_reconnect_and_outgoing_retry_merge(self):
        state=self.state();self.service._recover_signaling();self.service._handle_command(dict(_connect=True,**state['request']))
        for _ in range(2):self.message(type='resumed',accepted=True,generation=1,settled=False)
        self.assertEqual(sum(row['type']=='retry' for row in self.sent),1)
    def test_stale_signal_error_cannot_end_new_generation(self):
        state=self.state();state['generation']=3
        self.message(type='error',generation=1,requestType='candidate',message='旧信令')
        self.assertFalse(state['ready'].is_set());self.assertNotIn('error',state)
    def test_rejected_offer_does_not_resurrect_on_reconnect(self):
        state=self.state('receive');self.service.cancel(self.identifier);APP.processEvents()
        self.assertIn((self.identifier,'cancel'),self.service._outbox)
        self.service._recover_signaling();offers=[];self.service.offer_received.connect(offers.append)
        self.message(type='request');self.assertEqual(offers,[])
        self.message(type='cancelled');self.assertNotIn((self.identifier,'cancel'),self.service._outbox)
    def test_expired_accept_retires_receiver_without_worker(self):
        self.state('receive');events=[];self.service.progress.connect(events.append)
        self.message(type='error',requestType='accept',message='请求已失效')
        self.assertNotIn(self.identifier,self.service._pending)
        self.assertEqual(events[-1]['state'],'error');self.assertEqual(events[-1]['message'],'请求已失效')
    def test_final_confirmation_cache_waits_for_both_endpoints(self):
        state=self.state('receive');self.service._completed_wan(dict(id=self.identifier))
        self.assertTrue(state['done']);self.assertIn(self.identifier,self.service._pending)
        self.message(type='settled');self.assertNotIn(self.identifier,self.service._pending)

class NativeStream(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).parent/'p2p/runtime'/('node.exe' if sys.platform=='win32' else 'node')
        if not root.is_file():self.skipTest('Build host must prepare pinned WebRTC assets')
        self.peers=[];self.closing=False;self.candidates=[]
        def signal(index,value):
            if self.closing:return
            if value.get('type') in ('candidate','description'):
                if index==1 and value['type']=='candidate':self.candidates.append(value)
                try:self.peers[1-index].control(value)
                except OSError:pass
        self.peers=[WebRTCStream(lambda value:signal(0,value)),WebRTCStream(lambda value:signal(1,value))]
        self.peers[1].control(dict(type='init',iceServers=[],initiator=False))
        self.peers[0].control(dict(type='init',iceServers=[],initiator=True))
        self.assertTrue(all(peer.opened.wait(12) for peer in self.peers),'Native host ICE failed')
    def tearDown(self):
        if not hasattr(self,'peers'):return
        self.closing=True
        for peer in self.peers:peer.close(abort=True)
    def test_ordered_close_preserves_last_ack(self):
        original=os.urandom(2*1024*1024-2);received=bytearray();failure=[]
        def consume():
            try:
                while len(received)<len(original):
                    try:part=self.peers[1].recv(65536)
                    except TimeoutError:continue
                    if not part:break
                    received.extend(part)
            except Exception as error:failure.append(type(error).__name__)
        receiver=threading.Thread(target=consume,daemon=True);receiver.start()
        self.peers[0].sendall(original);self.peers[0].close();receiver.join(10)
        self.assertFalse(receiver.is_alive());self.assertEqual(failure,[]);self.assertEqual(hashlib.sha256(received).digest(),hashlib.sha256(original).digest())
    def test_slow_consumer_does_not_block_ui_control_or_cancel(self):
        stopped=threading.Event()
        def send():
            try:
                for _ in range(64):self.peers[0].sendall(b'x'*(1024*1024))
            except OSError:pass
            finally:stopped.set()
        sender=threading.Thread(target=send,daemon=True);sender.start();deadline=time.monotonic()+8
        while self.peers[1]._size<8*1024*1024 and time.monotonic()<deadline:time.sleep(.01)
        self.assertLessEqual(self.peers[1]._size,8*1024*1024)
        self.assertTrue(self.candidates);started=time.monotonic();self.peers[0].control(self.candidates[0])
        self.assertLess(time.monotonic()-started,1)
        self.peers[0].close(abort=True);sender.join(3);self.assertFalse(sender.is_alive())

if __name__=='__main__':unittest.main()
