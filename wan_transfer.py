"""Manual remote consent/signaling; bytes use original Syncthing or account R2."""
from __future__ import annotations
import hashlib,json,os,re,secrets,shutil,sys,threading,time
from pathlib import Path
from uuid import uuid4
from PySide6.QtCore import QTimer,QUrl,Signal
from PySide6.QtNetwork import QNetworkRequest
from PySide6.QtWebSockets import QWebSocket
from lan_transfer import _Transfer
from syncthing_lan import SyncthingLanService
from syncthing_engine import check_device,check_addresses,EngineError
from syncthing_jobs import ReceiveJob,DirectUnavailable,deliver
from manual_payload import prepare,descriptor
from manual_cloud import ManualCloud
from selection_archive import _is_link
from cloud_config import DEFAULT_CLOUD_URL,CLOUD_ORIGINS
SIGNAL_ORIGINS=CLOUD_ORIGINS
RECEIVE_CODE=re.compile(r'(?=.*[2-9])(?=.*[A-HJ-NP-Z])[23456789A-HJ-NP-Z]{4}')
CONSENT_SECONDS=210
SIGNAL_SECONDS=45
ACK_SECONDS=180
REQUEST_RECEIPT_SECONDS=3600
PROGRESS_SECONDS=1.


class _DirectCancel:
    def __init__(self,transfer,state):self.transfer=transfer;self.state=state
    def is_set(self):return self.transfer.cancel.is_set() or self.state['fallback'].is_set()

class WanTransferService(SyncthingLanService):
    lan_authorization=Signal(dict)
    identity_changed=Signal(dict)
    _command=Signal(dict)
    mode='wan'

    def start(self):
        if self.enabled and not self._connected and not self.retry.isActive():self.retry.start(0)
        if self.enabled and not self._warming:
            self._warming=True
            def warm():
                try:self.engine.start()
                except (OSError,EngineError) as error:self.status_changed.emit(str(error))
            self._thread(warm)

    def _state(self,role,peer):
        return dict(role=role,peer=peer,ready=threading.Event(),wake=threading.Event(),fallback=threading.Event(),
                    saved=threading.Event(),prepared=threading.Event(),generation=0,last_progress=time.monotonic())

    def _recover_signaling(self):
        for value in tuple(self._outbox.values()):self._send_signal(value)
        for identifier,state in tuple(self._pending.items()):
            if state.get('done'):self._send_signal(dict(type='complete',id=identifier));continue
            if state.get('native_out'):self._send_signal(state['native_out'])
            if state.get('cloud_out') and self._bound_identity==self._account_identity() and self._bound_identity is not None:
                self._send_signal(state['cloud_out'])
            if state['role']=='send' and state.get('request'):self._send_signal(state['request'])
            elif state['role']=='send' and state.get('prepare'):self._send_signal(state['prepare'])
            elif state['role']=='receive' and state.get('destination'):self._send_signal(dict(type='accept',id=identifier))

    def _handle_command(self,value):
        state=self._pending.get(value.get('id'))
        if value.get('type')=='request' and state:state['request']=value
        if value.get('type')=='native_signal' and state:
            kind=value['value']['kind']
            if kind=='native':state['native_out']=value
            if kind=='r2':
                state['cloud_out']=value
                if self._bound_identity is None or self._bound_identity!=self._account_identity():return
        if value.get('type') in ('complete','cancel','reject'):
            self._outbox[(value['id'],value['type'])]=value
            while len(self._outbox)>64:self._outbox.pop(next(iter(self._outbox)))
        self._send_signal(value)

    def _signal(self,identifier,metadata):
        self._command.emit(dict(type='native_signal',id=identifier,generation=self._pending[identifier]['generation'],value=metadata))

    def _transfer_message(self,kind,identifier,state,value):
        if kind=='request':
            if not re.fullmatch('[a-f0-9]{32}',str(identifier)) or identifier in self._pending or len(self._pending)>=32:return
            peer=value['peer'];total=int(value['total']);count=value['count'];names=value['names']
            if (not isinstance(peer,dict) or not re.fullmatch('[a-f0-9]{64}',str(peer.get('id','')))
                    or 'syncthing-v2' not in peer.get('features',[]) or not 0<=total<(1<<63)
                    or type(count) is not int or not 0<=count<=100000 or not isinstance(names,list) or not 1<=len(names)<=12
                    or any(not isinstance(name,str) or not name or len(name)>255 or re.search(r'[\\/\x00-\x1f]',name) for name in names)
                    or not re.fullmatch('[a-f0-9]{64}',str(value.get('digest','')))
                    or not re.fullmatch('[a-f0-9]{64}',str(value.get('token','')))):
                self._send_signal(dict(type='reject',id=identifier));raise ValueError('请让两端使用当前版本的传输内核。')
            state=self._state('receive',peer);state.update(total=total,count=count,names=names,digest=value['digest'],token=value['token'],
                account_device=value.get('account_device') is True,account_id=value.get('account_id',''))
            self._pending[identifier]=state
            transfer=_Transfer(identifier,'receive',peer['name'],total=total,token=value['token'],manifest_hash=value['digest'])
            self._transfers[identifier]=transfer
            self.offer_received.emit(dict(id=identifier,peer=peer['name'],host='远程',code='',total=total,count=count,names=names,
                account_device=state['account_device'],account_id=state['account_id'],device_code=value.get('deviceCode','')))
            return
        if not state:return
        transfer=self._transfers[identifier]
        if kind=='prepared':
            peer=value['peer']
            if 'syncthing-v2' not in peer.get('features',[]):raise ValueError('请让接收端更新到当前版本。')
            state['peer']=peer;state['prepared_ok']=True;state['prepared'].set()
        elif kind=='waiting':
            state['peer']=value['peer'];transfer.peer=value['peer']['name'];self._notify(transfer,'waiting')
        elif kind in ('begin','resumed') and not value.get('settled'):
            if kind=='begin' and value.get('transport')!='syncthing-v2':
                self.cancel(identifier);raise ValueError('服务端尚未启用当前传输内核。')
            generation=value['generation']
            if type(generation) is not int or generation<1 or generation<state['generation']:return
            state['generation']=generation;state['ready'].set()
            if kind=='begin' or value.get('account_device') is True:state['account_device']=value.get('account_device') is True
            if state['role']=='receive' and not state.get('worker') and state.get('destination'):
                state['worker']=True;transfer.active=True;self._thread(self._receive,transfer,state)
        elif kind=='native_signal' and value.get('generation')==state['generation']:
            metadata=value['value'];signal=metadata['kind']
            if signal=='native':
                native=dict(device_id=check_device(metadata['device_id']),addresses=check_addresses(metadata['addresses'],'lan' if self.engine.loopback else 'remote'))
                if state['role']=='receive':native['content']=descriptor(metadata['content'])
                elif metadata.get('content') is not None:raise ValueError('接收端不能更改发送文件。')
                if state.get('native') and state['native']!=native:raise ValueError('传输设备或文件内容已改变。')
                state['native']=native
            elif signal=='fallback':state['fallback'].set()
            elif signal=='saved' and state['role']=='send':state['saved'].set()
            elif signal=='r2' and state['role']=='receive':
                from uuid import UUID
                item=str(UUID(metadata['id']))
                if item!=metadata['id'] or type(metadata['version']) is not int or metadata['version']<1:raise ValueError('云端文件版本无效。')
                spec=descriptor({key:metadata[key] for key in ('size','sha256')})
                if not state.get('native') or state['native']['content']!=spec:raise ValueError('云端文件与直连文件不一致。')
                state['cloud']=dict(id=item,version=metadata['version'],kind='transfer',**spec);state['fallback'].set()
            elif signal=='progress' and state['role']=='send':
                total=metadata['total'];received=metadata['received'];spec=state['payload']['descriptor']
                if type(received) is not int or total!=spec['size'] or not 0<=received<=total:raise ValueError('接收进度无效。')
                transfer.done=max(transfer.done,int(transfer.total*received/total));state['last_progress']=time.monotonic();self._notify(transfer,'transferring')
            else:raise ValueError('远程传输状态无效。')
            state['wake'].set()
        elif kind=='cancelled':self.cancel(identifier,remote=True)
        elif kind=='error':
            state['error']=str(value.get('message') or '远程传输失败。');state['wake'].set();state['prepared'].set();state['ready'].set()
        elif kind=='settled' or kind=='resumed' and value.get('settled'):
            if transfer.finished:self._pending.pop(identifier,None)

    def send(self,peer_id,paths):
        if not self._connected:raise ValueError('远程服务尚未连接，请稍后重试。')
        peer=self._peers.get(peer_id)
        if not peer and not RECEIVE_CODE.fullmatch(str(peer_id)):raise ValueError('请选择在线设备或输入四位接收码。')
        if peer and (not peer.get('online') or 'syncthing-v2' not in peer.get('features',[])):raise ValueError('请确认对方在线并使用当前版本。')
        identifier=uuid4().hex;peer=dict(peer or dict(code=peer_id,name='接收码 '+peer_id))
        with self._lock:
            if sum(item.active and item.direction=='send' for item in self._transfers.values())>=2:raise ValueError('请等待当前传输完成。')
            transfer=_Transfer(identifier,'send',peer['name'],active=True,token=secrets.token_hex(32));self._transfers[identifier]=transfer
            state=self._state('send',peer);self._pending[identifier]=state
        if peer.get('code'):
            state['prepare']=dict(type='prepare',id=identifier,token=transfer.token,code=peer['code']);self._send_signal(state['prepare'])
        self._thread(self._send,transfer,state,list(paths));return identifier

    def _wait(self,transfer,state,predicate,seconds):
        deadline=time.monotonic()+seconds
        while not predicate():
            if transfer.cancel.is_set() or self._stopped.is_set():raise InterruptedError('已取消传输。')
            if state.get('error'):raise ValueError(state['error'])
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('等待远程设备响应超时。')
            state['wake'].wait(min(.1,remaining));state['wake'].clear()

    def _send(self,transfer,state,paths):
        identifier=transfer.transfer_id;shared=False;record=None
        try:
            if state.get('prepare'):self._wait(transfer,state,lambda:state['prepared'].is_set(),25)
            if state.get('error'):raise ValueError(state['error'])
            self._notify(transfer,'preparing');stage=self._stage(transfer,self.base,'send-'+identifier)
            payload=prepare(paths,stage/'payload',transfer.cancel,self._preparation_progress(transfer));state['payload']=payload;transfer.total=payload['total']
            transfer.entries=payload['entries'];transfer.peer=state['peer']['name']
            request=dict(type='request',id=identifier,token=transfer.token,digest=hashlib.sha256(json.dumps(payload['entries'],sort_keys=True).encode()).hexdigest(),
                total=str(payload['total']),count=payload['count'],names=payload['names'][:12])
            request.update(dict(prepared=True) if state.get('prepared_ok') else dict(peer=state['peer']['id']))
            self._command.emit(request);self._wait(transfer,state,lambda:state['ready'].is_set(),CONSENT_SECONDS)
            try:self.engine.start();device=self.engine.device_id;addresses=self.engine.addresses()
            except (OSError,EngineError):
                # The native identity remains mandatory for an accepted snapshot.
                # If the bundled engine cannot start, fail visibly instead of
                # advertising a made-up peer or silently changing the transport.
                raise
            self._signal(identifier,dict(kind='native',device_id=device,addresses=addresses,content=payload['descriptor']))
            self._notify(transfer,'connecting',route='direct')
            self._wait(transfer,state,lambda:bool(state.get('native')) or state['fallback'].is_set(),SIGNAL_SECONDS)
            task='fd-manual-'+identifier
            if not state['fallback'].is_set():
                native=state['native'];self.engine.share(task,payload['folder'],native['device_id'],native['addresses'],send=True);shared=True
                started=time.monotonic()
                while not state['saved'].is_set() and not state['fallback'].is_set():
                    if transfer.cancel.is_set() or self._stopped.is_set():raise InterruptedError('已取消传输。')
                    if state.get('error'):raise ValueError(state['error'])
                    state['wake'].wait(.1);state['wake'].clear()
                    connected=self.engine.connections().get(native['device_id'],{}).get('connected') is True
                    now=time.monotonic()
                    if not connected and now-started>SIGNAL_SECONDS or connected and now-state['last_progress']>ACK_SECONDS:
                        state['fallback'].set();self._signal(identifier,dict(kind='fallback'))
            if state['fallback'].is_set() and not state['saved'].is_set():
                if shared:self.engine.unshare(task);shared=False
                if not self.cloud or not self._same_account(state):raise ConnectionError('无法建立直连；云端兜底需要两端登录同一账号。')
                state['route']='cloud';self._notify(transfer,'transferring',route='cloud')
                record=self.cloud.upload(payload,transfer.cancel,lambda done,total:self._cloud_progress(transfer,done,total))
                self._signal(identifier,dict(kind='r2',**{key:record[key] for key in ('id','version','size','sha256')}))
                self._wait(transfer,state,lambda:state['saved'].is_set(),REQUEST_RECEIPT_SECONDS)
            self._finish(transfer,paths)
        except Exception as error:self._failed(transfer,state,error)
        finally:
            try:
                if shared:self.engine.unshare('fd-manual-'+identifier)
                if record:self.cloud.retire(record)
            except Exception as error:self.status_changed.emit('传输清理未完成：'+str(error)[:180])
            finally:
                self._cleanup(transfer);transfer.active=False;transfer.updated=time.monotonic()

    def _same_account(self,state):
        peer=state['peer']
        return bool(self.account_service and self.account_service.signed_in and self._bound_identity==self._account_identity()
                    and (state.get('account_device') or peer.get('account_device')))

    def _cloud_progress(self,transfer,received,total):
        transfer.done=min(transfer.total,int(transfer.total*received/total));self._notify(transfer,'transferring',route='cloud')

    def _receive(self,transfer,state):
        identifier=transfer.transfer_id;job=None
        try:
            self._wait(transfer,state,lambda:bool(state.get('native')),SIGNAL_SECONDS)
            native=state['native'];spec=native['content'];target=state['destination']
            if shutil.disk_usage(target).free<transfer.total+spec['size']+1024*1024:raise ValueError('接收位置的可用空间不足。')
            stage=self._stage(transfer,target,'.flickdock-receiving-'+identifier)
            try:
                self.engine.start();self._signal(identifier,dict(kind='native',device_id=self.engine.device_id,addresses=self.engine.addresses()))
                if state['fallback'].is_set():raise DirectUnavailable('对方已请求云端兜底。')
                folder=stage/'network';folder.mkdir()
                last=[0.]
                def progress(done,total):
                    transfer.done=min(transfer.total,int(transfer.total*done/total));self._notify(transfer,'transferring',route='direct')
                    now=time.monotonic()
                    if now-last[0]>=PROGRESS_SECONDS or done==total:
                        last[0]=now;self._signal(identifier,dict(kind='progress',received=done,total=total))
                job=ReceiveJob(self.engine,'fd-manual-'+identifier,folder,native['device_id'],native['addresses'],spec,
                               cancel=_DirectCancel(transfer,state),progress=progress)
                payload=job.receive()
            except (DirectUnavailable,InterruptedError) as error:
                if transfer.cancel.is_set():raise
                if isinstance(error,InterruptedError) and not state['fallback'].is_set():raise
                if job:job.close();job=None
                if not self.cloud or not self._same_account(state):raise ConnectionError('无法建立直连；云端兜底需要两端登录同一账号。') from error
                state['fallback'].set();self._signal(identifier,dict(kind='fallback'));state['route']='cloud'
                self._notify(transfer,'connecting',route='cloud')
                self._wait(transfer,state,lambda:bool(state.get('cloud')),REQUEST_RECEIPT_SECONDS)
                payload=self.cloud.download(state['cloud'],stage/'r2',transfer.cancel,
                    lambda done,total:self._cloud_progress(transfer,done,total))
            paths=deliver(payload,stage,target,spec,state,transfer.cancel)
            transfer.finished=True;state['saved'].set();self._signal(identifier,dict(kind='saved'));self._finish(transfer,paths)
        except Exception as error:self._failed(transfer,state,error)
        finally:
            try:
                if job:job.close()
            finally:
                self._cleanup(transfer);transfer.active=False;transfer.updated=time.monotonic();transfer.receiver_idle.set()

    def _finish(self,transfer,paths):
        transfer.done=transfer.total;transfer.finished=True;transfer.result_paths=[str(path) for path in paths]
        self._notify(transfer,'complete');self.completed.emit(dict(id=transfer.transfer_id,direction=transfer.direction,peer=transfer.peer,
            paths=transfer.result_paths,total=transfer.total))

    def _failed(self,transfer,state,error):
        state['failed']=not transfer.cancel.is_set()
        state['error']=str(error);self._notify(transfer,'cancelled' if transfer.cancel.is_set() else 'error',message=str(error))
        if state['failed']:transfer.cancel.set()
        self._command.emit(dict(type='cancel',id=transfer.transfer_id))

    def accept(self,identifier,destination):
        state=self._pending.get(identifier)
        if not state or state['role']!='receive' or state.get('destination'):return False
        target=Path(destination).expanduser().resolve(strict=True)
        if not target.is_dir() or _is_link(target):raise ValueError('请选择可写入的接收文件夹。')
        state['destination']=target;self._transfers[identifier].destination=target
        self._send_signal(dict(type='accept',id=identifier));return True

    def reject(self,identifier):self.cancel(identifier)

    def cancel(self,identifier,*,remote=False):
        state=self._pending.get(identifier);transfer=self._transfers.get(identifier)
        # The server echoes our failure cleanup as cancelled. It must not erase
        # the originating error or turn a failed history entry into a user cancel.
        if remote and state and state.get('failed'):return
        if transfer:
            if transfer.finished:return
            transfer.cancel.set()
        if state:
            state['error']='已取消传输。';state['wake'].set();state['prepared'].set();state['ready'].set()
            if not remote:self._command.emit(dict(type='cancel',id=identifier))
        if transfer and not transfer.active:self._notify(transfer,'cancelled')

    def _completed_wan(self,value):
        state=self._pending.get(value['id'])
        if state:state['done']=True
        self._command.emit(dict(type='complete',id=value['id']))

    def _notify(self,transfer,state,**extra):
        pending=self._pending.get(transfer.transfer_id,{})
        extra['route']=pending.get('route',extra.get('route','direct'))
        super()._notify(transfer,state,**extra)

    def close(self):
        if self._closing:return
        self._closing=True;self.retirement_timer.stop();self.retry.stop();self.poll.stop();self.code_timer.stop();self.connect_timeout.stop();self.bind_retry.stop()
        if self._bind_key and self.account_service:self.account_service.api.cancel(self._bind_key)
        if self.cloud:self.cloud.close()
        self.socket.abort();super().close()
    def __init__(self,base,parent=None,*,device_name='',enabled=True,origin=DEFAULT_CLOUD_URL,binary=None,allow_test_origin=False,loopback=False):
        super().__init__(base,parent,device_name=device_name,discovery=False,binary=binary)
        if loopback and os.environ.get('QT_QPA_PLATFORM')!='offscreen':raise ValueError('Loopback is only available to isolated QA')
        self.engine.mode='remote';self.engine.loopback=loopback;self._warming=False;self.cloud=None
        self.base=Path(base);self.enabled=enabled;self.origin=origin;self.code=''
        self._code_refresh_at=0.;self._code_expires_at=0.;self._code_duration=240.;self._refresh_sent_at=0.
        self._last_signal=0.;self._peer_poll_count=0
        self.account_service=None;self.lan_id='';self.nearby_peers_provider=lambda:[]
        self._bind_epoch=0;self._bind_key=None;self._bound_identity=None;self._pending_identity=None
        self._account_nonce='';self._account_ack=False
        self.allow_test_origin=allow_test_origin
        self._credential='';self._pending={};self._outbox={};self._connected=False;self._closing=False
        self._origins=SIGNAL_ORIGINS if origin==DEFAULT_CLOUD_URL else (origin,)
        self._origin_index=0;self._connect_attempt=0;self._connection_error='';self._manual_retry_at=0.
        try:
            path=self.base/'wan-device.key';path.parent.mkdir(parents=True,exist_ok=True)
            if not path.exists():
                with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as stream:stream.write(secrets.token_hex(32))
            self._credential=path.read_text().strip()
            if not re.fullmatch('[a-f0-9]{64}',self._credential):raise ValueError('远程设备凭据已损坏。')
            self.device_id=hashlib.sha256(self._credential.encode()).hexdigest()
        except (OSError,ValueError):self._credential=''
        self.socket=QWebSocket(parent=self)
        self.socket.connected.connect(self._hello);self.socket.disconnected.connect(self._disconnected)
        self.socket.errorOccurred.connect(self._socket_error);self.socket.textMessageReceived.connect(self._message)
        self._command.connect(self._handle_command)
        self.retry=QTimer(self);self.retry.setSingleShot(True);self.retry.timeout.connect(self._connect)
        self.poll=QTimer(self);self.poll.setInterval(15000);self.poll.timeout.connect(self._heartbeat)
        self.code_timer=QTimer(self);self.code_timer.setInterval(1000);self.code_timer.timeout.connect(self.check_code)
        self.connect_timeout=QTimer(self);self.connect_timeout.setSingleShot(True);self.connect_timeout.setInterval(10000)
        self.connect_timeout.timeout.connect(self._connection_timed_out)
        self.completed.connect(self._completed_wan)
        self._retirement_running=False
        self.retirement_timer=QTimer(self);self.retirement_timer.setInterval(60000)
        self.retirement_timer.timeout.connect(self._retire_terminal);self.retirement_timer.start()
        self.bind_retry=QTimer(self);self.bind_retry.setSingleShot(True);self.bind_retry.setInterval(5000);self.bind_retry.timeout.connect(self._bind_account)



    def attach_account(self,account,*,lan_id='',nearby_peers=None):
        self.cloud=ManualCloud(account,self);self.account_service=account;self.lan_id=lan_id
        if nearby_peers:self.nearby_peers_provider=nearby_peers
        account.account_changed.connect(self._account_changed)
        account.devices_changed.connect(lambda _:self._send_signal(dict(type='recent')))
        self._account_changed()


    def _account_identity(self):
        account=self.account_service
        return (account.device_id,account.api.token) if account and account.signed_in else None


    def _account_changed(self,*_,force=False):
        identity=self._account_identity()
        if not force and identity is not None and identity in (self._bound_identity,self._pending_identity):return
        previous=self._bound_identity or self._pending_identity
        if previous is not None and previous!=identity:
            for identifier,transfer in tuple(self._transfers.items()):
                if not transfer.finished:self.cancel(identifier)
        self._bind_epoch+=1;self.bind_retry.stop();self._bound_identity=None
        if self._bind_key and self.account_service:self.account_service.api.cancel(self._bind_key)
        self._bind_key=None;self._pending_identity=identity
        self._account_nonce=secrets.token_hex(16);self._account_ack=False
        self._send_signal(dict(type='account_state',nonce=self._account_nonce,active=identity is not None))


    def _bind_account(self):
        identity=self._account_identity()
        if (not identity or not self._account_ack or not self.enabled or not self._connected or self._closing or self._bind_key
                or identity==self._bound_identity):return
        epoch=self._bind_epoch;pending=object();self._bind_key=pending
        def done(result,error):
            if epoch!=self._bind_epoch or self._closing:return
            self._bind_key=None
            if error:
                if getattr(error,'status',0)!=401:self.bind_retry.start()
                return
            self._bound_identity=identity;self._send_signal(dict(type='recent'));self._recover_signaling()
            self.peers_changed.emit(self.peers())
        key=self.account_service.api.request('POST','/v1/wan/account',done,
            data={'credential':self._credential,'lan_id':self.lan_id,'nonce':self._account_nonce},timeout_ms=12000)
        if self._bind_key is pending:self._bind_key=key


    def nearby_peer(self,identifier):
        peer=self._peers.get(identifier,{})
        return next((row for row in self.nearby_peers_provider() if row.get('id')==peer.get('lan_id')),None)


    def _connect(self):
        if self._closing or not self.enabled:return
        if not self._credential:
            self._connection_error='无法读取远程设备信息，请检查应用数据目录权限。'
            self.identity_changed.emit(self.code_identity());self.status_changed.emit(self._connection_error);return
        url=QUrl(self._origins[self._origin_index])
        local=self.allow_test_origin and url.scheme()=='http' and url.host()=='127.0.0.1'
        if not local and (url.scheme()!='https' or url.toString().rstrip('/') not in SIGNAL_ORIGINS):
            self.status_changed.emit('远程服务地址无效。');return
        url.setScheme('ws' if local else 'wss');url.setPath('/v1/wan/connect');url.setQuery(None);url.setFragment(None)
        request=QNetworkRequest(url);request.setRawHeader(b'Authorization',('Bearer '+self._credential).encode())
        self._connection_error='';self.identity_changed.emit(self.code_identity())
        self.status_changed.emit('正在连接远程传输服务…');self.connect_timeout.start();self.socket.open(request)


    def _abort_signaling(self):
        previous=self.socket.blockSignals(True)
        try:self.socket.abort()
        finally:self.socket.blockSignals(previous)


    def _socket_error(self,*_):
        from network_diagnostics import record_event
        record_event(self.base,'remote','signaling',getattr(self.socket.error(),'name','SOCKET_ERROR'))
        self._connection_error='远程连接失败：'+str(self.socket.errorString())[:180]
        self._abort_signaling();self._disconnected()


    def _connection_timed_out(self):
        from network_diagnostics import record_event
        record_event(self.base,'remote','signaling','CONNECT_TIMEOUT')
        self._connection_error='远程连接超时，正在重试。'
        self._abort_signaling();self._disconnected()


    def reconnect(self):
        if self._closing or not self.enabled or time.monotonic()-self._manual_retry_at<1:return
        self._manual_retry_at=time.monotonic();self.retry.stop();self.connect_timeout.stop()
        self._abort_signaling();self._connected=False;self._connect_attempt=0
        self._origin_index=(self._origin_index+1)%len(self._origins);self.retry.start(0)


    def _hello(self):
        self._connected=True;self.retry.stop();self.poll.start();self.code_timer.start()
        self._last_signal=time.monotonic()
        self._send_signal(dict(type='hello',name=self.device_name,platform='macos' if sys.platform=='darwin' else 'windows',lan_id=self.lan_id,features=['syncthing-v2']))
        self._account_changed(force=True)


    def _disconnected(self,*_):
        self.connect_timeout.stop()
        self._connected=False;self.poll.stop();self.code_timer.stop();self.code=''
        self._code_refresh_at=self._code_expires_at=self._refresh_sent_at=0.
        for state in self._pending.values():state['retry_inflight']=False
        self.peers_changed.emit(self.peers())
        if not self._closing and self.enabled and not self.retry.isActive():
            self._connect_attempt=min(11,self._connect_attempt+1)
            self._origin_index=(self._origin_index+1)%len(self._origins)
            self.retry.start(250 if self._connect_attempt==1 else min(300000,1000*2**(self._connect_attempt-2)))
            if not self._connection_error:self._connection_error='远程连接已断开，正在重试。'
            self.status_changed.emit(self._connection_error)
        self.identity_changed.emit(self.code_identity())


    def _heartbeat(self):
        if not self._connected:return
        if time.monotonic()-self._last_signal>40:
            self.socket.abort();self._disconnected();return
        self._send_signal(dict(type='ping'))
        self._peer_poll_count+=1
        if self._peer_poll_count%4==0:self._send_signal(dict(type='recent'))


    def _retire_terminal(self):
        if self._closing or self._retirement_running:return
        now=time.monotonic()
        with self._lock:
            for key,value in tuple(self._transfers.items()):
                if not value.active and now-value.updated>600 and (value.finished or value.cancel.is_set()):self._pending.pop(key,None)
            candidates=[(key,value) for key,value in self._transfers.items()
                        if not value.active and (self._cleanup_due.get(key,float('inf'))<=now or key not in self._pending and now-value.updated>1800)]
        if not candidates:return
        self._retirement_running=True
        def release():
            try:
                for key,value in candidates:
                    # Keep the same ACK/resume grace as LAN. Disk cleanup must
                    # not run on Qt or while holding the shared task lock.
                    if self._cleanup(value):
                        with self._lock:
                            if self._transfers.get(key) is value and not value.active and key not in self._pending and now-value.updated>1800:
                                self._transfers.pop(key,None)
            finally:self._retirement_running=False
        self._thread(release)


    def peers(self):
        rows=super().peers()
        return rows if self._connected else [dict(row,online=False) for row in rows]


    def code_identity(self):
        now=time.monotonic();valid=self._connected and self.code and now<self._code_expires_at
        return dict(code=self.code if valid else '',online=self._connected,error=self._connection_error,
            refresh_remaining=max(0.,self._code_refresh_at-now),refresh_duration=self._code_duration,
            expires_remaining=max(0.,self._code_expires_at-now),refreshing=bool(self._refresh_sent_at))


    def check_code(self):
        if not self._connected:return
        now=time.monotonic()
        if self._refresh_sent_at and now-self._refresh_sent_at>=12:
            self.socket.abort();self._disconnected();return
        if self.code and now>=self._code_refresh_at and not self._refresh_sent_at:
            if self._send_signal(dict(type='refresh')):self._refresh_sent_at=now
        if self.code and now>=self._code_expires_at:
            self.code='';self.identity_changed.emit(self.code_identity())


    def remove_peer(self,identifier):
        if identifier not in self._peers:return
        if not self._send_signal(dict(type='remove',peer=identifier)):
            raise ValueError('远程服务尚未连接，请稍后再试。')


    def _send_signal(self,value):
        if self._connected:
            return self.socket.sendTextMessage(json.dumps(value,separators=(',',':')))>0
        return False


    def _message(self,message):
        if len(message)>100000:return
        state=None
        try:
            value=json.loads(message)
            if value.get('schema_version')!=1:return
            self._last_signal=time.monotonic()
            kind=value.get('type');identifier=value.get('id');state=self._pending.get(identifier)
            if kind=='lan_authorization':
                self.lan_authorization.emit(value);return
            if kind=='account_state':
                if value.get('nonce')==self._account_nonce:
                    self._account_ack=True;self._bind_account()
                return
            if kind in ('cancelled','settled') or kind=='error' and value.get('requestType') in ('cancel','reject'):
                for key in list(self._outbox):
                    if key[0]==identifier:self._outbox.pop(key,None)
            if kind in ('identity','peers'):
                peers=value.get('recent',value.get('peers',[]))
                self._peers={row['id']:dict(row) for row in peers if isinstance(row,dict) and re.fullmatch('[a-f0-9]{64}',str(row.get('id','')))}
                self.peers_changed.emit(self.peers())
                if kind=='identity':
                    reconnect=not self.code
                    now=time.monotonic();server_now=float(value.get('serverNow',time.time()*1000))
                    expires=max(0.,min(300.,(float(value.get('expires',server_now+300000))-server_now)/1000))
                    refresh=max(0.,min(expires,(float(value.get('refreshAt',server_now+240000))-server_now)/1000))
                    self.code=str(value.get('code') or '')
                    if not RECEIVE_CODE.fullmatch(self.code):
                        self.code='';self._refresh_sent_at=0.;self.code_timer.stop();self.connect_timeout.stop()
                        self._connection_error='远程传输服务版本不匹配，请更新服务端。'
                        self.status_changed.emit(self._connection_error);self.identity_changed.emit(self.code_identity());return
                    self.connect_timeout.stop();self._connect_attempt=0;self._connection_error=''
                    self._code_refresh_at=now+refresh;self._code_expires_at=now+expires
                    self._code_duration=max(1.,refresh);self._refresh_sent_at=0.
                    self.identity_changed.emit(self.code_identity())
                    self._bind_account()
                    if reconnect:
                        self.status_changed.emit('')
                        self._recover_signaling()
                return
            self._transfer_message(kind,identifier,state,value)
        except (OSError,ValueError,KeyError,TypeError) as error:
            problem='远程连接状态无效：'+str(error)[:180]
            if state:
                state['error']=problem
                for key in ('wake','ready','prepared'):state[key].set()
            self.status_changed.emit(problem)

