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
        self.assertEqual(policy.choose(routes),'storage')
    def test_real_measurements_override_fixed_preference_with_dwell_and_no_oscillation(self):
        clock=[10.];policy=RoutePolicy(lambda:clock[0]);routes={'local':'lan','direct':'p2p','turn':'relay'}
        self.assertEqual(policy.choose(routes),'local')
        policy.observe('local',100,1);policy.observe('direct',1000,1)
        for _ in range(100):self.assertEqual(policy.choose(routes),'local')
        clock[0]+=6;policy.observe('direct',1000,1)
        self.assertEqual(policy.choose(routes),'direct')
        policy.observe('turn',1050,1);policy.observe('turn',1050,1);clock[0]+=6
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

    def test_automatic_probe_selects_faster_turn_over_slow_direct_and_records_all_three(self):
        self.raw[0].delay=.035;self.raw[1].delay=.035
        self.add('local','lan',.08);self.add('turn','relay',.001)
        self.transfer(os.urandom(2*1024*1024))
        # Keep an active transfer window while the bounded probes settle.
        deadline=time.monotonic()+8
        while time.monotonic()<deadline and self.streams[0].route!='relay':
            self.streams[0]._last_send=time.monotonic();time.sleep(.05)
        self.assertEqual(self.streams[0].route,'relay',(self.streams[0].measurements(),self.streams[0]._probe,[(p.key,p.probes,p.probed) for p in self.streams[0]._paths.values()]))
        self.assertEqual({row['route'] for row in self.streams[0].measurements()},{'lan','p2p','relay'})
        self.assertTrue(all(row['bytes_per_second'] is not None for row in self.streams[0].measurements()))

    def test_cloud_is_selected_when_it_is_the_fastest_of_four_real_socket_paths(self):
        self.raw[0].delay=.05;self.raw[1].delay=.05
        self.add('local','lan',.08);self.add('turn','relay',.06);self.add('storage','cloud',.001)
        self.transfer(os.urandom(2*1024*1024))
        deadline=time.monotonic()+10
        while time.monotonic()<deadline and self.streams[0].route!='cloud':
            self.streams[0]._last_send=time.perf_counter();time.sleep(.05)
        self.assertEqual(self.streams[0].route,'cloud',self.streams[0].measurements())
        self.assertEqual({row['route'] for row in self.streams[0].measurements()},{'lan','p2p','relay','cloud'})

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


if __name__=='__main__':unittest.main()
