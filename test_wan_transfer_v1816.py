"""WAN recovery/security contracts and actual bounded native host-ICE stream."""
import hashlib,json,os,subprocess,sys,tempfile,threading,time,unittest
from unittest.mock import patch
from pathlib import Path
from uuid import uuid4
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QCoreApplication,QEvent
from wan_transfer import WanTransferService
from webrtc_stream import WebRTCStream

APP=QApplication.instance() or QApplication([])

class WanRecovery(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.root=Path(self.temporary.name)
        self.service=WanTransferService(self.root,enabled=False);self.sent=[]
        self.service._send_signal=lambda value:self.sent.append(dict(value)) or True
        self.identifier=uuid4().hex
    def tearDown(self):
        self.service.close();self.service.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);APP.processEvents()
        self.temporary.cleanup()
    def state(self,role='send'):
        value=dict(role=role,peer=dict(id='a'*64,name='测试设备'),ready=threading.Event(),generation=1,requested=True,total=0,
            request=dict(type='request',id=self.identifier,token='b'*64,digest='c'*64,total='0',count=0,names=[]))
        self.service._pending[self.identifier]=value;return value
    def message(self,**value):self.service._message(json.dumps(dict(schema_version=1,id=self.identifier,**value)))
    def test_identity_survives_restart_and_requires_no_login(self):
        identity=self.service.device_id;other=WanTransferService(self.root,enabled=False)
        try:self.assertEqual(identity,other.device_id);self.assertEqual(hashlib.sha256((self.root/'wan-device.key').read_bytes()).hexdigest(),identity)
        finally:other.close();other.deleteLater()

    def test_incoming_request_uses_the_signaled_sender_device_code(self):
        offers=[];self.service.offer_received.connect(offers.append)
        self.message(type='request',peer=dict(id='d'*64,name='发送方'),token='b'*64,digest='c'*64,total='12',count=1,names=['file.txt'],deviceCode='ABCD2345')
        self.assertEqual(offers[0]['device_code'],'ABCD2345')
    def test_refresh_deadline_restarts_from_server_identity_not_socket_start(self):
        self.service._connected=True;identities=[];self.service.identity_changed.connect(identities.append)
        with patch('wan_transfer.time.monotonic',return_value=1000):
            self.message(type='identity',code='ABCD2345',serverNow=100000,refreshAt=340000,expires=400000,recent=[])
        with patch('wan_transfer.time.monotonic',return_value=1239):
            self.service.check_code();self.assertFalse(self.sent)
            self.assertEqual(self.service.code_identity()['refresh_remaining'],1)
        with patch('wan_transfer.time.monotonic',return_value=1241):
            self.service.check_code();self.assertEqual(self.sent[-1],dict(type='refresh'))
            self.service.check_code();self.assertEqual(len(self.sent),1)
            self.message(type='identity',code='EFGH6789',serverNow=500000,refreshAt=740000,expires=800000,recent=[])
        with patch('wan_transfer.time.monotonic',return_value=1242):
            self.assertEqual(self.service.code_identity()['refresh_remaining'],239)
            self.assertEqual(self.service.code_identity()['code'],'EFGH6789')
        self.assertEqual(identities[-1]['code'],'EFGH6789')
    def test_unanswered_refresh_reconnects_instead_of_leaving_an_expired_code(self):
        self.service._connected=True
        with patch('wan_transfer.time.monotonic',return_value=1000):
            self.message(type='identity',code='ABCD2345',serverNow=0,refreshAt=240000,expires=300000,recent=[])
        with patch('wan_transfer.time.monotonic',return_value=1240):self.service.check_code()
        with patch('wan_transfer.time.monotonic',return_value=1253):self.service.check_code()
        self.assertFalse(self.service._connected);self.assertEqual(self.service.code_identity()['code'],'')
    def test_valid_code_is_reserved_before_folder_preparation(self):
        self.service._connected=True;workers=[];outgoing=[]
        self.service._thread=lambda target,*args:workers.append((target,args))
        self.service._outgoing=lambda *args:outgoing.append(args)
        identifier=self.service.send('ABCD2345',[str(self.root)])
        self.assertEqual(self.sent[0]['type'],'prepare');self.assertFalse(outgoing)
        self.service._message(json.dumps(dict(schema_version=1,type='prepared',id=identifier,peer=dict(id='d'*64,name='Mac'))))
        workers[0][0](*workers[0][1])
        self.assertEqual(outgoing[0][1],dict(id='d'*64,name='Mac'))
        self.assertTrue(self.service._pending[identifier]['prepared_ok'])
        self.assertEqual(self.sent[0]['token'],outgoing[0][0].token)
    def test_cancellation_before_preparation_worker_releases_the_send_slot(self):
        self.service._connected=True;workers=[];events=[]
        self.service._thread=lambda target,*args:workers.append((target,args))
        self.service.progress.connect(events.append)
        identifier=self.service.send('ABCD2345',[str(self.root)])
        self.service.cancel(identifier);workers[0][0](*workers[0][1]);APP.processEvents()
        self.assertFalse(self.service._transfers[identifier].active)
        self.assertEqual(events[-1]['state'],'cancelled')
    def test_recent_device_send_ignores_expired_code_and_delete_waits_for_server(self):
        self.service._connected=True;self.service.code='';workers=[]
        self.service._peers={'d'*64:dict(id='d'*64,name='Mac',online=True)}
        self.service._thread=lambda target,*args:workers.append((target,args))
        identifier=self.service.send('d'*64,[str(self.root)])
        self.assertFalse(self.sent);self.assertNotIn('prepare',self.service._pending[identifier])
        self.service.remove_peer('d'*64);self.assertEqual(self.sent[-1],dict(type='remove',peer='d'*64))
        self.message(type='peers',peers=[]);self.assertEqual(self.service.peers(),[])
    def test_offline_recent_device_cannot_start_a_transfer_and_disconnect_updates_status(self):
        self.service._connected=True;changes=[];self.service.peers_changed.connect(changes.append)
        self.service._peers={'d'*64:dict(id='d'*64,name='Mac',online=False)}
        with self.assertRaisesRegex(ValueError,'不在线，无法发送'):self.service.send('d'*64,[str(self.root)])
        self.assertEqual(self.service._transfers,{})
        self.service._peers['d'*64]['online']=True;self.service._disconnected()
        self.assertFalse(changes[-1][0]['online'])
    def test_code_circle_and_delete_button_use_live_identity_in_both_themes(self):
        from app_settings import SettingsStore
        from lan_panel import LanDialog
        self.service._connected=True
        dialog=LanDialog(self.service,SettingsStore(self.root/'settings'))
        try:
            dialog.show();APP.processEvents()
            with patch('wan_transfer.time.monotonic',return_value=1000):
                self.message(type='identity',code='ABCD2345',serverNow=0,refreshAt=240000,expires=300000,
                    recent=[dict(id='d'*64,name='Mac',online=True)])
                panel=dialog.panel
                for theme in ('light','dark'):
                    dialog.apply_theme(theme);APP.processEvents();self.assertFalse(panel.code_countdown.grab().isNull())
                    self.assertTrue(panel.send_code_value.isEnabled())
                    self.assertEqual(panel.send_code_value.text(),'ABCD2345')
                    self.assertLessEqual(abs(panel.send_code_value.geometry().center().y()-panel.code_countdown.geometry().center().y()),1)
                panel.remove_recent_button.click();self.assertEqual(self.sent[-1],dict(type='remove',peer='d'*64))
                self.message(type='peers',peers=[]);self.assertFalse(panel.remove_recent_button.isEnabled())
            QApplication.clipboard().setText('not a code')
            with patch('wan_transfer.time.monotonic',return_value=1301):
                panel.send_code_value._copy();self.assertEqual(QApplication.clipboard().text(),'not a code')
                panel.code_countdown._tick();self.assertFalse(panel.send_code_value.isEnabled())
        finally:dialog.close();dialog.deleteLater();APP.processEvents()
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

    def test_failed_tls_allocation_switches_next_generation_to_supplied_udp(self):
        state=self.state();fake=unittest.mock.Mock();fake.opened.is_set.return_value=False
        state.update(bridge=fake,relay_transport='tls',relay_allowed=False)
        self.service._handle_native(dict(id=self.identifier,generation=1,event=dict(type='state',state='failed')))
        self.assertTrue(state['ready'].is_set());self.assertTrue(state['tls_failed'])
        with patch('wan_transfer.WebRTCStream',return_value=fake):
            self.service._begin(self.identifier,state,dict(generation=2,iceServers=[],relayAllowed=False))
        self.assertFalse(fake.control.call_args.args[0]['preferTls'])

    def test_direct_session_does_not_resolve_a_proxy(self):
        state=self.state();fake=unittest.mock.Mock()
        with patch('wan_transfer.WebRTCStream',return_value=fake),patch('wan_transfer._turn_http_proxy',side_effect=AssertionError('Direct P2P must not query proxy')):
            self.service._begin(self.identifier,state,dict(generation=2,iceServers=[],relayAllowed=False))
        self.assertEqual(fake.control.call_args.args[0]['proxies'],{})

    def test_turn_uses_qt_proxy_selection_and_honors_bypass(self):
        from PySide6.QtNetwork import QNetworkProxy
        from wan_transfer import _turn_http_proxy
        proxy=QNetworkProxy(QNetworkProxy.ProxyType.HttpProxy,'127.0.0.1',7890,'qa-user','qa-secret')
        bypass=QNetworkProxy(QNetworkProxy.ProxyType.NoProxy)
        with patch('wan_transfer.QNetworkProxyFactory.proxyForQuery',return_value=[proxy,bypass]) as query:
            self.assertEqual(_turn_http_proxy(5349),dict(type='http',host='127.0.0.1',port=7890,username='qa-user',password='qa-secret'))
            self.assertEqual(query.call_args.args[0].url().host(),'turn.cloudflare.com')
            self.assertEqual(query.call_args.args[0].url().port(),5349)
        with patch('wan_transfer.QNetworkProxyFactory.proxyForQuery',return_value=[bypass,proxy]):self.assertIsNone(_turn_http_proxy(443))
        proxy.setType(QNetworkProxy.ProxyType.Socks5Proxy)
        with patch('wan_transfer.QNetworkProxyFactory.proxyForQuery',return_value=[proxy]):
            self.assertEqual(_turn_http_proxy(443)['type'],'socks5')

    def test_selected_proxy_is_forwarded_to_the_native_tls_adapter(self):
        state=self.state();fake=unittest.mock.Mock();proxy=dict(type='http',host='127.0.0.1',port=7890)
        servers=[dict(urls=['turns:turn.cloudflare.com:443?transport=tcp','turns:turn.cloudflare.com:5349?transport=tcp'],username='qa',credential='private')]
        with patch('wan_transfer.WebRTCStream',return_value=fake),patch('wan_transfer._turn_http_proxy',side_effect=lambda port:proxy if port==443 else None):
            self.service._begin(self.identifier,state,dict(generation=2,iceServers=servers,relayAllowed=False))
        self.assertEqual(fake.control.call_args.args[0]['proxies'],{'443':proxy,'5349':None})

class TurnTLS(unittest.TestCase):
    def test_real_adapter_framing_and_transport_failure_contracts(self):
        root=Path(__file__).parent/'p2p';node=root/'runtime'/('node.exe' if sys.platform=='win32' else 'node')
        if not node.is_file():self.skipTest('Build host must prepare pinned WebRTC assets')
        result=subprocess.run([str(node),'--test',str(root/'test-turn-tls.mjs')],cwd=root,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('pass 16',result.stdout)

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

    def test_network_progress_counts_only_acknowledged_file_bytes(self):
        header=b'manifest-header';body=os.urandom(2*1024*1024);trailer=b'sha256-trailer'
        original=header+body+trailer;received=bytearray()
        def consume():
            while len(received)<len(original):
                try:part=self.peers[1].recv(65536)
                except TimeoutError:continue
                if not part:break
                received.extend(part)
        thread=threading.Thread(target=consume,daemon=True);thread.start()
        self.peers[0].sendall(header);self.peers[0].set_payload_offset(123)
        self.peers[0].send_payload(body);self.peers[0].sendall(trailer);thread.join(8)
        deadline=time.monotonic()+3
        while self.peers[0].payload_progress()<123+len(body) and time.monotonic()<deadline:
            self.peers[0].traffic(refresh=True);time.sleep(.01)
        self.assertEqual(bytes(received),original)
        self.assertEqual(self.peers[0].payload_progress(),123+len(body))
        self.assertFalse(self.peers[0]._payload_spans)

    def test_native_file_pipeline_preserves_folder_bytes_and_native_usage(self):
        from lan_transfer import LanTransferService,_Transfer
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'folder';source.mkdir();destination=root/'received';destination.mkdir()
            for i in range(20):(source/f'{i:02}.bin').write_bytes(bytes([i])*65536)
            sender=LanTransferService(discovery=False);receiver=LanTransferService(discovery=False)
            sender.mode=receiver.mode='wan'
            sender._connect_peer=lambda *args:self.peers[0];sender._stream_code=lambda *args:''
            receiver._incoming_stream=lambda stream:stream;receiver._stream_code=lambda *args:''
            def accept(transfer,*args):transfer.destination=destination;transfer.accepted.set()
            receiver._incoming_offer=accept
            identifier=uuid4().hex;transfer=_Transfer(identifier,'send','fixture',active=True)
            receiver._slots.acquire();thread=threading.Thread(target=receiver._incoming,args=(self.peers[1],'fixture'),daemon=True)
            thread.start();sender._outgoing(transfer,{},[source]);thread.join(12)
            self.assertTrue(transfer.finished);self.assertFalse(thread.is_alive())
            for path in source.iterdir():self.assertEqual((destination/source.name/path.name).read_bytes(),path.read_bytes())
            self.assertGreater(sum(self.peers[0].traffic().values()),0)
            self.assertFalse(any(t.name=='FlickDock-file-ack' and t.is_alive() for t in threading.enumerate()))
            sender.close();receiver.close();sender.deleteLater();receiver.deleteLater();APP.processEvents()
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
