"""Authenticated host ICE -> pinned TLS; original-byte and fallback coverage."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import hashlib,json,socket,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
import nearby_transport as nearby
import test_wan_transfer_v1816 as native
from lan_transfer import LanTransferService,_Transfer

class NearbyTests(unittest.TestCase):
    def setUp(self):
        self.fixture=native.NativeStream();self.fixture.setUp();self.raw=self.fixture.peers
    def tearDown(self):self.fixture.tearDown()
    def upgrade(self,change_offer=None,break_commit=False):
        results={};errors={};cancel=threading.Event()
        original=self.raw[1].control
        def control(value):
            if break_commit and value.get('type')=='nearby' and value['data'].get('state')=='committed':raise ConnectionError('Injected lost commit')
            return original(value)
        self.raw[1].control=control
        def receive():
            try:results['receive']=nearby.try_nearby(self.raw[1],'receive',cancel)
            except OSError as error:errors['receive']=type(error).__name__
        thread=threading.Thread(target=receive,daemon=True);thread.start()
        if change_offer:
            deadline=time.monotonic()+2
            while not self.raw[0]._nearby_messages and time.monotonic()<deadline:time.sleep(.002)
            with self.raw[0]._condition:change_offer(self.raw[0]._nearby_messages[0])
        try:results['send']=nearby.try_nearby(self.raw[0],'send',cancel)
        except OSError as error:errors['send']=type(error).__name__
        thread.join(5);self.assertFalse(thread.is_alive())
        return results,errors
    def roundtrip(self,streams):
        body=os.urandom(128*1024);result=bytearray()
        def consume():
            while len(result)<len(body):result.extend(streams['receive'].recv(65536))
        thread=threading.Thread(target=consume,daemon=True);thread.start();streams['send'].sendall(body);thread.join(5)
        self.assertFalse(thread.is_alive());self.assertEqual(bytes(result),body)

    def test_native_pinned_tls_file_pipeline_preserves_original_128_mib_and_progress(self):
        streams,errors=self.upgrade();self.assertFalse(errors)
        self.assertTrue(all(isinstance(stream,nearby.NearbyStream) for stream in streams.values()))
        self.assertIn(streams['send'].stream.version(),('TLSv1.2','TLSv1.3'))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source.bin';destination=root/'received';destination.mkdir()
            block=os.urandom(1024*1024)
            with source.open('wb') as output:
                for _ in range(128):output.write(block)
            expected=hashlib.sha256(block*128).hexdigest()
            sender=LanTransferService(discovery=False);receiver=LanTransferService(discovery=False)
            sender.mode=receiver.mode='wan'
            sender._connect_peer=lambda *args:streams['send'];sender._stream_code=lambda *args:''
            receiver._incoming_stream=lambda raw:streams['receive'];receiver._stream_code=lambda *args:''
            def accept(transfer,*args):transfer.destination=destination;transfer.accepted.set()
            receiver._incoming_offer=accept;transfer=_Transfer(uuid4().hex,'send','fixture',active=True)
            receiver._slots.acquire();thread=threading.Thread(target=receiver._incoming,args=(self.raw[1],'fixture'),daemon=True)
            thread.start();started=time.perf_counter();sender._outgoing(transfer,{},[source]);thread.join(120);elapsed=time.perf_counter()-started
            self.assertTrue(transfer.finished);self.assertFalse(thread.is_alive())
            actual=destination/source.name;self.assertEqual(actual.stat().st_size,128*1024*1024)
            self.assertEqual(hashlib.sha256(actual.read_bytes()).hexdigest(),expected)
            self.assertEqual(streams['send'].payload_progress(),128*1024*1024)
            print(json.dumps(dict(transport='nearby-pinned-tls',size_mib=128,seconds=round(elapsed,3),mib_per_second=round(128/elapsed,2),sha256_verified=True)),flush=True)
            sender.close();receiver.close();sender.deleteLater();receiver.deleteLater();native.APP.processEvents()

    def test_unreachable_socket_falls_back_to_same_original_datachannel(self):
        unused=socket.socket();unused.bind(('127.0.0.1',0));port=unused.getsockname()[1];unused.close()
        streams,errors=self.upgrade(lambda offer:offer.update(port=port))
        self.assertFalse(errors);self.assertIs(streams['send'],self.raw[0]);self.assertIs(streams['receive'],self.raw[1]);self.roundtrip(streams)

    def test_wrong_tls_pin_cannot_receive_payload_and_falls_back(self):
        streams,errors=self.upgrade(lambda offer:offer.update(pin='0'*64))
        self.assertFalse(errors);self.assertIs(streams['send'],self.raw[0]);self.assertIs(streams['receive'],self.raw[1]);self.roundtrip(streams)

    def test_failure_after_commit_retries_instead_of_mixing_two_byte_streams(self):
        streams,errors=self.upgrade(break_commit=True)
        self.assertEqual(streams,{})
        self.assertEqual(errors,dict(send='ConnectionError',receive='ConnectionError'))

    def test_cancel_closes_listener_without_waiting_for_handshake_deadline(self):
        ended=threading.Event()
        def receive():
            try:nearby.try_nearby(self.raw[1],'receive',threading.Event())
            except OSError:pass
            finally:ended.set()
        thread=threading.Thread(target=receive,daemon=True);thread.start()
        deadline=time.monotonic()+2
        while not self.raw[0]._nearby_messages and time.monotonic()<deadline:time.sleep(.002)
        started=time.monotonic();self.raw[1].close(abort=True);self.assertTrue(ended.wait(1));self.assertLess(time.monotonic()-started,1)

class NearbyBoundaryTests(unittest.TestCase):
    def test_legacy_public_relay_and_ipv6_peers_do_not_open_an_extra_socket(self):
        for capable,route,remote in ((False,'direct','192.168.1.3'),(True,'relay','192.168.1.3'),(True,'direct','8.8.8.8'),(True,'direct','::1')):
            raw=SimpleNamespace(route=route,_nearby_context=dict(capable=capable,local='192.168.1.2',remote=remote))
            with patch.object(nearby.socket,'socket',side_effect=AssertionError('Untrusted destination dialed')):
                self.assertIs(nearby.try_nearby(raw,'send',threading.Event()),raw)

if __name__=='__main__':unittest.main()
