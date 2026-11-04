"""Ephemeral AES-GCM blocks through the existing authenticated CloudApi/R2."""
from collections import deque
from dataclasses import dataclass,field
import base64
import hashlib
import logging
import os
import re
import socket
import struct
import threading
import time
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
from PySide6.QtCore import QObject,Signal,Qt
from cloud_api import CloudApi,CloudError

CHUNK_BYTES=1024*1024
QUEUE_BYTES=4*CHUNK_BYTES
CALL_TIMEOUT_SECONDS=18.
LOG=logging.getLogger(__name__)


@dataclass
class Call:
    method:str
    path:str
    identity:tuple
    options:dict
    done:object=field(default_factory=threading.Event)
    key:object=None
    result:object=None
    error:object=None


class CloudPipeApi(QObject):
    requested=Signal(object)
    cancelled=Signal(object)
    def __init__(self,account,credential,parent=None,*,network_api=None):
        super().__init__(parent);self.account=account;self.credential=credential;self.closed=False
        self.api=network_api
        self._calls={};self._lock=threading.Lock()
        self.requested.connect(self._request,Qt.ConnectionType.QueuedConnection)
        self.cancelled.connect(self._cancel,Qt.ConnectionType.QueuedConnection)

    def identity(self):
        return (self.account.account['id'],self.account.device_id,self.account.api.token) if self.account.signed_in else None

    def call(self,method,path,cancel,**options):
        identity=self.identity()
        if self.closed or identity is None:raise ConnectionError('云端传输账号已退出。')
        job=Call(method,path,identity,options)
        with self._lock:self._calls[id(job)]=job
        self.requested.emit(job);deadline=time.perf_counter()+CALL_TIMEOUT_SECONDS
        while not job.done.wait(.05):
            if cancel.is_set() or self.closed or self.identity()!=identity or time.perf_counter()>deadline:
                self.cancelled.emit(job);raise ConnectionError('云端传输已取消或超时。')
        if job.error is not None:raise job.error
        return job.result

    def _request(self,job):
        with self._lock:active=id(job) in self._calls
        if not active:return
        if self.closed or self.identity()!=job.identity:
            self._complete(job,None,ConnectionError('云端传输授权已改变。'));return
        if self.api is None:
            self.api=CloudApi(self.account.api.origin,self,allow_test_http=self.account.api._allow_test_http)
        self.api.token=job.identity[2]
        def done(result,error):self._complete(job,result,error)
        job.key=self.api.request(job.method,job.path,done,timeout_ms=15000,**job.options)

    def _complete(self,job,result,error):
        with self._lock:active=self._calls.pop(id(job),None)
        if active is None:return
        if self.identity()!=job.identity:error=ConnectionError('云端传输授权已改变。')
        job.result=result;job.error=error;job.done.set()

    def _cancel(self,job):
        with self._lock:active=self._calls.pop(id(job),None)
        if active is None:return
        if job.key is not None:self.api.cancel(job.key)
        job.error=ConnectionError('云端传输已关闭。');job.done.set()

    def create(self,peer,cancel):
        identifier=uuid4().hex;key=os.urandom(32)
        self.call('POST','/v1/wan/cloud/'+identifier,cancel,data=dict(peer=peer),headers={'X-Wan-Credential':self.credential})
        return dict(id=identifier,key=key.hex())

    def open(self,peer,descriptor,cancel,initiator):
        if self.closed or self.identity() is None:return None
        try:
            descriptor=self.create(peer,cancel) if descriptor is None else descriptor
            return CloudPipeStream(self,descriptor,initiator),descriptor
        except CloudError as error:raise ConnectionError('云端线路暂不可用：'+str(error.code or error.status)) from error

    def close(self):
        self.closed=True
        with self._lock:jobs=list(self._calls.values())
        for job in jobs:self._cancel(job)
        if self.api is not None:self.api.close()


class CloudPipeStream:
    route='cloud'
    def __init__(self,api,descriptor,initiator):
        self.api=api;self.identifier=descriptor['id'];self.key=bytes.fromhex(descriptor['key'])
        if len(self.key)!=32 or not re.fullmatch('[a-f0-9]{32}',self.identifier):raise ValueError('Invalid cloud transport descriptor')
        self._cipher=AESGCM(self.key);self.side=0 if initiator else 1;self.closed=False;self.error=''
        self._cancel=threading.Event();self._condition=threading.Condition();self._send_lock=threading.Lock()
        self._out=deque();self._out_bytes=0;self._input=deque();self._input_bytes=0;self._timeout=1.
        for work,name in ((self._writer,'write'),(self._reader,'read')):
            threading.Thread(target=work,daemon=True,name='FlickDock-cloud-route-'+name).start()

    def settimeout(self,value):self._timeout=value

    def _nonce(self,side,sequence):return struct.pack('!IQ',side,sequence)

    def _aad(self,side,sequence):return self.identifier.encode('ascii')+self._nonce(side,sequence)

    def sendall(self,data):
        with self._send_lock:
            view=memoryview(data)
            for offset in range(0,len(view),CHUNK_BYTES):
                chunk=bytes(view[offset:offset+CHUNK_BYTES]);deadline=time.perf_counter()+CALL_TIMEOUT_SECONDS
                with self._condition:
                    while self._out_bytes+len(chunk)>QUEUE_BYTES:
                        if self.closed or time.perf_counter()>deadline:raise ConnectionError('云端传输写入超时。')
                        self._condition.wait(.1)
                    if self.closed:raise ConnectionError('云端传输已关闭。')
                    self._out.append(chunk);self._out_bytes+=len(chunk);self._condition.notify_all()

    def recv(self,size):
        with self._condition:
            deadline=time.perf_counter()+self._timeout
            while not self._input:
                if self.closed:return b''
                remaining=deadline-time.perf_counter()
                if remaining<=0:raise socket.timeout()
                self._condition.wait(remaining)
            data=self._input.popleft();count=min(size,len(data))
            if count<len(data):self._input.appendleft(data[count:])
            self._input_bytes-=count;self._condition.notify_all();return data[:count]

    def _request(self,method,side,sequence,**options):
        deadline=time.perf_counter()+45.;attempts=0
        while not self.closed:
            try:return self.api.call(method,f'/v1/wan/cloud/{self.identifier}/{side}/{sequence}',self._cancel,**options)
            except CloudError as error:
                if error.code=='CLOUD_WINDOW' and time.perf_counter()<deadline:self._cancel.wait(.1);continue
                attempts+=1
                if error.status in (400,401,403,409,410,413,429) or attempts>=3:raise
                self._cancel.wait(.25*attempts)
        raise ConnectionError('云端传输已关闭。')

    def _writer(self):
        sequence=0
        try:
            while not self.closed:
                with self._condition:
                    while not self._out and not self.closed:self._condition.wait(.2)
                    if self.closed:return
                    # Batch adjacent records into one existing cloud request.
                    self._condition.wait(.02);chunks=[];size=0
                    while self._out and size<CHUNK_BYTES:
                        chunk=self._out.popleft();count=min(CHUNK_BYTES-size,len(chunk));chunks.append(chunk[:count]);size+=count
                        if count<len(chunk):self._out.appendleft(chunk[count:])
                    self._out_bytes-=size;self._condition.notify_all()
                plain=b''.join(chunks);encrypted=self._cipher.encrypt(self._nonce(self.side,sequence),plain,self._aad(self.side,sequence))
                self._request('PUT',self.side,sequence,raw=encrypted,headers={'X-Content-SHA256':hashlib.sha256(encrypted).hexdigest()})
                sequence+=1
        except (OSError,CloudError,ValueError,InvalidTag) as error:
            self.error='writer '+type(error).__name__+' '+str(getattr(error,'status',''))+' '+getattr(error,'code','')
            LOG.info('Cloud transport writer ended: %s %s',type(error).__name__,getattr(error,'code',''));self.shutdown()

    def _reader(self):
        side=1-self.side;sequence=0
        try:
            while not self.closed:
                result=self._request('GET',side,sequence)
                if result.get('ready') is not True:continue
                encoded=result.get('body')
                if not isinstance(encoded,str) or len(encoded)>((CHUNK_BYTES+18)//3)*4:raise ValueError('Cloud transport block too large')
                encrypted=base64.b64decode(encoded,validate=True)
                if not 16<=len(encrypted)<=CHUNK_BYTES+16:raise ValueError('Invalid cloud transport block')
                plain=self._cipher.decrypt(self._nonce(side,sequence),encrypted,self._aad(side,sequence))
                with self._condition:
                    while self._input_bytes+len(plain)>QUEUE_BYTES and not self.closed:self._condition.wait(.1)
                    if self.closed:return
                    self._input.append(plain);self._input_bytes+=len(plain);self._condition.notify_all()
                self._request('DELETE',side,sequence);sequence+=1
        except (OSError,CloudError,ValueError,InvalidTag) as error:
            self.error='reader '+type(error).__name__+' '+str(getattr(error,'status',''))+' '+getattr(error,'code','')
            LOG.info('Cloud transport reader ended: %s %s',type(error).__name__,getattr(error,'code',''));self.shutdown()

    def shutdown(self,*_):
        with self._condition:
            if self.closed:return
            self.closed=True;self._cancel.set();self._out.clear();self._out_bytes=0;self._condition.notify_all()
        def remove():
            try:self.api.call('DELETE','/v1/wan/cloud/'+self.identifier,threading.Event())
            except (OSError,CloudError):pass
        threading.Thread(target=remove,daemon=True,name='FlickDock-cloud-route-close').start()

    close=shutdown
