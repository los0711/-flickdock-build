"""Bounded full-duplex requests over an authenticated account byte stream."""
from __future__ import annotations
from dataclasses import dataclass
import heapq
import json
import logging
from queue import Empty,Full,PriorityQueue,Queue
import re
import socket
import struct
import threading
import time
from uuid import uuid4
from transfer_priority import bulk_delay,clipboard_priority_active

RPC_HEADER_BYTES=65536
RPC_BLOCK_BYTES=1024*1024
RPC_REQUESTS_PER_PEER=8
RPC_QUEUED_BYTES=8*1024*1024
RPC_QUEUED_MESSAGES=256
RPC_TIMEOUT_SECONDS=30.
LOG=logging.getLogger(__name__)


class PeerUnavailable(ConnectionError):pass


@dataclass(frozen=True)
class PendingRequest:
    identifier:str
    reply:Queue
    count:int
    deadline:float
    topic:str


class PeerRpc:
    def __init__(self,stream,provider,notify,submit,authorized):
        self.stream=stream;self.provider=provider;self.notify=notify;self.submit=submit;self.authorized=authorized
        self.closed=threading.Event();self._lock=threading.Condition();self._pending={};self._serving=0
        self.error=''
        self._outgoing=PriorityQueue(RPC_QUEUED_MESSAGES);self._queued_bytes=0;self._sequence=0
        self._requests=[];self._request_sequence=0;self._reserved_bytes=0
        stream.settimeout(.25)
        threading.Thread(target=self._reader,daemon=True,name='FlickDock-peer-rpc-reader').start()
        threading.Thread(target=self._writer,daemon=True,name='FlickDock-peer-rpc-writer').start()

    def _alive(self):return not self.closed.is_set() and not self.stream.closed and self.authorized()

    def _enqueue(self,header,body=b'',*,priority=1,wait=False,reservation=0):
        if not self._alive():raise PeerUnavailable('同账号连接已关闭或授权已失效。')
        encoded=json.dumps(dict(header,data_size=len(body)),separators=(',',':'),ensure_ascii=False).encode('utf8')
        if len(encoded)>RPC_HEADER_BYTES or len(body)>RPC_BLOCK_BYTES:raise ValueError('Peer RPC packet exceeds limit')
        packet=struct.pack('!I',len(encoded))+encoded+body;deadline=time.monotonic()+RPC_TIMEOUT_SECONDS
        with self._lock:
            # A clipboard response has its own bounded reserve even when
            # several shelf blocks have filled the ordinary output budget.
            limit=RPC_QUEUED_BYTES+(RPC_BLOCK_BYTES if priority==0 else RPC_HEADER_BYTES if not body else 0)
            while self._queued_bytes+self._reserved_bytes-reservation+len(packet)>limit:
                if not wait:return False
                if not self._alive():raise PeerUnavailable('同账号连接已关闭。')
                remaining=deadline-time.monotonic()
                if remaining<=0:raise PeerUnavailable('同账号连接发送队列超时。')
                self._lock.wait(min(.1,remaining))
            self._sequence+=1
            try:self._outgoing.put_nowait((priority,self._sequence,packet))
            except Full:return False
            self._queued_bytes+=len(packet);self._lock.notify_all();return True

    def send_notify(self,value,*,priority=1):
        return self._enqueue(dict(type='notify',value=value),priority=priority)

    def begin_request(self,topic,digest,offset,count,cancel,*,wait=False):
        identifier=uuid4().hex;reply=Queue(1);deadline=time.monotonic()+RPC_TIMEOUT_SECONDS
        limit=RPC_REQUESTS_PER_PEER-(topic=='shelf')
        with self._lock:
            while len(self._pending)>=limit:
                if not wait:return None
                if cancel.is_set():raise InterruptedError('已取消设备同步。')
                if not self._alive():raise PeerUnavailable('设备连接已中断。')
                if time.monotonic()>=deadline:raise PeerUnavailable('设备请求队列超时。')
                self._lock.wait(.05)
            self._pending[identifier]=reply
        try:
            queued=self._enqueue(dict(type='request',id=identifier,topic=topic,sha256=digest,offset=offset,count=count),
                                 priority=0 if topic=='clipboard' else 1,wait=True)
            if not queued:raise PeerUnavailable('设备请求队列已满。')
        except (OSError,ValueError):
            with self._lock:self._pending.pop(identifier,None);self._lock.notify_all()
            raise
        return PendingRequest(identifier,reply,count,deadline,topic)

    def discard_request(self,request):
        with self._lock:self._pending.pop(request.identifier,None);self._lock.notify_all()

    def receive_request(self,request,cancel):
        deadline=request.deadline;last=time.monotonic()
        try:
            while True:
                now=time.monotonic()
                if request.topic!='clipboard' and clipboard_priority_active():deadline+=now-last
                last=now
                if cancel.is_set():raise InterruptedError('已取消设备同步。')
                if not self._alive():raise PeerUnavailable('设备连接已中断。')
                remaining=deadline-time.monotonic()
                if remaining<=0:raise PeerUnavailable('设备数据请求超时。')
                try:header,body=request.reply.get(timeout=min(.05,remaining));break
                except Empty:continue
            if not header.get('ok'):raise PeerUnavailable(str(header.get('error','设备数据暂不可用。'))[:160])
            if len(body)!=request.count:raise PeerUnavailable('设备返回的数据长度无效。')
            return body
        finally:self.discard_request(request)

    def call(self,topic,digest,offset,count,cancel):
        return self.receive_request(self.begin_request(topic,digest,offset,count,cancel,wait=True),cancel)

    def _exact(self,count):
        result=bytearray()
        while len(result)<count:
            if not self._alive():raise PeerUnavailable('设备连接已中断。')
            try:chunk=self.stream.recv(count-len(result))
            except socket.timeout:continue
            if not chunk:raise EOFError('设备连接已关闭。')
            result.extend(chunk)
        return bytes(result)

    def _reader(self):
        try:
            while self._alive():
                size=struct.unpack('!I',self._exact(4))[0]
                if not 1<=size<=RPC_HEADER_BYTES:raise ValueError('Invalid peer RPC header size')
                header=json.loads(self._exact(size))
                if not isinstance(header,dict):raise ValueError('Invalid peer RPC header')
                count=header.get('data_size')
                if type(count) is not int or not 0<=count<=RPC_BLOCK_BYTES:raise ValueError('Invalid peer RPC payload size')
                body=self._exact(count)
                kind=header.get('type');identifier=header.get('id')
                if kind=='response':
                    with self._lock:pending=self._pending.get(identifier)
                    if pending is not None:
                        try:pending.put_nowait((header,body))
                        except Full:pass
                elif kind=='request':
                    if (body or not isinstance(identifier,str) or not re.fullmatch('[a-f0-9]{32}',identifier)
                            or header.get('topic') not in ('clipboard','shelf')
                            or not re.fullmatch('[a-f0-9]{64}',str(header.get('sha256','')))
                            or type(header.get('offset')) is not int or header['offset']<0
                            or type(header.get('count')) is not int or not 0<=header['count']<=RPC_BLOCK_BYTES):
                        raise ValueError('Invalid peer object request')
                    with self._lock:
                        if self._serving>=RPC_REQUESTS_PER_PEER:raise ValueError('Peer request limit exceeded')
                        self._serving+=1
                        self._request_sequence+=1
                        heapq.heappush(self._requests,(0 if header['topic']=='clipboard' else 2,self._request_sequence,header))
                    self._dispatch_requests()
                elif kind=='notify' and not body:
                    value=header.get('value')
                    if not isinstance(value,dict):raise ValueError('Invalid peer notification')
                    self.notify(value)
                else:raise ValueError('Invalid peer RPC message')
        except (OSError,EOFError,ValueError,KeyError,TypeError,RecursionError) as error:self.close('reader: '+repr(error))

    def _dispatch_requests(self):
        # Reserve response memory before entering the shared disk pool. A slow
        # network must never park every provider worker waiting for queue space.
        with self._lock:
            while self._requests and self._alive():
                priority,sequence,header=self._requests[0]
                capacity=header['count']+RPC_HEADER_BYTES+4
                limit=RPC_QUEUED_BYTES+(RPC_BLOCK_BYTES if priority==0 else 0)
                if self._queued_bytes+self._reserved_bytes+capacity>limit:break
                heapq.heappop(self._requests);self._reserved_bytes+=capacity
                if not self.submit(lambda header=header,capacity=capacity:self._serve(header,capacity)):
                    self._reserved_bytes-=capacity;heapq.heappush(self._requests,(priority,sequence,header));break

    def _serve(self,header,capacity):
        try:
            if not self._alive():return
            try:
                body=self.provider(header['topic'],header['sha256'],header['offset'],header['count'])
                response=dict(type='response',id=header['id'],ok=True)
            except (OSError,ValueError) as error:
                LOG.debug('Peer body is unavailable',exc_info=True)
                body=b'';response=dict(type='response',id=header['id'],ok=False,error='设备内容已更新或暂不可用。')
            if not self._alive():return
            if not self._enqueue(response,body,priority=0 if header['topic']=='clipboard' else 2,reservation=capacity):
                raise PeerUnavailable('设备响应队列已满。')
        except (OSError,ValueError) as error:self.close('provider: '+repr(error))
        finally:
            with self._lock:
                self._serving-=1;self._reserved_bytes-=capacity
                self._lock.notify_all()
            self._dispatch_requests()

    def _writer(self):
        last_bulk=time.monotonic()
        try:
            while self._alive():
                self._dispatch_requests()
                with self._lock:
                    with self._outgoing.mutex:
                        priority=self._outgoing.queue[0][0] if self._outgoing.queue else None
                    if priority is None or priority>=2 and bulk_delay(last_bulk)>0:
                        self._lock.wait(.025);continue
                    _,_,packet=self._outgoing.get_nowait()
                    self._queued_bytes-=len(packet);self._lock.notify_all()
                self.stream.sendall(packet)
                if priority>=2:last_bulk=time.monotonic()
        except (OSError,ValueError) as error:self.close('writer: '+repr(error))

    def close(self,error=''):
        if self.closed.is_set():return
        self.error=error
        if error:LOG.warning('Account peer connection closed: %s',error)
        self.closed.set()
        with self._lock:self._lock.notify_all()
        self.stream.shutdown()
