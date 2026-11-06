"""Account-verified consent tickets for manual LAN transfers.

Only bounded authorization metadata uses signaling. File bytes stay on the
selected LAN TLS connection; this service never opens a P2P or sync channel.
"""
import hashlib,json,re,secrets,threading,time
from PySide6.QtCore import QObject,Signal

AUTHORIZATION_SECONDS=5.
TICKET_LIFETIME_SECONDS=180.
MAX_TICKETS=64
HEX32=re.compile('[a-f0-9]{32}')
HEX64=re.compile('[a-f0-9]{64}')


def manifest_digest(entries):
    return hashlib.sha256(json.dumps(entries,sort_keys=True).encode('utf8')).hexdigest()


class LanAccountAuthorizer(QObject):
    _prepare=Signal(object)

    def __init__(self,lan,wan):
        super().__init__(lan);self.lan=lan;self.wan=wan;self._lock=threading.RLock()
        self._identity=None;self._peers={};self._waiting={};self._tickets={}
        self._prepare.connect(self._send);wan.lan_authorization.connect(self._receive)
        wan.account_service.account_changed.connect(self._refresh);wan.peers_changed.connect(self._refresh)
        self._refresh();lan.account_authorizer=self

    def _session(self):
        account=self.wan.account_service
        return (account.account['id'],account.device_id,account.api.token) if account.signed_in else None

    def _active(self):
        return (not self.wan._closing and self.wan._connected and self._session() is not None
                and self.wan._bound_identity==self.wan._account_identity())

    def _signal(self,peer,value):
        return self.wan._send_signal(dict(type='lan_authorization',peer=peer,value=value))

    def _refresh(self,*_):
        identity=self._session();rows=self.wan.peers()
        with self._lock:
            if identity!=self._identity:
                for waiting in self._waiting.values():
                    waiting['error']='账号已改变，局域网发送已取消。';waiting['done'].set()
                self._waiting.clear();self._tickets.clear();self._identity=identity
            self._peers={row['lan_id']:dict(row) for row in rows
                         if row.get('account_device') is True and HEX32.fullmatch(str(row.get('lan_id','')))
                         and HEX64.fullmatch(str(row.get('id','')))}

    def prepare(self,transfer,peer,entries):
        with self._lock:
            target=self._peers.get(peer['id']);identity=self._identity
            if target is None:return
            waiting=dict(peer=target['id'],identity=identity,done=threading.Event(),error='',accepted=False,
                         payload=dict(type='lan-transfer',action='prepare',id=transfer.transfer_id,
                                      token=transfer.token,digest=manifest_digest(entries),fingerprint=peer['fingerprint']))
            self._waiting[transfer.transfer_id]=waiting
        try:
            self._prepare.emit(waiting);deadline=time.monotonic()+AUTHORIZATION_SECONDS
            while not waiting['done'].wait(.05):
                if transfer.cancel.is_set() or self.lan._stopped.is_set():raise InterruptedError('已取消局域网传输。')
                if time.monotonic()>=deadline:raise ConnectionError('同账号局域网接收授权超时，请检查对方连接。')
            if not waiting['accepted']:raise ConnectionError(waiting['error'] or '同账号局域网接收授权失败。')
        finally:
            with self._lock:self._waiting.pop(transfer.transfer_id,None)

    def _send(self,waiting):
        with self._lock:
            if waiting['identity']!=self._identity or not self._active():
                waiting['error']='账号连接已失效。';waiting['done'].set();return
        if not self._signal(waiting['peer'],waiting['payload']):
            waiting['error']='同账号身份验证服务尚未就绪，请稍后重试。';waiting['done'].set()

    def _receive(self,message):
        peer=message.get('peer');value=message.get('value')
        if not isinstance(value,dict) or not HEX64.fullmatch(str(peer)):return
        if value.get('type')!='lan-transfer' or not HEX32.fullmatch(str(value.get('id',''))):return
        now=time.monotonic()
        with self._lock:
            if not self._active() or self._identity!=self._session():return
            if value.get('action')=='accepted':
                waiting=self._waiting.get(value['id'])
                if waiting is not None and waiting['peer']==peer and waiting['identity']==self._identity:
                    waiting['accepted']=True;waiting['done'].set()
                return
            if (value.get('action')!='prepare' or not all(HEX64.fullmatch(str(value.get(key,''))) for key in ('token','digest','fingerprint'))
                    or not secrets.compare_digest(value['fingerprint'],self.lan.fingerprint)):return
            source=next((row for row in self._peers.values() if row['id']==peer),None)
            if source is None:return
            self._tickets={key:ticket for key,ticket in self._tickets.items() if now-ticket['at']<TICKET_LIFETIME_SECONDS}
            previous=self._tickets.get(value['id'])
            if previous is not None and any(previous[key]!=value[key] for key in ('token','digest')):return
            if previous is None and len(self._tickets)>=MAX_TICKETS:return
            self._tickets[value['id']]=dict(token=value['token'],digest=value['digest'],peer=peer,name=source['name'],at=now,identity=self._identity)
        self._signal(peer,dict(type='lan-transfer',action='accepted',id=value['id']))

    def incoming(self,transfer):
        with self._lock:
            ticket=self._tickets.get(transfer.transfer_id)
            if (ticket is None or not self._active() or ticket['identity']!=self._session()
                    or time.monotonic()-ticket['at']>=TICKET_LIFETIME_SECONDS
                    or not secrets.compare_digest(ticket['token'],transfer.token)
                    or not secrets.compare_digest(ticket['digest'],transfer.manifest_hash)):return None
            transfer.peer=ticket['name']
            return dict(account_device=True,account_id=ticket['identity'][0],peer=ticket['name'])
