"""Account-verified consent tickets for manual LAN transfers.

Only bounded authorization metadata uses signaling. File bytes stay on the
selected LAN TLS connection; this service never opens a P2P or sync channel.
"""
import hashlib,json,re,secrets,threading,time
from PySide6.QtCore import QObject,Signal

AUTHORIZATION_SECONDS=5.
TICKET_LIFETIME_SECONDS=180.
KNOWN_PEER_SECONDS=900.
MAX_TICKETS=64
HEX32=re.compile('[a-f0-9]{32}')
HEX64=re.compile('[a-f0-9]{64}')


def manifest_digest(entries, shelf_intake=None):
    payload=entries if shelf_intake is None else {'entries':entries,'shelf_intake':shelf_intake}
    return hashlib.sha256(json.dumps(payload,sort_keys=True).encode('utf8')).hexdigest()


class LanAccountAuthorizer(QObject):
    _prepare=Signal(object)

    def __init__(self,lan,wan):
        super().__init__(lan);self.lan=lan;self.wan=wan;self._lock=threading.RLock()
        self._peer_updates=threading.Condition(self._lock)
        self._identity=None;self._peers={};self._waiting={};self._tickets={}
        self._known_peers={}
        self._deferred={}
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
        ready=[]
        with self._lock:
            if identity!=self._identity:
                for waiting in self._waiting.values():
                    waiting['error']='账号已改变，局域网发送已取消。';waiting['done'].set()
                self._waiting.clear();self._tickets.clear();self._deferred.clear();self._identity=identity
                self._known_peers.clear()
            self._peers={row['lan_id']:dict(row) for row in rows
                         if row.get('account_device') is True and HEX32.fullmatch(str(row.get('lan_id','')))
                         and HEX64.fullmatch(str(row.get('id','')))}
            if self._active():
                # Previous proof is only used to require a fresh ticket when
                # reconnecting. It never authorizes a transfer by itself.
                now=time.monotonic()
                current={key:expiry for key,expiry in self._known_peers.items() if expiry>now}
                current.update(dict.fromkeys(self._peers,now+KNOWN_PEER_SECONDS))
                self._known_peers=dict(sorted(current.items(),key=lambda item:item[1])[-256:])
            self._peer_updates.notify_all()
            if not self.wan._connected or self.wan._closing:
                self._deferred.clear()
                return
            bound=self._active()
            peers={row['id'] for row in self._peers.values()}
            now=time.monotonic()
            for key,(deadline,value) in tuple(self._deferred.items()):
                peer,_=key
                if deadline<=now:
                    del self._deferred[key]
                elif bound and peer in peers:
                    del self._deferred[key]
                    ready.append(dict(peer=peer,value=value))
        for message in ready:
            self._receive(message)

    def known_peer(self,lan_id):
        with self._lock:
            return (self._identity is not None and self._identity==self._session()
                    and (self._known_peers.get(lan_id,0)>time.monotonic()
                         or self._active() and lan_id in self._peers))

    def prepare(self,transfer,peer,entries):
        deadline=time.monotonic()+AUTHORIZATION_SECONDS
        with self._peer_updates:
            identity=self._identity
            while True:
                target=self._peers.get(peer['id'])
                if target is not None and self._active():break
                if not transfer.account_only:return False
                if transfer.cancel.is_set() or self.lan._stopped.is_set():raise InterruptedError('已取消局域网传输。')
                if identity is None or identity!=self._session():return False
                remaining=deadline-time.monotonic()
                if remaining<=0:return False
                self._peer_updates.wait(min(.05,remaining))
            if transfer.shelf_intake is None:transfer.account_only=True
            waiting=dict(peer=target['id'],identity=identity,done=threading.Event(),error='',accepted=False,
                         payload=dict(type='lan-transfer',action='prepare',id=transfer.transfer_id,
                                      token=transfer.token,digest=manifest_digest(entries,transfer.shelf_intake),
                                      fingerprint=peer['fingerprint']))
            self._waiting[transfer.transfer_id]=waiting
        try:
            self._prepare.emit(waiting)
            while not waiting['done'].wait(.05):
                if transfer.cancel.is_set() or self.lan._stopped.is_set():raise InterruptedError('已取消局域网传输。')
                if time.monotonic()>=deadline:return False
            if waiting['identity']!=self._session():raise ConnectionError('账号已改变，局域网发送已取消。')
            return waiting['accepted']
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
            if (self._identity is None or self._identity!=self._session() or
                    self.wan._closing or not self.wan._connected):return
            bound=self._active()
            if value.get('action')=='accepted':
                if not bound:return
                waiting=self._waiting.get(value['id'])
                if waiting is not None and waiting['peer']==peer and waiting['identity']==self._identity:
                    waiting['accepted']=True;waiting['done'].set()
                return
            if (value.get('action')!='prepare' or not all(isinstance(value.get(key),str) and HEX64.fullmatch(value[key]) for key in ('token','digest','fingerprint'))
                    or not secrets.compare_digest(value['fingerprint'],self.lan.fingerprint)):return
            source=next((row for row in self._peers.values() if row['id']==peer),None)
            if not bound or source is None:
                # The server can relay consent before this client's bind reply
                # or peer list. Keep it briefly, scoped to this local session.
                self._deferred={key:entry for key,entry in self._deferred.items() if entry[0]>now}
                if len(self._deferred)<MAX_TICKETS:
                    self._deferred.setdefault((peer,value['id']),(now+AUTHORIZATION_SECONDS,dict(value)))
                return
            self._tickets={key:ticket for key,ticket in self._tickets.items() if now-ticket['at']<TICKET_LIFETIME_SECONDS}
            previous=self._tickets.get(value['id'])
            if previous is not None and any(previous[key]!=value[key] for key in ('token','digest')):return
            if previous is None and len(self._tickets)>=MAX_TICKETS:return
            self._tickets[value['id']]=dict(token=value['token'],digest=value['digest'],peer=peer,name=source['name'],at=now,identity=self._identity)
        self._signal(peer,dict(type='lan-transfer',action='accepted',id=value['id']))

    def incoming(self,transfer):
        with self._lock:
            ticket=self._tickets.get(transfer.transfer_id)
            if (ticket is None or ticket['identity']!=self._session()
                    or time.monotonic()-ticket['at']>=TICKET_LIFETIME_SECONDS
                    or not secrets.compare_digest(ticket['token'],transfer.token)
                    or not secrets.compare_digest(ticket['digest'],transfer.manifest_hash)):return None
            transfer.peer=ticket['name']
            return dict(account_device=True,account_id=ticket['identity'][0],peer=ticket['name'],peer_id=ticket['peer'])
