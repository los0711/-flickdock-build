"""Native loopback coverage of peers that do not advertise flow v2."""
import hashlib
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch
from webrtc_stream import WebRTCStream


class LegacyFlowTests(unittest.TestCase):
    def test_older_peer_without_flow_advertisement_uses_legacy_messages(self):
        node=Path(__file__).parent/'p2p/runtime'/('node.exe' if sys.platform=='win32' else 'node')
        if not node.is_file():self.skipTest('Pinned native WebRTC runtime is unavailable')
        peers=[];closing=False;lock=threading.RLock()
        def signal(index,value):
            with lock:
                if closing or value.get('type') not in ('candidate','description'):return
                if index==1 and value['type']=='description':
                    value=dict(value,sdp=re.sub(r'^a=x-flickdock-flow:[^\r\n]*\r?\n?', '',value['sdp'],flags=re.M))
                peers[1-index].control(value)
        peers=[WebRTCStream(lambda value:signal(0,value)),WebRTCStream(lambda value:signal(1,value))]
        try:
            peers[1].control(dict(type='init',iceServers=[],initiator=False))
            peers[0].control(dict(type='init',iceServers=[],initiator=True))
            self.assertTrue(all(peer.opened.wait(12) for peer in peers))
            payload=os.urandom(512*1024);peers[0].sendall(payload)
            received=bytearray();largest=0;deadline=time.monotonic()+12
            while len(received)<len(payload) and time.monotonic()<deadline:
                try:chunk=peers[1].recv(65536)
                except socket.timeout:continue
                if not chunk:break
                largest=max(largest,len(chunk));received.extend(chunk)
            self.assertEqual(hashlib.sha256(received).digest(),hashlib.sha256(payload).digest())
            self.assertLessEqual(largest,32768)
        finally:
            closing=True
            for peer in peers:peer.close(abort=True)

    def test_opt_in_bridge_peak_stats_preserve_ipc_and_payload(self):
        node=Path(__file__).parent/'p2p/runtime'/('node.exe' if sys.platform=='win32' else 'node')
        if not node.is_file():self.skipTest('Pinned native WebRTC runtime is unavailable')
        peers=[];samples=[];closing=False;lock=threading.RLock()
        def signal(index,value):
            with lock:
                if value.get('type')=='stats' and index==0:samples.append(value)
                if not closing and value.get('type') in ('candidate','description'):
                    peers[1-index].control(value)
        with patch.dict(os.environ,{'FLICKDOCK_TRANSFER_DIAG':'1'}):
            peers=[WebRTCStream(lambda value:signal(0,value)),WebRTCStream(lambda value:signal(1,value))]
        try:
            peers[1].control(dict(type='init',iceServers=[],initiator=False))
            peers[0].control(dict(type='init',iceServers=[],initiator=True))
            self.assertTrue(all(peer.opened.wait(12) for peer in peers))
            payload=os.urandom(512*1024);peers[0].sendall(payload)
            received=bytearray();deadline=time.monotonic()+12
            while len(received)<len(payload) and time.monotonic()<deadline:
                try:chunk=peers[1].recv(65536)
                except socket.timeout:continue
                if not chunk:break
                received.extend(chunk)
            self.assertEqual(hashlib.sha256(received).digest(),hashlib.sha256(payload).digest())
            peers[0].control({'type':'stats'})
            while time.monotonic()<deadline and not any(row.get('unackedMax',0)>0 for row in samples):time.sleep(.01)
            peak=next(row for row in samples if row.get('unackedMax',0)>0)
            self.assertGreaterEqual(peak['unackedMax'],peak['unacked'])
            self.assertGreaterEqual(peak['bufferedMax'],0)
        finally:
            closing=True
            for peer in peers:peer.close(abort=True)


if __name__=='__main__':unittest.main()
