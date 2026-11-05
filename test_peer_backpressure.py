"""A slow authenticated peer must not consume every provider worker."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import socket
import threading
import time
import unittest
from unittest.mock import patch

from peer_rpc import PeerRpc, RPC_BLOCK_BYTES


class SocketStream:
    def __init__(self, sock, gate=None):
        self.sock=sock;self.gate=gate;self.entered=threading.Event();self.closed=False
    def settimeout(self, value):self.sock.settimeout(value)
    def recv(self, count):return self.sock.recv(count)
    def sendall(self, body):
        self.entered.set()
        while self.gate is not None and not self.gate.wait(.02):
            if self.closed:raise ConnectionError('Closed test stream')
        self.sock.sendall(body)
    def shutdown(self):
        self.closed=True
        try:self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:pass
        self.sock.close()


class PeerBackpressureTests(unittest.TestCase):
    def test_stalled_output_keeps_rpc_and_other_peer_clipboard_alive(self):
        gate=threading.Event();rpcs=[];streams=[];cancel=threading.Event()
        body=b'f'*RPC_BLOCK_BYTES;digest=hashlib.sha256(body).hexdigest()
        with ThreadPoolExecutor(max_workers=2) as pool,patch('peer_rpc.RPC_QUEUED_BYTES',4*RPC_BLOCK_BYTES):
            def submit(action):pool.submit(action);return True
            def pair(blocked=False):
                first,second=socket.socketpair()
                client_stream=SocketStream(first);server_stream=SocketStream(second,gate if blocked else None)
                streams.extend((client_stream,server_stream))
                provider=lambda topic,digest,offset,count:body[:count]
                client=PeerRpc(client_stream,provider,lambda _:None,submit,lambda:True)
                server=PeerRpc(server_stream,provider,lambda _:None,submit,lambda:True)
                rpcs.extend((client,server));return client,server
            slow,server=pair(True);fast,_=pair()
            try:
                requests=[slow.begin_request('shelf',digest,0,len(body),cancel,wait=True) for _ in range(7)]
                self.assertTrue(server.stream.entered.wait(1))
                time.sleep(1.25)
                self.assertFalse(server.closed.is_set(),server.error)
                started=time.monotonic()
                self.assertEqual(fast.call('clipboard',digest,0,31,cancel),body[:31])
                self.assertLess(time.monotonic()-started,.75)
                gate.set()
                for request in requests:
                    received=slow.receive_request(request,cancel)
                    self.assertEqual(hashlib.sha256(received).hexdigest(),digest)
                self.assertFalse(server.closed.is_set(),server.error)
            finally:
                gate.set();cancel.set()
                for rpc in rpcs:rpc.close()


if __name__=='__main__':unittest.main()
