"""Anonymous code pairing, consent and native WebRTC file transport.

The LAN file protocol is reused over a framed ordered DTLS DataChannel.
Stable device credentials and signaling are separate from cloud login.
"""
import hashlib,json,os,re,secrets,shutil,socket,sys,threading,time
from pathlib import Path
from uuid import uuid4
from PySide6.QtCore import QTimer,QUrl,Signal
from PySide6.QtNetwork import QNetworkRequest,QNetworkProxy,QNetworkProxyFactory,QNetworkProxyQuery
from PySide6.QtWebSockets import QWebSocket
from lan_transfer import LanTransferService,_Transfer,_validate_manifest
from selection_archive import _is_link
from webrtc_stream import WebRTCStream
from cloud_config import DEFAULT_CLOUD_URL,CLOUD_ORIGINS
SIGNAL_ORIGINS=CLOUD_ORIGINS

def _turn_http_proxy(port):
    # Match Qt's existing destination-specific proxy/bypass selection. Proxy
    # credentials travel only through the private child-process IPC channel.
    for proxy in QNetworkProxyFactory.proxyForQuery(QNetworkProxyQuery(QUrl('https://turn.cloudflare.com:'+str(port)))):
        if proxy.type()==QNetworkProxy.ProxyType.NoProxy:return None
        if proxy.type() in (QNetworkProxy.ProxyType.HttpProxy,QNetworkProxy.ProxyType.Socks5Proxy):
            return dict(type='http' if proxy.type()==QNetworkProxy.ProxyType.HttpProxy else 'socks5',
                        host=proxy.hostName(),port=proxy.port(),username=proxy.user(),password=proxy.password())
    return None

class WanTransferService(LanTransferService):
    identity_changed=Signal(dict)
    custom_code_changed=Signal(dict)
    _command=Signal(dict)
    _native=Signal(dict)
    mode='wan'
    def __init__(self,base,parent=None,*,device_name='',enabled=True,origin=DEFAULT_CLOUD_URL,node=None,allow_test_origin=False,qa_force_relay=False):
        super().__init__(parent,device_name=device_name,discovery=False)
        self.base=Path(base);self.enabled=enabled;self.origin=origin;self.node=node;self.code=''
        self._code_refresh_at=0.;self._code_expires_at=0.;self._code_duration=240.;self._refresh_sent_at=0.
        self._last_signal=0.;self._peer_poll_count=0
        self._custom_code={};self._custom_expires=0.;self._custom_operation=''
        self.account_service=None;self.lan_id='';self.nearby_peers_provider=lambda:[]
        self._bind_epoch=0;self._bind_key=None;self._bound_identity=None;self._pending_identity=None
        self._account_nonce='';self._account_ack=False
        self.allow_test_origin=allow_test_origin
        self._qa_force_relay=bool(qa_force_relay and os.environ.get('QT_QPA_PLATFORM')=='offscreen')
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
        self._command.connect(self._handle_command);self._native.connect(self._handle_native)
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
        self.account_service=account;self.lan_id=lan_id
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
            self._bound_identity=identity;self._send_signal(dict(type='recent'))
        key=self.account_service.api.request('POST','/v1/wan/account',done,
            data={'credential':self._credential,'lan_id':self.lan_id,'nonce':self._account_nonce},timeout_ms=12000)
        if self._bind_key is pending:self._bind_key=key

    def nearby_peer(self,identifier):
        peer=self._peers.get(identifier,{})
        return next((row for row in self.nearby_peers_provider() if row.get('id')==peer.get('lan_id')),None)

    def start(self):
        if self.enabled and not self._connected and not self.retry.isActive():self.retry.start(0)
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
        self._connection_error='远程连接超时，正在切换备用入口。'
        self._abort_signaling();self._disconnected()
    def reconnect(self):
        if self._closing or not self.enabled or time.monotonic()-self._manual_retry_at<1:return
        self._manual_retry_at=time.monotonic();self.retry.stop();self.connect_timeout.stop()
        self._abort_signaling();self._connected=False;self._connect_attempt=0
        self._origin_index=(self._origin_index+1)%len(self._origins);self.retry.start(0)
    def _hello(self):
        self._connected=True;self.retry.stop();self.poll.start();self.code_timer.start()
        self._last_signal=time.monotonic()
        self._send_signal(dict(type='hello',name=self.device_name,platform='macos' if sys.platform=='darwin' else 'windows',lan_id=self.lan_id))
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
        self.custom_code_changed.emit(self.custom_identity())
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
            candidates=[(key,value) for key,value in self._transfers.items()
                        if not value.active and key not in self._pending and now-value.updated>1800]
        if not candidates:return
        self._retirement_running=True
        def release():
            try:
                for key,value in candidates:
                    # Keep the same ACK/resume grace as LAN. Disk cleanup must
                    # not run on Qt or while holding the shared task lock.
                    if self._cleanup(value):
                        with self._lock:
                            if self._transfers.get(key) is value and not value.active and key not in self._pending:
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
    def custom_identity(self):
        value=dict(self._custom_code);remaining=max(0.,self._custom_expires-time.monotonic())
        value.update(online=self._connected,remaining=remaining)
        if value.get('status') in ('available','connecting') and not remaining:value['status']='expired'
        return value
    def set_custom_code(self,code):
        code=str(code).strip().upper()
        if not re.fullmatch('[0-9]{4}',code):raise ValueError('请输入 4 位数字。')
        self._custom_operation=uuid4().hex
        if not self._send_signal(dict(type='custom_set',id=self._custom_operation,code=code)):
            raise ValueError('远程服务尚未连接，请稍后再试。')
    def cancel_custom_code(self):
        self._custom_operation=uuid4().hex
        if not self._send_signal(dict(type='custom_cancel',id=self._custom_operation,nonce=self._custom_code.get('nonce',''))):
            raise ValueError('远程服务尚未连接，请稍后再试。')

    def check_code(self):
        if not self._connected:return
        if self._custom_code:self.custom_code_changed.emit(self.custom_identity())
        now=time.monotonic()
        if self._refresh_sent_at and now-self._refresh_sent_at>=12:
            self.socket.abort();self._disconnected();return
        if now>=self._code_refresh_at and not self._refresh_sent_at:
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
    def _recover_signaling(self):
        for key,value in list(self._outbox.items()):
            self._send_signal(value)
        for identifier,state in list(self._pending.items()):
            if state.get('done'):
                self._send_signal(dict(type='complete',id=identifier));continue
            bridge=state.get('bridge')
            if bridge and bridge.opened.is_set() and not bridge.closed:
                self._send_signal(dict(type='connected',id=identifier,generation=state['generation']));continue
            if state['role']=='send' and state.get('request'):
                self._send_signal(state['request'])
            elif state['role']=='send' and state.get('prepare') and not state.get('prepared_ok'):
                self._send_signal(state['prepare'])
            elif state['role']=='receive' and state.get('destination'):
                self._send_signal(dict(type='accept',id=identifier))
    def _request_retry(self,identifier,state):
        if state.get('retry_inflight'):return
        if self._send_signal(dict(type='retry',id=identifier)):state['retry_inflight']=True
    def _handle_command(self,value):
        if value.pop('_retire',False):
            state=self._pending.pop(value['id'],None)
            if state and state.get('bridge'):state['bridge'].shutdown()
        elif value.pop('_connect',False):
            identifier=value['id'];state=self._pending.get(identifier)
            if not state:return
            state['request']=value
            if state.get('requested'):self._request_retry(identifier,state)
            else:self._send_signal(value)
        elif value.get('type') in ('cancel','reject','complete'):
            self._outbox[(value['id'],value['type'])]=value
            while len(self._outbox)>64:self._outbox.pop(next(iter(self._outbox)))
            self._send_signal(value)
        else:self._send_signal(value)

    def _message(self,message):
        if len(message)>100000:return
        try:
            value=json.loads(message)
            if value.get('schema_version')!=1:return
            self._last_signal=time.monotonic()
            kind=value.get('type');identifier=value.get('id');state=self._pending.get(identifier)
            if kind=='custom_code':
                if value.get('operation') and value['operation']!=self._custom_operation:return
                self._custom_code=dict(value)
                self._custom_expires=time.monotonic()+max(0.,min(600.,(float(value.get('expires',0))-float(value.get('serverNow',time.time()*1000)))/1000))
                self.custom_code_changed.emit(self.custom_identity());return
            if kind=='error' and value.get('requestType') in ('custom_set','custom_cancel','custom_status'):
                if identifier and identifier!=self._custom_operation:return
                self.custom_code_changed.emit(dict(self.custom_identity(),error=str(value.get('message') or '接收码设置失败。')));return
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
                    if not re.fullmatch('[23456789A-HJ-NP-Z]{8}',self.code):self.code=''
                    if not self.code:raise ValueError('Invalid receiving code')
                    self.connect_timeout.stop();self._connect_attempt=0;self._connection_error=''
                    self._code_refresh_at=now+refresh;self._code_expires_at=now+expires
                    self._code_duration=max(1.,refresh);self._refresh_sent_at=0.
                    self.identity_changed.emit(self.code_identity())
                    self._bind_account()
                    self.status_changed.emit('远程互传已就绪。同账号或最近设备可直接发送。')
                    if reconnect:self._recover_signaling()
                return
            if kind=='request':
                if any(key[0]==identifier and key[1] in ('cancel','reject') for key in self._outbox):
                    self._send_signal(dict(type='cancel',id=identifier));return
                if not re.fullmatch('[a-f0-9]{32}',str(identifier)) or identifier in self._pending or len(self._pending)>=32:return
                total=int(value['total']);peer=value['peer']
                if (total<0 or not isinstance(peer,dict) or not re.fullmatch('[a-f0-9]{64}',str(peer.get('id','')))
                        or not re.fullmatch('[a-f0-9]{64}',str(value.get('token','')))
                        or not re.fullmatch('[a-f0-9]{64}',str(value.get('digest','')))):return
                self._pending[identifier]=dict(role='receive',peer=peer,token=value['token'],digest=value['digest'],total=total,count=value['count'],names=value['names'],ready=threading.Event(),generation=0)
                device_code=str(value.get('deviceCode') or '')
                if not re.fullmatch('[23456789A-HJ-NP-Z]{8}',device_code):device_code=''
                self.offer_received.emit(dict(id=identifier,peer=peer['name'],host='远程',code='',device_code=device_code,total=total,count=value['count'],names=value['names']))
                return
            if not state:return
            if kind=='prepared':
                peer=value.get('peer',{})
                if state['role']!='send' or not re.fullmatch('[a-f0-9]{64}',str(peer.get('id',''))):return
                state['peer']=peer;state['prepared_ok']=True;state['prepared'].set()
            elif kind=='waiting':
                state['requested']=True
                state['peer']=value['peer'];transfer=self._transfers.get(identifier)
                if transfer:transfer.peer=value['peer']['name'];self._notify(transfer,'waiting')
            elif kind=='resumed':
                state['requested']=True
                if value.get('settled'):
                    if state.get('done'):self._pending.pop(identifier,None)
                elif state['role']=='send' and value.get('accepted') and not state.get('done'):
                    bridge=state.get('bridge')
                    generation=value.get('generation',0)
                    if generation<state.get('generation',0) or state.get('retry_inflight'):return
                    if not bridge or not bridge.opened.is_set() or bridge.closed:
                        self._request_retry(identifier,state)
            elif kind=='settled':
                if state.get('done'):self._pending.pop(identifier,None)
            elif kind=='begin':self._begin(identifier,state,value)
            elif kind in ('description','candidate') and value.get('generation')==state.get('generation'):
                bridge=state.get('bridge')
                if bridge:bridge.control({key:item for key,item in value.items() if key not in ('id','generation','schema_version')})
            elif kind=='cancelled':
                state['error']='对方已取消或拒绝传输。';state['ready'].set()
                self.cancel(identifier,remote=True)
            elif kind=='error':
                if value.get('generation') is not None and value['generation']!=state.get('generation'):return
                if value.get('requestType') in ('description','candidate','connected'):return
                bridge=state.get('bridge')
                if value.get('requestType')=='relay' and bridge and bridge.opened.is_set() and not bridge.closed:return
                state['error']=str(value.get('message') or '远程连接失败。');state['ready'].set();self.status_changed.emit(state['error'])
                if state.get('prepared'):state['prepared'].set()
                if state['role']=='receive' and identifier not in self._transfers:
                    self.progress.emit(dict(id=identifier,state='error',direction='receive',peer=state['peer']['name'],received=0,total=state.get('total',0),message=state['error']))
                    self._handle_command(dict(_retire=True,id=identifier))
        except (OSError,ValueError,KeyError,TypeError):self.status_changed.emit('远程连接状态无效，请重新连接。')

    def _begin(self,identifier,state,value):
        previous=state.get('bridge')
        if state.get('done') and state['role']=='send':return
        if value.get('relayAttempt') and previous and previous.opened.is_set() and not previous.closed:return
        if previous:
            if previous.route=='relay':state['relay_bytes_prior']=state.get('relay_bytes_prior',0)+sum(previous.traffic().values())
            previous.shutdown()
        generation=value['generation'];state['generation']=generation;state['ready'].clear();state.pop('error',None)
        state['requested']=True;state['retry_inflight']=False
        state['relay_allowed']=bool(value.get('relayAllowed'));state['connect_started']=time.monotonic()
        state['relay_requested']=False;state.pop('network_error',None)
        state['relay_transport']=''
        urls=[]
        for server in value['iceServers']:
            for url in server.get('urls',[]):
                parsed=re.fullmatch(r'(stun|turn|turns):([\w.-]+):(\d+)(?:\?transport=(udp|tcp))?',url)
                if not parsed:continue
                scheme,host,port,transport=parsed.groups()
                if host not in ('stun.cloudflare.com','turn.cloudflare.com'):continue
                if scheme=='stun':urls.append(url)
                else:urls.append(dict(hostname=host,port=int(port),username=server['username'],password=server['credential'],relayType='TurnTls' if scheme=='turns' else 'TurnTcp' if transport=='tcp' else 'TurnUdp'))
        try:bridge=WebRTCStream(lambda event:self._native.emit(dict(id=identifier,generation=generation,event=event)),node=self.node)
        except (OSError,RuntimeError) as error:
            self._begin_failed(identifier,state,'远程传输组件启动失败，请检查安装完整性和系统权限。',
                               'COMPONENT_'+type(error).__name__.upper());return
        state['bridge']=bridge
        try:
            proxies={str(port):_turn_http_proxy(port) for port in sorted({row['port'] for row in urls if isinstance(row,dict) and row.get('relayType')=='TurnTls'})}
            bridge.control(dict(type='init',iceServers=urls,initiator=state['role']=='send',forceRelay=self._qa_force_relay,preferTls=not state.get('tls_failed',False),proxies=proxies))
        except (OSError,RuntimeError):
            bridge.shutdown();self._begin_failed(identifier,state,'远程传输组件初始化失败，请重试或重新安装当前版本。','INIT_FAILED');return
        if state['role']=='send':
            transfer=self._transfers.get(identifier)
            if transfer:self._notify(transfer,'connecting',route='relay' if value.get('relayAttempt') else 'direct')
            if value.get('relayAllowed'):QTimer.singleShot(8000,lambda:self._relay_if_needed(identifier,generation))
    def _relay_if_needed(self,identifier,generation):
        state=self._pending.get(identifier)
        if state and state['generation']==generation and not state['bridge'].opened.is_set():
            if not state.get('relay_allowed'):return
            state['relay_allowed']=False
            state['relay_requested']=True
            self.status_changed.emit('直连暂未成功，正在尝试中继…')
            self._send_signal(dict(type='relay',id=identifier,generation=generation))
    def _handle_native(self,value):
        identifier=value['id'];state=self._pending.get(identifier)
        if not state or state.get('generation')!=value['generation']:return
        event=value['event'];kind=event.get('type')
        if (kind=='closed' and type(event.get('exit_code')) is int and event['exit_code']!=0
                and not state['bridge'].opened.is_set() and not self._closing):
            self._begin_failed(identifier,state,'远程传输组件异常退出，请检查安装完整性和系统权限。',
                               str(event.get('reason','UNKNOWN')));return
        if kind in ('description','candidate'):
            self._send_signal(dict(id=identifier,generation=state['generation'],**event))
        elif kind=='transport':
            state['relay_transport']=event.get('relayTransport','')
            if event.get('tlsFallback'):state['tls_failed']=True
        elif kind=='open':
            state['route']=event['route'];state['ready'].set()
            self._send_signal(dict(type='connected',id=identifier,generation=state['generation']))
            self.status_changed.emit('P2P 直连已建立。' if event['route']=='direct' else '中继连接已建立。')
            if state['role']=='receive':
                if self._slots.acquire(blocking=False):self._thread(self._incoming,state['bridge'],state['peer']['id'])
                else:self.cancel(identifier)
        elif kind=='stats':
            transfer=self._transfers.get(identifier)
            if transfer and state.get('progress_state')=='transferring' and not transfer.cancel.is_set():
                self._notify(transfer,'transferring')
        elif kind in ('error','closed') or kind=='state' and event.get('state')=='failed':
            from network_diagnostics import record_event
            record_event(self.base,'remote','ice','WEBRTC_'+str(kind).upper())
            # A TLS handshake alone does not prove that TURN allocation works.
            # The next bounded, resumable attempt can try the supplied UDP row.
            if state.get('relay_transport')=='tls' and not state.get('done'):state['tls_failed']=True
            if not state['bridge'].opened.is_set():
                if state.get('relay_requested'):return
                if state['role']=='send' and state.get('relay_allowed'):
                    self._relay_if_needed(identifier,state['generation'])
                else:
                    state['error']='P2P 连接未能建立，请检查网络后重试。';state['network_error']=True;state['ready'].set()
            if kind=='error':self.status_changed.emit(str(event.get('message') or 'WebRTC 连接中断。'))

    def _begin_failed(self,identifier,state,message,code):
        from network_diagnostics import record_event
        record_event(self.base,'remote','component',code)
        state['error']=message;state['ready'].set();self.status_changed.emit(message)
        if state.get('prepared'):state['prepared'].set()
        self._send_signal(dict(type='cancel',id=identifier))
        if state.get('role')=='receive' and identifier not in self._transfers:
            self.progress.emit(dict(id=identifier,state='error',direction='receive',peer=state['peer']['name'],
                                    received=0,total=state.get('total',0),message=message))
            self._handle_command(dict(_retire=True,id=identifier))

    def send(self,peer_id,paths):
        if not self._connected:raise ValueError('远程服务尚未连接，请稍后再试。')
        peer=self._peers.get(peer_id)
        if not peer and not re.fullmatch('(?:[0-9]{4}|[23456789A-HJ-NP-Z]{8})',str(peer_id)):raise ValueError('请输入有效的接收码，或选择最近设备。')
        if peer and not peer.get('online'):raise ValueError('对方设备不在线，无法发送。')
        identifier=uuid4().hex;peer=dict(peer or dict(code=peer_id,name='接收码 '+peer_id))
        with self._lock:
            if sum(t.active and t.direction=='send' for t in self._transfers.values())>=2:raise ValueError('请等待当前传输完成。')
            transfer=_Transfer(identifier,'send',peer['name'],active=True);transfer.token=secrets.token_hex(32)
            transfer.remote_peer=peer;self._transfers[identifier]=transfer
            self._pending[identifier]=dict(role='send',peer=peer,ready=threading.Event(),generation=0)
        if peer.get('code'):
            state=self._pending[identifier];state['prepared']=threading.Event()
            state['prepare']=dict(type='prepare',id=identifier,token=transfer.token,code=peer['code'])
            self._send_signal(state['prepare'])
            self._thread(self._prepared_outgoing,transfer,list(paths))
        else:self._thread(self._outgoing,transfer,peer,list(paths))
        return identifier
    def _prepared_outgoing(self,transfer,paths):
        state=self._pending.get(transfer.transfer_id)
        if not state:
            self._notify(transfer,'cancelled' if transfer.cancel.is_set() else 'error',message='已取消传输。',phase='prepare')
            transfer.active=False;transfer.updated=time.monotonic();return
        self._notify(transfer,'preparing');deadline=time.monotonic()+25
        while not state['prepared'].wait(.1):
            if self._stopped.is_set() or transfer.cancel.is_set():break
            if time.monotonic()>=deadline:
                state['error']='接收码验证超时，请检查网络后重试。';break
        if state.get('prepared_ok') and not transfer.cancel.is_set() and not state.get('error'):
            transfer.peer=state['peer']['name'];self._outgoing(transfer,state['peer'],paths);return
        self._command.emit(dict(type='cancel',id=transfer.transfer_id))
        self._notify(transfer,'cancelled' if transfer.cancel.is_set() else 'error',
            message=state.get('error') or '已取消传输。',phase='prepare')
        transfer.active=False;transfer.updated=time.monotonic()
    def _connect_peer(self,peer,transfer,entries):
        state=self._pending[transfer.transfer_id];state['ready'].clear();state.pop('error',None);state.pop('connect_started',None)
        request=dict(type='request',id=transfer.transfer_id,token=transfer.token,digest=hashlib.sha256(json.dumps(entries,sort_keys=True).encode()).hexdigest(),
            total=str(transfer.total),count=sum(row['kind']=='file' for row in entries),names=[row['path'] for row in entries if '/' not in row['path']][:12])
        request.update(dict(prepared=True) if state.get('prepared_ok') else dict(code=peer['code']) if peer.get('code') else dict(peer=peer['id']))
        self._command.emit(dict(_connect=True,**request));deadline=time.monotonic()+210
        while not state['ready'].wait(.1):
            if self._stopped.is_set() or transfer.cancel.is_set():raise InterruptedError('已取消传输。')
            if time.monotonic()>deadline:raise TimeoutError('对方未确认或连接未成功，请重新发起。')
            if state.get('connect_started') and time.monotonic()-state['connect_started']>30:raise TimeoutError('无法建立远程连接，请重试。')
        if state.get('error'):
            if state.pop('network_error',False):raise ConnectionError(state['error'])
            raise ValueError(state['error'])
        from nearby_transport import try_nearby
        return try_nearby(state['bridge'],'send',transfer.cancel)
    def _incoming_stream(self,raw):
        from nearby_transport import try_nearby
        return try_nearby(raw,'receive',self._stopped)
    def _stream_code(self,stream,identifier):return ''
    def _incoming_offer(self,transfer,host,code):
        state=self._pending.get(transfer.transfer_id)
        if (not state or state['role']!='receive' or not state.get('destination') or state['peer']['id']!=host
                or state['digest']!=transfer.manifest_hash or state['token']!=transfer.token
                or state['total']!=transfer.total or state['count']!=sum(e['kind']=='file' for e in transfer.entries)
                or state['names']!=[e['path'] for e in transfer.entries if '/' not in e['path']][:12]):
            transfer.cancel.set();transfer.accepted.set();self._transfers.pop(transfer.transfer_id,None)
            self.progress.emit(dict(id=transfer.transfer_id,state='error',direction='receive',peer=transfer.peer,received=0,total=transfer.total,message='收到的文件清单与已确认的请求不符。'))
            self._command.emit(dict(type='cancel',id=transfer.transfer_id))
            self._command.emit(dict(_retire=True,id=transfer.transfer_id))
            raise ValueError('收到的文件清单与已确认的请求不符。')
        transfer.peer=state['peer']['name'];transfer.destination=state['destination'];transfer.accepted.set()
    def accept(self,identifier,destination):
        state=self._pending.get(identifier)
        if not state or state['role']!='receive' or state.get('destination'):return False
        path=Path(destination).expanduser().resolve(strict=True)
        if not path.is_dir() or _is_link(path):raise ValueError('请选择可写入的接收文件夹。')
        if shutil.disk_usage(path).free<state['total']+1024*1024:raise ValueError('接收位置的可用空间不足。')
        state['destination']=path;self._send_signal(dict(type='accept',id=identifier));return True
    def cancel(self,identifier,*,remote=False):
        active=bool(self._transfers.get(identifier) and self._transfers[identifier].active)
        state=self._pending.get(identifier)
        if state:
            state['error']='已取消传输。';state['ready'].set()
            if not remote:self._command.emit(dict(type='cancel',id=identifier))
            if state.get('bridge'):state['bridge'].shutdown()
        if identifier in self._transfers:super().cancel(identifier)
        elif state:self.progress.emit(dict(id=identifier,state='cancelled',direction=state['role'],peer=state['peer']['name'],received=0,total=state.get('total',0)))
        # An active worker publishes its terminal wire counters before the
        # queued retire command removes the bridge context.
        if not active:self._pending.pop(identifier,None)
    def _cancel_remote(self,transfer):self._command.emit(dict(type='cancel',id=transfer.transfer_id))
    def _completed_wan(self,value):
        state=self._pending.get(value['id'])
        if state:state['done']=True
        self._command.emit(dict(type='complete',id=value['id']))
        def retire():
            transfer=self._transfers.get(value['id'])
            if transfer and transfer.finished:self._pending.pop(value['id'],None)
        # Keep the receiver's idempotent final acknowledgement available beyond
        # the protocol's 45-second ACK timeout and all bounded sender retries.
        QTimer.singleShot(600000,retire)
    def _notify(self,transfer,state,**extra):
        pending=self._pending.get(transfer.transfer_id,{})
        pending['progress_state']=state
        if pending.get('route'):extra['route']=pending['route']
        bridge=pending.get('bridge');relay_bytes=pending.get('relay_bytes_prior',0)
        worker=threading.current_thread() is not threading.main_thread()
        if bridge and worker and state in ('error','cancelled','interrupted'):
            # A failed writer may close before stdout's last stats frame is
            # consumed. Drain in the transfer worker before publishing history;
            # retiring this context must never wait for a process on the GUI.
            bridge.close(abort=True)
        if bridge and bridge.route=='relay':
            final=state in ('complete','error','cancelled','interrupted') and worker
            relay_bytes+=sum(bridge.traffic(refresh=final).values())
        extra['relay_bytes']=relay_bytes
        super()._notify(transfer,state,**extra)
        if state in ('error','cancelled'):self._command.emit(dict(_retire=True,id=transfer.transfer_id))
    def close(self):
        self.retirement_timer.stop()
        self._closing=True;self.retry.stop();self.poll.stop();self.code_timer.stop();self.connect_timeout.stop();self.bind_retry.stop()
        if self._bind_key and self.account_service:self.account_service.api.cancel(self._bind_key)
        self.socket.abort();super().close()
        for state in self._pending.values():
            if state.get('bridge'):state['bridge'].shutdown()
