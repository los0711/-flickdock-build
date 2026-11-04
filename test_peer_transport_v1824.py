"""Actual multi-peer native connections with bounded shared subprocesses."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import hashlib
import threading
import unittest
from peer_stream import PeerBroker


class PeerTransportTests(unittest.TestCase):
    def test_three_devices_connect_all_pairs_and_close_one_link_independently(self):
        brokers=[PeerBroker() for _ in range(3)];streams={};closing=threading.Event()
        pairs=((0,1,'a'*32),(0,2,'b'*32),(1,2,'c'*32))
        def event(device,other,identifier,value):
            if not closing.is_set() and value.get('type') in ('description','candidate'):
                try:streams[other,device].control(value)
                except OSError:
                    if not closing.is_set():raise
        try:
            for first,second,identifier in pairs:
                for device,other in ((first,second),(second,first)):
                    streams[device,other]=brokers[device].stream(identifier,
                        lambda value,device=device,other=other,identifier=identifier:event(device,other,identifier,value))
            for first,second,_ in pairs:
                streams[second,first].control(dict(type='init',iceServers=[],initiator=False))
                streams[first,second].control(dict(type='init',iceServers=[],initiator=True))
            self.assertTrue(all(stream.opened.wait(12) for stream in streams.values()))
            self.assertTrue(all(stream.adaptive_capable for stream in streams.values()))
            self.assertEqual(len({broker._process.pid for broker in brokers}),3)
            payload=os.urandom(2*1024*1024);received={};threads=[]
            def read(device,other):
                body=bytearray();stream=streams[device,other];stream.settimeout(5)
                while len(body)<len(payload):body.extend(stream.recv(min(65536,len(payload)-len(body))))
                received[device]=hashlib.sha256(body).hexdigest()
            for other in (1,2):
                thread=threading.Thread(target=read,args=(other,0));thread.start();threads.append(thread)
            for other in (1,2):streams[0,other].sendall(payload)
            for thread in threads:thread.join(12);self.assertFalse(thread.is_alive())
            self.assertEqual(received,{1:hashlib.sha256(payload).hexdigest(),2:hashlib.sha256(payload).hexdigest()})
            streams[0,1].close(abort=True)
            streams[0,2].sendall(b'other connection retained')
            self.assertEqual(streams[2,0].recv(100),b'other connection retained')
        finally:
            closing.set()
            for broker in brokers:broker.close()


if __name__=='__main__':unittest.main(verbosity=2)
