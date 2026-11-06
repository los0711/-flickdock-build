"""Native DTLS upgrade after signaling overtakes the existing route's control."""
import hashlib,os,socket,threading,time,unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from adaptive_stream import AdaptiveStream
from webrtc_stream import WebRTCStream
from test_adaptive_routes_v1825 import Stream,until


class SecondarySignalingTests(unittest.TestCase):
    def test_signal_during_native_initialization_waits_for_init_and_retired_signals_are_ignored(self):
        first,second=socket.socketpair();raw=Stream(first);calls=[];holder=[];identifier=uuid4().hex
        signal=dict(kind='candidate',id=identifier,payload=dict(type='candidate',candidate='bounded-test'))
        def control(value):
            calls.append(value['type'])
            if value['type']=='init':holder[0]._secondary_signal(signal)
        candidate=SimpleNamespace(control=control,shutdown=lambda:None)
        with patch.object(AdaptiveStream,'_thread'):
            stream=AdaptiveStream(raw,raw,initiator=False,cancel=threading.Event(),allow_lan=False,
                secondary=lambda *args:(candidate,dict(type='init')))
        holder.append(stream)
        try:
            stream._secondary_signal(dict(kind='start',id=identifier,directOnly=True))
            self.assertEqual(calls,['init','candidate']);self.assertEqual(stream._early_secondary_bytes,0)
            with stream._condition:stream._retire_secondary(identifier)
            stream._secondary_signal(signal);self.assertEqual(calls,['init','candidate'])
            stream.shutdown();stream._secondary_signal(signal);self.assertFalse(stream._early_secondary)
        finally:stream.shutdown();second.close()

    def test_early_signal_queue_has_a_memory_bound_and_expires_old_attempts(self):
        from adaptive_stream import EARLY_SECONDARY_SIGNALS,EARLY_SECONDARY_BYTES,PATH_CONNECT_SECONDS
        first,second=socket.socketpair();raw=Stream(first)
        with patch.object(AdaptiveStream,'_thread'):
            stream=AdaptiveStream(raw,raw,initiator=False,cancel=threading.Event(),allow_lan=False)
        try:
            identifier=uuid4().hex
            value=dict(kind='candidate',id=identifier,payload=dict(type='description',sdp='x'*1000))
            stream._secondary_signal(value);stream._secondary_signal(value)
            self.assertEqual(len(stream._early_secondary),1)
            with patch('adaptive_stream.time.perf_counter',return_value=time.perf_counter()+PATH_CONNECT_SECONDS+1):
                with stream._condition:self.assertEqual(stream._take_secondary_signals(identifier),[])
            for _ in range(EARLY_SECONDARY_SIGNALS+1):
                stream._secondary_signal(dict(value,id=uuid4().hex))
            self.assertLessEqual(len(stream._early_secondary),EARLY_SECONDARY_SIGNALS)
            self.assertLessEqual(stream._early_secondary_bytes,EARLY_SECONDARY_BYTES)
        finally:stream.shutdown();second.close()

    def test_native_description_and_candidate_before_start_still_upgrade_the_route(self):
        cancel=threading.Event();streams=[];native=[];held=[];early=[];errors=[];timers=[]
        first,second=socket.socketpair();raw=[Stream(first),Stream(second)]
        for value in raw:value.route='relay'
        def signal(index,value):
            if cancel.is_set():return False
            if index==0 and value['kind']=='start':held.append(value);return True
            try:streams[1-index]._secondary_signal(value)
            except (ValueError,OSError) as error:errors.append(type(error).__name__)
            if index==0 and value['kind']=='candidate':
                early.append(value['payload']['type'])
                if value['payload']['type']=='candidate' and held:
                    start=held.pop(0)
                    def deliver():
                        if not cancel.is_set():streams[1]._secondary_signal(start)
                    timer=threading.Timer(.05,deliver);timers.append(timer);timer.start()
            return True
        def secondary(event,configuration):
            peer=WebRTCStream(event);native.append(peer)
            return peer,dict(configuration,iceServers=[],forceRelay=False)
        try:
            for index,value in enumerate(raw):
                streams.append(AdaptiveStream(value,value,initiator=index==0,cancel=cancel,
                    secondary=secondary,allow_lan=False,signal_out=lambda value,index=index:signal(index,value)))
            until(lambda:'description' in early and 'candidate' in early,5)
            until(lambda:all(any(not path.dead and path.route=='p2p' for path in stream._paths.values()) for stream in streams),8)
            self.assertIn('description',early);self.assertIn('candidate',early);self.assertFalse(errors)
            body=os.urandom(1024*1024+37);received=bytearray();write_errors=[]
            def send():
                try:streams[0].sendall(body)
                except OSError as error:write_errors.append(type(error).__name__)
            writer=threading.Thread(target=send);writer.start()
            deadline=time.monotonic()+5
            while len(received)<len(body) and time.monotonic()<deadline:
                try:received.extend(streams[1].recv(min(64*1024,len(body)-len(received))))
                except socket.timeout:continue
            writer.join(2);self.assertFalse(writer.is_alive());self.assertFalse(write_errors)
            self.assertEqual(hashlib.sha256(received).digest(),hashlib.sha256(body).digest())
            self.assertEqual(streams[0].route,'p2p');self.assertEqual(streams[1].received_route,'p2p')
            self.assertTrue(all(peer.route=='direct' for peer in native))
        finally:
            cancel.set()
            for timer in timers:timer.cancel();timer.join(1)
            for stream in streams:stream.shutdown()
            for peer in native:peer.close(abort=True)


if __name__=='__main__':unittest.main()
