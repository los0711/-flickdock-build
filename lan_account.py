"""Authorize unattended LAN reception over the existing authenticated mesh.

Only the short-lived capability travels over the mesh. File bytes always use
the selected LAN service's pinned TLS socket, independent of the mesh route.
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

    def __init__(self,lan,mesh):
        super().__init__(lan);self.lan=lan;self.mesh=mesh;self._lock=threading.RLock()
        self._identity=None;self._peers={};self._waiting={};self._tickets={}
        self._prepare.connect(self._send);mesh.control_received.connect(self._receive)
        mesh.account.account_changed.connect(self._refresh);mesh.wan.peers_changed.connect(self._refresh)
        self._refresh();lan.account_authorizer=self

    def _refresh(self,*_):
        identity=self.mesh._session();rows=self.mesh.wan.peers()
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
            if waiting['identity']!=self._identity or self.mesh.closed or not self.mesh._active():
                waiting['error']='账号连接已失效。';waiting['done'].set();return
        if not self.mesh.send_control(waiting['peer'],waiting['payload']):
            waiting['error']='同账号验证连接尚未就绪；局域网模式不会改用远程传输。';waiting['done'].set()

    def _receive(self,peer,value):
        if value.get('type')!='lan-transfer' or not HEX32.fullmatch(str(value.get('id',''))):return
        now=time.monotonic()
        with self._lock:
            if not self.mesh._active() or self._identity!=self.mesh._session():return
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
        self.mesh.send_control(peer,dict(type='lan-transfer',action='accepted',id=value['id']))

    def incoming(self,transfer):
        with self._lock:
            ticket=self._tickets.get(transfer.transfer_id)
            if (ticket is None or self.mesh.closed or ticket['identity']!=self.mesh._session()
                    or time.monotonic()-ticket['at']>=TICKET_LIFETIME_SECONDS
                    or not secrets.compare_digest(ticket['token'],transfer.token)
                    or not secrets.compare_digest(ticket['digest'],transfer.manifest_hash)):return None
            transfer.peer=ticket['name']
            return dict(account_device=True,account_id=ticket['identity'][0],peer=ticket['name'])
