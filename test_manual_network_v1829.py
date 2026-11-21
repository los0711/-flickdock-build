"""Connection attempts, local consent and proxy changes without desktop effects."""
import os,json,tempfile,threading,time,unittest
os.environ['QT_QPA_PLATFORM']='offscreen'
from pathlib import Path
from unittest.mock import Mock,patch
from PySide6.QtNetwork import QNetworkProxy
from PySide6.QtWidgets import QApplication
from wan_transfer import WanTransferService
from manual_network import relay_proxy
from network_environment import proxy_routes
APP=QApplication.instance() or QApplication([])

class ManualNetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.service=WanTransferService(self.temp.name,enabled=True)
        self.sent=[];self.service._send_signal=lambda value:self.sent.append(value) or True
    def tearDown(self):self.service.close();self.service.deleteLater();APP.processEvents();self.temp.cleanup()

    def test_send_queues_when_signaling_is_disconnected_without_an_online_gate(self):
        service=self.service;service._peers={'a'*64:dict(id='a'*64,name='Mac',online=False)}
        with patch.object(service,'reconnect') as reconnect,patch.object(service,'_thread') as worker:
            identifier=service.send('a'*64,[str(Path(self.temp.name)/'source')])
            reconnect.assert_called_once();worker.assert_called_once()
        self.assertIn(identifier,service._pending);self.assertNotIn('prepare',service._pending[identifier])
        self.assertFalse(service._pending[identifier]['ready'].is_set())

    def test_disconnect_keeps_an_established_file_stream_alive(self):
        bridge=Mock();bridge.opened.is_set.return_value=True;bridge.closed=False
        self.service._pending['b'*32]=dict(role='send',bridge=bridge,generation=1)
        self.service._disconnected()
        bridge.close.assert_not_called();bridge.shutdown.assert_not_called()
        self.assertIn('b'*32,self.service._pending)

    def test_proxy_change_reconnects_immediately_without_polling_device_status(self):
        direct=QNetworkProxy(QNetworkProxy.NoProxy);proxy=QNetworkProxy(QNetworkProxy.HttpProxy,'127.0.0.1',7890)
        self.service._proxy_routes=[proxy,direct];self.service._last_proxy_check=0
        with patch('wan_transfer.proxy_routes',return_value=[direct]),patch.object(self.service,'reconnect') as reconnect:
            self.service.check_code();reconnect.assert_called_once()
        self.assertFalse(self.sent)

    def test_failed_system_proxy_attempt_selects_direct_route(self):
        direct=QNetworkProxy(QNetworkProxy.NoProxy);proxy=QNetworkProxy(QNetworkProxy.HttpProxy,'127.0.0.1',7890)
        with patch('wan_transfer.proxy_routes',return_value=[proxy,direct]),patch.object(self.service.socket,'open'):
            self.service._connect();self.assertEqual(self.service.socket.proxy().type(),QNetworkProxy.HttpProxy)
            self.service._connection_timed_out();self.service.retry.stop();self.service._connect()
            self.assertEqual(self.service.socket.proxy().type(),QNetworkProxy.NoProxy)

    def test_slow_description_exchange_does_not_consume_actual_ice_check_time(self):
        bridge=Mock();bridge.opened.is_set.return_value=False
        state=dict(role='send',bridge=bridge,generation=1,relay_allowed=True,connect_started=100.,ready=threading.Event())
        self.service._pending['c'*32]=state
        with patch('wan_transfer.time.monotonic',return_value=108.),patch('wan_transfer.QTimer.singleShot') as later:
            self.service._relay_if_needed('c'*32,1);later.assert_called_once()
        self.assertFalse(self.sent);state['remote_description_at']=109.;state['gathered_at']=109.
        with patch('wan_transfer.time.monotonic',return_value=110.),patch('wan_transfer.QTimer.singleShot'):
            self.service._relay_if_needed('c'*32,1)
        self.assertFalse(self.sent)
        with patch('wan_transfer.time.monotonic',return_value=117.):self.service._relay_if_needed('c'*32,1)
        self.assertEqual(self.sent[-1]['type'],'relay')

    def test_missing_signaling_is_not_misreported_as_a_nat_relay_problem(self):
        bridge=Mock();bridge.opened.is_set.return_value=False
        state=dict(role='send',bridge=bridge,generation=1,relay_allowed=True,connect_started=100.,ready=threading.Event())
        self.service._pending['d'*32]=state
        with patch('wan_transfer.time.monotonic',return_value=121.):self.service._relay_if_needed('d'*32,1)
        self.assertTrue(state['ready'].is_set());self.assertIn('连接信息',state['error']);self.assertFalse(self.sent)

    def test_one_failed_mapping_restarts_direct_once_before_requesting_relay(self):
        bridge=Mock();bridge.opened.is_set.return_value=False
        identity='e'*32
        state=dict(role='send',bridge=bridge,generation=1,relay_allowed=True,connect_started=100.,
            remote_description_at=100.,gathered_at=100.,direct_attempts=1,direct_deadline=120.,ready=threading.Event())
        self.service._pending[identity]=state
        with patch('wan_transfer.time.monotonic',return_value=108.):
            self.service._relay_if_needed(identity,1)
            self.service._relay_if_needed(identity,1,failed=True)
        self.assertEqual(self.sent,[dict(type='retry',id=identity)])
        state.update(generation=2,retry_inflight=False,direct_attempts=2,connect_started=108.,remote_description_at=109.,gathered_at=109.)
        with patch('wan_transfer.time.monotonic',return_value=117.):self.service._relay_if_needed(identity,2)
        self.assertEqual(self.sent[-1],dict(type='relay',id=identity,generation=2))
        self.assertFalse(state['relay_allowed']);self.assertEqual(state['direct_deadline'],120.)

    def test_direct_restart_changes_stun_mapping_without_resetting_total_wait_budget(self):
        from wan_transfer import STUN_SERVERS
        identity='f'*32;state=dict(role='receive',peer=dict(id='a'*64),ready=threading.Event(),generation=0)
        self.service._pending[identity]=state
        first=Mock(route='direct');second=Mock(route='direct')
        with patch('wan_transfer.WebRTCStream',side_effect=[first,second]),patch('wan_transfer.time.monotonic',return_value=100.):
            self.service._begin(identity,state,dict(generation=1,initiator=self.service.device_id,iceServers=[],relayAllowed=True))
        self.service._handle_native(dict(id=identity,generation=1,event=dict(type='stun_selected',server=STUN_SERVERS[0])))
        with patch('wan_transfer.WebRTCStream',return_value=second),patch('wan_transfer.time.monotonic',return_value=109.):
            self.service._begin(identity,state,dict(generation=2,initiator=self.service.device_id,iceServers=[],relayAllowed=True))
        self.assertEqual(second.control.call_args.args[0]['preferredStun'],STUN_SERVERS[1])
        self.assertEqual(state['direct_deadline'],120.);self.assertEqual(state['direct_attempts'],2)

    def test_proxy_selection_preserves_credentials_only_in_memory_and_direct_route_exists(self):
        proxy=QNetworkProxy(QNetworkProxy.HttpProxy,'127.0.0.1',7890,'test-user','test-secret')
        with patch('network_environment.QNetworkProxyFactory.systemProxyForQuery',return_value=[proxy]):
            routes=proxy_routes('https://flickdock.cc.cd');self.assertEqual(len(routes),2)
            self.assertEqual(relay_proxy(),dict(kind='http',host='127.0.0.1',port=7890,username='test-user',password='test-secret'))

    def test_guest_lan_runtime_does_not_start_cloud_signaling(self):
        from PySide6.QtCore import QObject,Signal
        from PySide6.QtTest import QTest
        from transfer_runtime import ManualTransferRuntime
        class Account(QObject):
            account_changed=Signal(object);devices_changed=Signal(object)
            signed_in=False;device_name='Guest';device_id='fixture';account=None
            api=Mock(token='')
        account=Account();runtime=ManualTransferRuntime(Path(self.temp.name)/'guest',APP,account,enabled=True)
        try:
            with patch.object(runtime.lan,'start'),patch.object(runtime.wan.socket,'open') as opened:
                runtime.start();runtime.wan.reconnect();runtime.wan._connect();QTest.qWait(50)
                opened.assert_not_called();account.api.request.assert_not_called()
                self.assertFalse(runtime.wan._signaling_allowed)
                runtime.enable_cloud_signaling();QTest.qWait(50)
                opened.assert_called_once()
        finally:runtime.close()

    def test_two_guest_runtimes_transfer_on_loopback_without_cloud_requests(self):
        from PySide6.QtCore import QObject,Signal
        from transfer_runtime import ManualTransferRuntime
        from test_lan_transfer_v181 import wait_for
        class Account(QObject):
            account_changed=Signal(object);devices_changed=Signal(object)
            signed_in=False;device_name='Guest';device_id='fixture';account=None
            def __init__(self):super().__init__();self.api=Mock(token='')
        accounts=[Account(),Account()]
        runtimes=[ManualTransferRuntime(Path(self.temp.name)/str(i),APP,accounts[i],enabled=True) for i in range(2)]
        a,b=(runtime.lan for runtime in runtimes)
        for service in (a,b):service.bind_host='127.0.0.1';service.discovery_enabled=False
        offers=[];sending=[];received=[]
        b.offer_received.connect(offers.append);a.progress.connect(sending.append);b.completed.connect(received.append)
        source=Path(self.temp.name)/'guest.bin';payload=b'guest local data'*4096;source.write_bytes(payload)
        destination=Path(self.temp.name)/'receive';destination.mkdir()
        try:
            with patch.object(runtimes[0].wan.socket,'open') as first,patch.object(runtimes[1].wan.socket,'open') as second:
                for runtime in runtimes:runtime.start()
                wait_for(lambda:a.port and b.port)
                a.remember_peer(b.own_peer())
                identity=a.send(b.device_id,[source]);wait_for(lambda:offers and any(s['state']=='waiting' for s in sending))
                self.assertEqual(offers[0]['code'],next(s['code'] for s in sending if s['state']=='waiting'))
                self.assertEqual(list(destination.iterdir()),[])
                b.accept(identity,destination,confirmation=next(s['code'] for s in sending if s['state']=='waiting'));wait_for(lambda:received)
                self.assertEqual(Path(received[0]['paths'][0]).read_bytes(),payload)
                first.assert_not_called();second.assert_not_called()
                for account in accounts:account.api.request.assert_not_called()
        finally:
            for runtime in runtimes:runtime.close()
            for service in (a,b):
                for thread in list(service._threads):thread.join(timeout=1.2)

if __name__=='__main__':unittest.main()
