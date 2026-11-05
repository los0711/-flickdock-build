"""Real socket bytes, measured selection, interruption, flow control and fences."""
import hashlib
import os
import socket
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from adaptive_stream import AdaptiveStream,HEADER,DATA,ACK,WINDOW_BYTES
from route_policy import RoutePolicy


def until(predicate,seconds=8):
    deadline=time.monotonic()+seconds
    while not predicate() and time.monotonic()<deadline:time.sleep(.005)
    if not predicate():raise AssertionError('Adaptive route verification did not settle')


class Stream:
    route='direct'
    def __init__(self,stream,delay=0.):self.stream=stream;self.delay=delay;self.closed=False
    def settimeout(self,value):self.stream.settimeout(value)
    def sendall(self,data):
        if self.delay and len(data)>1000:time.sleep(self.delay)
        self.stream.sendall(data)
    def recv(self,size):return self.stream.recv(size)
    def shutdown(self,*_):
        if self.closed:return
        self.closed=True
        try:self.stream.shutdown(socket.SHUT_RDWR)
        except OSError:pass
        self.stream.close()
    close=shutdown
    def release_owned_socket(self,stream):pass


class RoutePolicyTests(unittest.TestCase):
    def test_cloud_bootstrap_yields_to_new_native_routes_before_measurements_exist(self):
        clock=[10.];policy=RoutePolicy(lambda:clock[0]);routes={'storage':'cloud'}
        self.assertEqual(policy.choose(routes),'storage')
        routes['direct']='p2p';self.assertEqual(policy.choose(routes),'direct')
        routes['local']='lan';self.assertEqual(policy.choose(routes),'local')
        policy.observe('local',100,1);policy.observe('storage',1000,1)
        self.assertEqual(policy.choose(routes),'local')
        clock[0]+=6;policy.observe('storage',1000,1)
        self.assertEqual(policy.choose(routes),'local')
        self.assertEqual(policy.choose({'storage':'cloud'}),'storage')
        self.assertEqual(policy.choose({'storage':'cloud','direct':'p2p'}),'direct')
    def test_real_measurements_override_fixed_preference_with_dwell_and_no_oscillation(self):
        clock=[10.];policy=RoutePolicy(lambda:clock[0]);routes={'local':'lan','direct':'p2p','turn':'relay'}
        self.assertEqual(policy.choose(routes),'local')
        policy.observe('local',100,1);policy.observe('direct',1000,1)
        for _ in range(100):self.assertEqual(policy.choose(routes),'local')
        clock[0]+=6;policy.observe('direct',1000,1)
        self.assertEqual(policy.choose(routes),'direct')
        policy.observe('turn',100000,1);policy.observe('turn',100000,1);clock[0]+=6
        self.assertEqual(policy.choose(routes),'direct')
        self.assertEqual(policy.choose({'turn':'relay'}),'turn')
        policy.observe('turn',1,float('nan'));self.assertTrue(policy.samples['turn'].rate>0)


class AdaptiveTests(unittest.TestCase):
    def setUp(self):
        self.cancel=threading.Event();self.authorized=True;self.streams=[];self.events=[[],[]]
        first,second=socket.socketpair();self.raw=[Stream(first),Stream(second)]
        for index,raw in enumerate(self.raw):
            self.streams.append(AdaptiveStream(raw,raw,initiator=index==0,cancel=self.cancel,
                authorized=lambda:self.authorized,event=self.events[index].append))
        self.workers=[]

    def tearDown(self):
        self.cancel.set()
        for stream in self.streams:stream.shutdown()
        for worker in self.workers:worker.join(timeout=3)

    def add(self,key,route,delay=0.):
        pair=socket.socketpair()
        for stream,raw in zip(self.streams,pair):stream._add(key,route,Stream(raw,delay))

    def transfer(self,body,source=0):
        result=bytearray();error=[]
        sender=self.streams[source];receiver=self.streams[1-source]
        def send():
            try:sender.send_payload(body)
            except OSError as problem:error.append(problem)
        worker=threading.Thread(target=send);worker.start();self.workers.append(worker)
        while len(result)<len(body):
            try:chunk=receiver.recv(min(96*1024,len(body)-len(result)))
            except socket.timeout:continue
            if not chunk:raise AssertionError('Incomplete adaptive payload')
            result.extend(chunk)
        worker.join(timeout=3);self.assertFalse(worker.is_alive());self.assertFalse(error)
        self.assertEqual(hashlib.sha256(result).digest(),hashlib.sha256(body).digest())
        until(lambda:sender.payload_progress()==len(body))
        return bytes(result)

    def test_healthy_direct_path_never_opens_the_cloud_as_a_speed_challenger(self):
        calls=[];self.streams[0].cloud=lambda descriptor:calls.append(descriptor)
        self.transfer(os.urandom(3*1024*1024+29))
        time.sleep(.2)
        self.assertFalse(calls);self.assertEqual(self.streams[0].route,'p2p')

    def test_stalled_direct_path_falls_back_preserves_bytes_and_returns_to_recovered_p2p(self):
        first,second=socket.socketpair();cloud=[Stream(first),Stream(second)];calls=[]
        def fallback(descriptor):
            calls.append(descriptor);self.streams[1]._add('cloud','cloud',cloud[1]);return cloud[0],{'id':'fixture','key':'fixture'}
        self.streams[0].cloud=fallback;self.streams[0].signal_out=lambda value:True
        original=self.raw[0].sendall
        def blocked(data):
            if HEADER.unpack(data[:HEADER.size])[0]!=DATA:original(data)
        with patch.object(self.raw[0],'sendall',side_effect=blocked),patch('adaptive_stream.STALL_SECONDS',.12):
            self.transfer(os.urandom(2*1024*1024+17))
        self.assertEqual(len(calls),1);self.assertEqual(self.streams[0].route,'cloud')
        self.add('recovered','p2p');self.streams[0].set_payload_offset(0)
        self.transfer(os.urandom(2*1024*1024+31))
        self.assertEqual(self.streams[0].route,'p2p');self.assertEqual(len(calls),1)

    def test_original_bytes_and_acknowledged_payload_survive_three_routes_and_path_loss(self):
        self.add('local','lan');self.add('turn','relay')
        self.assertEqual(self.streams[0].route,'lan')
        # Start on LAN, then interrupt it in the middle of a larger original.
        with self.streams[0]._condition:self.streams[0].policy.current='local'
        body=os.urandom(12*1024*1024+23)
        def interrupt():
            until(lambda:self.streams[0]._sequence>5)
            self.streams[0]._paths['local'].stream.shutdown()
        worker=threading.Thread(target=interrupt);worker.start();self.workers.append(worker)
        self.transfer(body)
        self.assertTrue(self.streams[0]._paths['local'].dead)
        self.assertIn(self.streams[0].route,('p2p','relay'))
        self.assertLessEqual(self.streams[0]._pending_bytes,WINDOW_BYTES)

    def test_faster_turn_waits_until_both_direct_paths_are_unavailable(self):
        self.raw[0].delay=.035;self.raw[1].delay=.035
        self.add('local','lan',.08);self.add('turn','relay',.001)
        self.transfer(os.urandom(2*1024*1024))
        until(lambda:self.streams[0].route=='p2p',8)
        self.assertEqual(self.streams[0]._paths['turn'].probes,0)
        for stream in self.streams:
            stream._paths['primary'].stream.shutdown();stream._paths['local'].stream.shutdown()
        until(lambda:self.streams[0].route=='relay')
        self.streams[0].set_payload_offset(0);self.transfer(os.urandom(2*1024*1024+11))
        self.assertEqual({row['route'] for row in self.streams[0].measurements()},{'lan','p2p','relay'})

    def test_even_a_faster_lower_latency_cloud_path_cannot_take_native_payload_or_control(self):
        self.add('storage','cloud')
        with self.streams[0]._condition:
            native=self.streams[0]._paths['primary'];storage=self.streams[0]._paths['storage']
            native.latency=.04;storage.latency=.001
            native.pinged=storage.pinged=time.perf_counter()
            self.streams[0].policy.observe('primary',1024*1024,.1)
            self.streams[0].policy.observe('storage',1024*1024,.001)
        self.transfer(b'lightweight device message')
        self.assertEqual(self.streams[0].sent_route,'p2p')
        self.streams[0].set_payload_offset(0)
        self.transfer(os.urandom(2*1024*1024))
        self.assertEqual(self.streams[0].route,'p2p')
        self.assertEqual(self.streams[0]._paths['storage'].probes,0)
        self.raw[0].shutdown();self.raw[1].shutdown()
        until(lambda:self.streams[0].route=='cloud')
        self.streams[0].set_payload_offset(0)
        self.transfer(os.urandom(2*1024*1024+19))

    def test_duplicate_retries_deliver_each_byte_once_and_bad_ack_cannot_erase_pending(self):
        self.transfer(b'original')
        self.streams[0]._frame(self.streams[0]._paths['primary'],DATA,1,b'original')
        time.sleep(.05)
        with self.assertRaises(socket.timeout):self.streams[1].recv(1)
        self.assertFalse(self.streams[1]._input)
        with self.assertRaises(ValueError):self.streams[0]._ack(100,b'12345678')

    def test_logout_closes_all_paths_and_unblocks_waiting_reader(self):
        self.add('turn','relay');self.authorized=False
        until(lambda:all(stream.closed for stream in self.streams))
        self.assertEqual(self.streams[0].recv(10),b'')
        with self.assertRaises(ConnectionError):self.streams[0].sendall(b'private')

    def test_idle_relay_still_attempts_direct_only_and_keeps_working_path_on_failure(self):
        self.raw[0].route=self.raw[1].route='relay'
        attempts=[];signals=[]
        def alternative(callback,configuration):
            attempts.append(configuration);return None
        self.streams[0].secondary=alternative;self.streams[0].signal_out=lambda value:signals.append(value) or True
        until(lambda:bool(attempts),3)
        self.assertTrue(attempts[0]['directOnly']);self.assertFalse(attempts[0]['forceRelay'])
        self.assertTrue(signals[0]['directOnly']);self.assertFalse(self.streams[0]._paths['primary'].dead)
        self.transfer(b'working relay survives unavailable direct path')


if __name__=='__main__':unittest.main()
