"""LAN discovery and consent over TLS; every file byte uses original Syncthing."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import socket
import threading
import time

from lan_transfer import (LanTransferService,_Transfer,PROTOCOL,_hex,_local_address,
                          _read_json,_send_json,_validate_manifest,_authentication_code)
from manual_payload import prepare,descriptor
from selection_archive import _is_link
from syncthing_engine import SyncthingEngine,check_device
from syncthing_jobs import ReceiveJob,deliver

TRANSPORT='syncthing-v2'
CONSENT_TIMEOUT_SECONDS=180
CONTROL_IDLE_TIMEOUT_SECONDS=150
CONTROL_RETRIES=3
PROGRESS_SECONDS=.1
SPACE_MARGIN_BYTES=1024*1024
CLEANUP_RETRY_SECONDS=30
SHUTDOWN_WORKER_GRACE_SECONDS=3


def native_offer(value):
    if (not isinstance(value,dict) or set(value)!=set(('transport','device_id','port','content'))
            or value.get('transport')!=TRANSPORT or type(value.get('port')) is not int
            or not 0<value['port']<65536):raise ValueError('局域网传输内核信息无效。')
    return dict(transport=TRANSPORT,device_id=check_device(value['device_id']),port=value['port'],content=descriptor(value['content']))


def address(host,port):
    if not _local_address(host):raise ValueError('局域网互传只能连接本地网络设备。')
    return f'tcp://{host}:{port}'


class SyncthingLanService(LanTransferService):
    def __init__(self,base,parent=None,*,binary=None,**options):
        super().__init__(parent,**options)
        self.base=Path(base);self.base.mkdir(parents=True,exist_ok=True)
        self.engine=SyncthingEngine(self.base/'kernel',mode='lan',binary=binary,loopback=self.bind_host=='127.0.0.1')
        self._payloads={};self._native_specs={};self._shutdown=None
        self._owned_stages={};self._cleanup_due={}

    def _stage(self,transfer,parent,name):
        parent=Path(parent).resolve(strict=True);path=parent/name
        path.mkdir(mode=0o700)
        with self._lock:
            transfer.stage=path;self._owned_stages[transfer.transfer_id]=(path,parent,name)
        return path

    def _cleanup(self,transfer):
        if transfer.stage is None:return True
        with self._lock:owned=self._owned_stages.get(transfer.transfer_id)
        if owned is None:return False
        path,parent,name=owned
        try:
            if (Path(transfer.stage)!=path or path.name!=name or _is_link(path) or _is_link(parent)
                    or path.resolve().parent!=parent.resolve()):raise ValueError('传输临时目录的所有权发生变化。')
            if path.exists():shutil.rmtree(path)
        except (OSError,ValueError) as error:
            with self._lock:self._cleanup_due[transfer.transfer_id]=time.monotonic()+CLEANUP_RETRY_SECONDS
            self.startup_diagnostic=dict(phase='cleanup',error=type(error).__name__,errno=getattr(error,'errno',None))
            self.status_changed.emit('传输临时文件被占用，将自动重试清理。' if isinstance(error,OSError) else str(error))
            return False
        with self._lock:
            transfer.stage=None;self._owned_stages.pop(transfer.transfer_id);self._cleanup_due.pop(transfer.transfer_id,None)
        return True

    def _start(self):
        try:
            self.engine.start()
            if not self._stopped.is_set():super()._start()
        except Exception as error:
            self._starting=False
            if not self._stopped.is_set():self.status_changed.emit(str(error))

    def accept(self,identifier,destination):
        transfer=self._transfers.get(identifier);spec=self._native_specs.get(identifier)
        if transfer is None or spec is None:return False
        target=Path(destination).expanduser().resolve(strict=True)
        if shutil.disk_usage(target).free<transfer.total+spec['content']['size']+SPACE_MARGIN_BYTES:
            raise ValueError('接收位置需要同时保存下载临时文件和校验后的文件，可用空间不足。')
        return super().accept(identifier,target)

    def _outgoing(self,transfer,peer,paths):
        shared=False
        try:
            self._notify(transfer,'preparing',route='lan')
            stage=self._stage(transfer,self.base,'send-'+transfer.transfer_id)
            payload=prepare(paths,stage/'payload',transfer.cancel);self._payloads[transfer.transfer_id]=payload
            transfer.total=payload['total'];transfer.token=secrets.token_hex(32)
            own=dict(transport=TRANSPORT,device_id=self.engine.device_id,port=self.engine.port,content=payload['descriptor'])
            offer=dict(protocol=PROTOCOL,id=transfer.transfer_id,token=transfer.token,sender=self.device_name,
                       entries=payload['entries'],native=own)
            if self.account_authorizer:self.account_authorizer.prepare(transfer,peer,payload['entries'])
            task='fd-manual-'+transfer.transfer_id
            for attempt in range(CONTROL_RETRIES):
                stream=None
                try:
                    if transfer.cancel.is_set() or self._stopped.is_set():raise InterruptedError('已取消传输。')
                    stream=self._connect_peer(peer,transfer,payload['entries']);transfer.socket=stream
                    stream.settimeout(1);code=_authentication_code(stream,transfer.transfer_id)
                    _send_json(stream,offer);self._notify(transfer,'waiting',code=code,route='lan')
                    reply=_read_json(stream,transfer.cancel,timeout=CONSENT_TIMEOUT_SECONDS+15)
                    if reply.get('state')!='ready':raise ValueError('接收方未接受这项传输。')
                    remote=native_offer(reply.get('native'))
                    if remote['content']!=payload['descriptor']:raise ValueError('接收方确认了不同的文件内容。')
                    self.engine.share(task,payload['folder'],remote['device_id'],[address(peer['host'],remote['port'])],send=True);shared=True
                    while True:
                        reply=_read_json(stream,transfer.cancel,timeout=CONTROL_IDLE_TIMEOUT_SECONDS)
                        if reply.get('state')=='progress':
                            received=reply.get('received')
                            if type(received) is not int or not 0<=received<=transfer.total:raise ValueError('接收进度无效。')
                            transfer.done=max(transfer.done,received);self._notify(transfer,'transferring',route='lan',code=code)
                        elif reply.get('state')=='complete':
                            transfer.done=transfer.total;transfer.finished=True
                            self._notify(transfer,'complete',route='lan',code=code)
                            self.completed.emit(dict(id=transfer.transfer_id,direction='send',peer=transfer.peer,paths=[str(path) for path in paths],total=transfer.total))
                            return
                        else:raise ValueError('接收方未能完成文件保存。')
                except (ConnectionError,TimeoutError,OSError):
                    if transfer.cancel.is_set() or self._stopped.is_set() or attempt+1==CONTROL_RETRIES:raise
                    self._notify(transfer,'reconnecting',route='lan');transfer.cancel.wait(.5*(attempt+1))
                finally:
                    if stream is not None:stream.close()
                    transfer.socket=None
        except Exception as error:
            self._notify(transfer,'cancelled' if transfer.cancel.is_set() else 'error',message=str(error),route='lan')
            if transfer.token and not transfer.cancel.is_set():self._cancel_remote(transfer)
        finally:
            try:
                if shared:self.engine.unshare('fd-manual-'+transfer.transfer_id)
            finally:
                self._payloads.pop(transfer.transfer_id,None)
                self._cleanup(transfer)
                transfer.active=False;transfer.updated=time.monotonic()

    def _incoming(self,raw,host):
        stream=None;transfer=None;acquired=False;job=None;terminal=None
        try:
            stream=self._incoming_stream(raw);stream.settimeout(1)
            request=_read_json(stream,self._stopped,timeout=10);identifier=request.get('id');token=request.get('token')
            if request.get('protocol')!=PROTOCOL or not _hex(identifier,32) or not _hex(token,64):raise ValueError('局域网传输请求无效。')
            if request.get('action')=='cancel':
                transfer=self._transfers.get(identifier)
                if transfer is None or transfer.direction!='receive' or not secrets.compare_digest(transfer.token,token):raise ValueError('无法取消这项传输。')
                self.cancel(identifier);_send_json(stream,dict(state='cancelled'));return
            entries=request.get('entries');total=_validate_manifest(entries);native=native_offer(request.get('native'))
            digest=hashlib.sha256(json.dumps(entries,sort_keys=True).encode()).hexdigest();deadline=time.monotonic()+8
            while True:
                with self._lock:
                    transfer=self._transfers.get(identifier)
                    if transfer is None:
                        if sum(not item.finished and not item.cancel.is_set() for item in self._transfers.values())>=16:raise ValueError('待处理的传输请求过多。')
                        transfer=_Transfer(identifier,'receive',str(request.get('sender') or '局域网设备')[:64],
                            entries=entries,token=token,manifest_hash=digest,total=total)
                        self._transfers[identifier]=transfer;self._native_specs[identifier]=native
                        self._incoming_offer(transfer,host,_authentication_code(stream,identifier))
                    elif (transfer.direction!='receive' or transfer.cancel.is_set() or transfer.token!=token
                          or transfer.manifest_hash!=digest or self._native_specs[identifier]!=native):raise ValueError('无法恢复这项传输。')
                    if not transfer.active:
                        transfer.active=True;transfer.socket=stream;transfer.receiver_idle.clear();acquired=True;break
                if transfer.cancel.is_set() or self._stopped.is_set():raise InterruptedError('已取消传输。')
                if time.monotonic()>deadline:raise ConnectionError('上一条传输控制连接尚未释放。')
                transfer.receiver_idle.wait(.05)
            deadline=time.monotonic()+CONSENT_TIMEOUT_SECONDS
            while not transfer.accepted.wait(.1):
                if self._stopped.is_set() or transfer.cancel.is_set():raise InterruptedError('已取消传输。')
                if time.monotonic()>deadline:raise TimeoutError('接收请求已超时。')
            if transfer.cancel.is_set() or self._stopped.is_set() or transfer.destination is None:raise InterruptedError('已拒绝传输。')
            own=dict(transport=TRANSPORT,device_id=self.engine.device_id,port=self.engine.port,content=native['content'])
            _send_json(stream,dict(state='ready',native=own))
            if transfer.finished:_send_json(stream,dict(state='complete'));return
            if transfer.stage is None:
                self._stage(transfer,transfer.destination,'.flickdock-receiving-'+identifier)
            stage=Path(transfer.stage);network=stage/'network';network.mkdir(exist_ok=True)
            last=[0.]
            def progress(received,size):
                transfer.done=min(total,received*total//size)
                now=time.monotonic()
                if now-last[0]>=PROGRESS_SECONDS or received==size:
                    last[0]=now;_send_json(stream,dict(state='progress',received=transfer.done))
                    self._notify(transfer,'transferring',route='lan')
            job=ReceiveJob(self.engine,'fd-manual-'+identifier,network,native['device_id'],[address(host,native['port'])],native['content'],
                           cancel=transfer.cancel,progress=progress)
            payload=job.receive();job.close();job=None
            offer=dict(total=total,count=sum(row['kind']=='file' for row in entries),names=[row['path'] for row in entries if '/' not in row['path']],entries=entries)
            transfer.result_paths=deliver(payload,stage,transfer.destination,native['content'],offer,transfer.cancel)
            transfer.done=total;transfer.finished=True
            self._notify(transfer,'complete',route='lan')
            self.completed.emit(dict(id=identifier,direction='receive',peer=transfer.peer,paths=transfer.result_paths,total=total))
            _send_json(stream,dict(state='complete'))
        except Exception as error:
            if transfer is not None and acquired and not transfer.finished:
                network_error=isinstance(error,(ConnectionError,TimeoutError,OSError)) and not isinstance(error,InterruptedError)
                if not network_error:transfer.cancel.set()
                terminal=('cancelled' if isinstance(error,InterruptedError) else 'interrupted' if network_error else 'error',str(error))
        finally:
            try:
                try:
                    if job is not None:job.close()
                finally:
                    if stream is not None:stream.close()
                    else:raw.close()
            finally:
                if transfer is not None and acquired:
                    transfer.socket=None
                    if transfer.cancel.is_set() or transfer.finished:self._cleanup(transfer)
                    transfer.active=False;transfer.updated=time.monotonic();transfer.receiver_idle.set()
                    if terminal:self._notify(transfer,terminal[0],message=terminal[1],route='lan')
                self._slots.release()

    def _expire(self):
        now=time.monotonic()
        with self._lock:
            pending=[self._transfers[key] for key,due in self._cleanup_due.items()
                     if due<=now and key in self._transfers and not self._transfers[key].active]
        for transfer in pending:self._cleanup(transfer)
        super()._expire()
        with self._lock:
            self._native_specs={key:value for key,value in self._native_specs.items() if key in self._transfers}

    def close(self):
        if self._shutdown is not None:return
        super().close()
        def shutdown():
            deadline=time.monotonic()+SHUTDOWN_WORKER_GRACE_SECONDS
            for thread in tuple(self._threads):thread.join(max(0,deadline-time.monotonic()))
            self.engine.close()
            deadline=time.monotonic()+SHUTDOWN_WORKER_GRACE_SECONDS
            for thread in tuple(self._threads):thread.join(max(0,deadline-time.monotonic()))
            with self._lock:retired=[item for item in self._transfers.values() if not item.active and (item.finished or item.cancel.is_set())]
            for transfer in retired:self._cleanup(transfer)
        # macOS has no Windows Job Object. Keep Python alive until the owned
        # kernel releases its profile, even after Qt's event loop has exited.
        self._shutdown=threading.Thread(target=shutdown,daemon=False,name='FlickDock-native-LAN-close');self._shutdown.start()
