"""One libdatachannel process for the account's authenticated peer streams."""
from __future__ import annotations
import json
import logging
import os
from pathlib import Path
from queue import Empty,Full,Queue
import re
import struct
import subprocess
import sys
import threading

from webrtc_stream import WebRTCStream

MAX_PEERS=38
IPC_CHUNK_BYTES=64*1024
IPC_MAX_PACKET_BYTES=131089
IPC_QUEUE_FRAMES=256
LOG=logging.getLogger(__name__)


class PeerStream(WebRTCStream):
    def __init__(self,broker,identifier,on_signal):
        self.broker=broker;self.identifier=identifier
        self._initialize_buffers(on_signal)

    def _write(self,kind,data):
        if self.closed:raise ConnectionError('同账号设备连接已关闭。')
        if kind==b'J':
            value=json.loads(data);value['id']=self.identifier;self.broker.control(value)
        else:
            for offset in range(0,len(data),IPC_CHUNK_BYTES):
                self.broker.write(b'B',bytes.fromhex(self.identifier)+data[offset:offset+IPC_CHUNK_BYTES],timeout=self._timeout)
            with self._condition:self._sent_position+=len(data)

    def _ended(self):
        with self._condition:
            if self._closed:return
            self._closed=True;self._write_closed=True;self._condition.notify_all()
        self._close_sockets();self._reader_done.set();self.on_signal(dict(type='closed'))

    def shutdown(self,*_):self.close(abort=True)

    def close(self,*,abort=False):
        self.broker.retire(self.identifier);self._ended()


class PeerBroker:
    def __init__(self,*,root=None,node=None):
        folder=Path(root or getattr(sys,'_MEIPASS',Path(__file__).parent))/'p2p'
        executable=Path(node) if node else folder/'runtime'/('node.exe' if sys.platform=='win32' else 'node')
        if not executable.is_file() or not (folder/'sync-mesh.mjs').is_file():
            raise RuntimeError('同账号直连组件缺失，请重新安装。')
        self._lock=threading.RLock();self._streams={};self.closed=False;self._queue=Queue(IPC_QUEUE_FRAMES)
        self._process=subprocess.Popen([str(executable),'--use-system-ca',str(folder/'sync-mesh.mjs')],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=folder,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0,
            env={key:value for key,value in os.environ.items() if key not in ('NODE_OPTIONS','NODE_PATH','NODE_EXTRA_CA_CERTS')})
        threading.Thread(target=self._read,daemon=True,name='FlickDock-peer-reader').start()
        threading.Thread(target=self._write,daemon=True,name='FlickDock-peer-writer').start()
        threading.Thread(target=self._diagnostics,daemon=True,name='FlickDock-peer-diagnostics').start()

    def stream(self,identifier,on_signal):
        if not isinstance(identifier,str) or not re.fullmatch('[a-f0-9]{32}',identifier):raise ValueError('Invalid peer connection identity')
        with self._lock:
            if self.closed:raise ConnectionError('Peer runtime closed')
            if identifier in self._streams or len(self._streams)>=MAX_PEERS:raise ValueError('Peer connection limit exceeded')
            stream=PeerStream(self,identifier,on_signal);self._streams[identifier]=stream;return stream

    def write(self,kind,data,*,timeout=None):
        if self.closed:raise ConnectionError('Peer runtime closed')
        if len(data)+1>IPC_MAX_PACKET_BYTES:raise ValueError('Peer IPC packet exceeds limit')
        packet=struct.pack('!I',len(data)+1)+kind+data
        try:
            if timeout is None:self._queue.put_nowait(packet)
            else:self._queue.put(packet,timeout=max(.05,timeout))
        except Full:raise ConnectionError('同账号直连发送队列已满。') from None

    def control(self,value):self.write(b'J',json.dumps(value,separators=(',',':')).encode('utf8'))

    def retire(self,identifier):
        with self._lock:self._streams.pop(identifier,None)
        if not self.closed:self.control(dict(id=identifier,type='abort'))

    def _write(self):
        try:
            while not self.closed:
                try:packet=self._queue.get(timeout=.2)
                except Empty:continue
                self._process.stdin.write(packet);self._process.stdin.flush()
        except (OSError,ValueError) as error:
            if not self.closed:LOG.warning('Peer native IPC writer ended: %r',error)
            self.close()

    def _exact(self,count):
        result=bytearray()
        while len(result)<count:
            data=self._process.stdout.read(count-len(result))
            if not data:raise EOFError('Peer runtime exited')
            result.extend(data)
        return bytes(result)

    def _read(self):
        try:
            while not self.closed:
                size=struct.unpack('!I',self._exact(4))[0]
                if not 1<=size<=IPC_MAX_PACKET_BYTES:raise ValueError('Invalid peer IPC frame')
                packet=self._exact(size)
                if packet[:1]==b'J':
                    value=json.loads(packet[1:]);identifier=value['id'];event=value['event']
                    if not isinstance(event,dict):raise ValueError('Invalid peer event')
                    data=b'J'+json.dumps(event,separators=(',',':')).encode('utf8')
                elif packet[:1]==b'B' and len(packet)>17:
                    identifier=packet[1:17].hex();data=b'B'+packet[17:]
                else:raise ValueError('Invalid peer IPC type')
                with self._lock:stream=self._streams.get(identifier)
                if stream is not None:
                    if packet[:1]==b'J' and event.get('type')=='closed':
                        with self._lock:self._streams.pop(identifier,None)
                        stream._ended()
                    else:stream._accept_packet(data)
        except (OSError,EOFError,ValueError,KeyError,TypeError) as error:
            if not self.closed:LOG.warning('Peer native IPC ended: %r',error)
            self.close()

    def _diagnostics(self):
        try:
            while line:=self._process.stderr.readline(2048):
                LOG.warning('Peer native runtime: %s',line.decode('utf8',errors='replace').strip())
        except (OSError,ValueError):return

    def close(self):
        with self._lock:
            if self.closed:return
            self.closed=True;streams=list(self._streams.values());self._streams.clear()
        for stream in streams:stream._ended()
        def reap():
            if self._process.poll() is None:self._process.terminate()
            try:self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:self._process.kill();self._process.wait(timeout=3)
            for pipe in (self._process.stdin,self._process.stdout,self._process.stderr):
                try:pipe.close()
                except OSError:pass
        threading.Thread(target=reap,daemon=True,name='FlickDock-peer-reaper').start()
