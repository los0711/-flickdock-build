"""Prelaunch fixture through real local WAN signaling, never storage injection.

Run in a bounded subprocess before the frozen application starts. Only the
ephemeral loopback workerd is permitted. Two QWebSockets establish/accept one
zero-byte consent session, cancel it, and prove the recent peer is offline.
No WebRTC/ICE/relay/provider request is initiated. Secrets never reach stdout
or the small retained proof. No shipped application module is imported.
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

RECENT_NAME='Native QA recent offline'
PRIMARY_NAME='Frozen Windows QA'
SAFE_ERRORS=frozenset((
    'Unexpected signaling schema','Real local signaling rejected fixture operation',
    'Real signaling identity mismatch','Real signaling receiving code invalid',
    'Actual consent request did not match the zero-byte fixture',
    'Actual accepted session does not match fixture',
    'Unexpected cancellation before actual accepted session',
    'Recent fixture did not receive an actual peer list',
    'Accepted peer was not remembered uniquely by service',
    'Recent fixture is not the actual offline recent-only peer',
    'Peer disconnected before real cancellation','Primary disconnected before actual offline recent proof',
    'Actual local signaling send failed','Actual local socket error: primary','Actual local socket error: peer',
    'Actual local recent fixture exceeded 25 seconds',
))
MESSAGE_TYPES=frozenset(('identity','request','waiting','begin','cancelled','peers','custom_code','pong','error'))


def safe_failure(error):
    reason=str(error)
    return dict(failure_class=type(error).__name__,reason=reason if reason in SAFE_ERRORS else 'fixture callback failed')


def require_hosted():
    if (sys.platform!='win32' or os.environ.get('GITHUB_ACTIONS')!='true'
            or os.environ.get('RUNNER_OS')!='Windows'
            or os.environ.get('RUNNER_ENVIRONMENT')!='github-hosted'):
        raise RuntimeError('Recent fixture refuses non-ephemeral hosted Windows')


def validate_origin(origin):
    parsed=urlsplit(origin)
    if (parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or parsed.username or parsed.password
            or parsed.path not in ('','/') or parsed.query or parsed.fragment
            or parsed.port is None or not 1<=parsed.port<=65535):
        raise ValueError('Recent fixture requires exact loopback workerd origin')
    return 'ws://127.0.0.1:'+str(parsed.port)+'/v1/wan/connect'


def primary_credential(profile):
    profile=Path(profile).resolve()
    if (profile.name!='FlickDock' or not profile.parent.name.startswith('flickdock-frozen-normal-ci-')
            or not (profile/'settings.json').is_file() or not (profile/'shelves.json').is_file()):
        raise ValueError('Recent fixture requires its existing disposable prelaunch profile')
    path=profile/'wan-device.key'
    if path.exists():
        key=path.read_text(encoding='ascii').strip()
        if not re.fullmatch('[a-f0-9]{64}',key):raise ValueError('Existing disposable WAN credential invalid')
        return key
    key=secrets.token_hex(32)
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w',encoding='ascii') as stream:stream.write(key)
    return key


class SeedProtocol:
    """Testable exact protocol, emitting only real send/close instructions."""
    def __init__(self,primary_id,peer_id):
        self.ids={'primary':primary_id,'peer':peer_id};self.codes={}
        self.session=secrets.token_hex(16);self.token=secrets.token_hex(32)
        self.digest=hashlib.sha256(b'[]').hexdigest();self.requested=False
        self.accepted=False;self.begun=set();self.cancelled=set()
        self.peer_closed=False;self.complete=False;self.events=[]

    def record(self,stage,**values):
        self.events.append(dict(stage=stage,monotonic=time.monotonic(),**values))

    def connected(self,role):
        self.record('connected',role=role)
        return [('send',role,dict(type='hello',name=PRIMARY_NAME if role=='primary' else RECENT_NAME,platform='windows'))]

    def message(self,role,message):
        if message.get('schema_version')!=1:raise ValueError('Unexpected signaling schema')
        kind=message.get('type');self.record('received',role=role,type=kind if kind in MESSAGE_TYPES else 'unknown')
        if kind=='error':raise ValueError('Real local signaling rejected fixture operation')
        if kind=='identity':
            if message.get('device',{}).get('id')!=self.ids[role]:raise ValueError('Real signaling identity mismatch')
            code=message.get('code','')
            if not re.fullmatch('[23456789A-HJ-NP-Z]{8}',code):raise ValueError('Real signaling receiving code invalid')
            self.codes[role]=code
            if len(self.codes)==2 and not self.requested:
                self.requested=True;self.record('request-sent')
                return [('send','primary',dict(type='request',id=self.session,token=self.token,digest=self.digest,
                        code=self.codes['peer'],total='0',count=0,names=[]))]
        elif kind=='request':
            if (role!='peer' or not self.requested or self.accepted or message.get('id')!=self.session
                    or message.get('peer',{}).get('id')!=self.ids['primary'] or message.get('token')!=self.token
                    or message.get('digest')!=self.digest or message.get('total')!='0'
                    or message.get('count')!=0 or message.get('names')!=[]):
                raise ValueError('Actual consent request did not match the zero-byte fixture')
            self.accepted=True;self.record('receiver-confirmed')
            return [('send','peer',dict(type='accept',id=self.session))]
        elif kind=='begin':
            if (not self.accepted or message.get('id')!=self.session or message.get('generation')!=1
                    or message.get('initiator')!=self.ids['primary']):
                raise ValueError('Actual accepted session does not match fixture')
            self.begun.add(role)
            if self.begun=={'primary','peer'}:
                self.record('both-session-begin')
                return [('send','primary',dict(type='cancel',id=self.session))]
        elif kind=='cancelled':
            if message.get('id')!=self.session or self.begun!={'primary','peer'}:
                raise ValueError('Unexpected cancellation before actual accepted session')
            self.cancelled.add(role)
            if self.cancelled=={'primary','peer'}:return [('close','peer',None)]
        elif kind=='peers' and role=='primary' and self.peer_closed:
            rows=message.get('peers')
            if not isinstance(rows,list):raise ValueError('Recent fixture did not receive an actual peer list')
            matches=[row for row in rows if row.get('id')==self.ids['peer']]
            if len(matches)!=1:raise ValueError('Accepted peer was not remembered uniquely by service')
            peer=matches[0]
            if peer.get('name')!=RECENT_NAME or peer.get('online') is not False or peer.get('account_device'):
                raise ValueError('Recent fixture is not the actual offline recent-only peer')
            self.complete=True;self.record('actual-recent-offline',name=RECENT_NAME,online=False,account_device=False)
            return [('close','primary',None)]
        return []

    def disconnected(self,role):
        self.record('disconnected',role=role)
        if role=='peer':
            if self.cancelled!={'primary','peer'}:raise ValueError('Peer disconnected before real cancellation')
            self.peer_closed=True
            return [('send','primary',dict(type='recent'))]
        if not self.complete:raise ValueError('Primary disconnected before actual offline recent proof')
        return [('done','primary',None)]

    def proof(self):
        if not self.complete:raise ValueError('Recent fixture not actually complete')
        return dict(ok=True,real_local_signaling=True,public_cloud=False,provider_requests=False,ice_started=False,
                    payload_bytes=0,consent_confirmed=True,recent_name=RECENT_NAME,online=False,account_device=False,
                    events=self.events)

    def failure_proof(self,error):
        return dict(ok=False,real_local_signaling=True,public_cloud=False,provider_requests=False,ice_started=False,
                    stage=self.events[-1]['stage'] if self.events else 'created',**safe_failure(error),events=self.events[-128:])


def seed(origin,profile,proof_path):
    require_hosted();url=validate_origin(origin);primary=primary_credential(profile);peer=secrets.token_hex(32)
    target=Path(proof_path).resolve();base=Path(profile).resolve().parent
    if target.parent!=base:raise ValueError('Fixture proof must stay in its disposable root')
    from PySide6.QtCore import QCoreApplication,QTimer,QUrl
    from PySide6.QtNetwork import QNetworkRequest,QNetworkProxy
    from PySide6.QtWebSockets import QWebSocket
    app=QCoreApplication([])
    keys={'primary':primary,'peer':peer}
    protocol=SeedProtocol(*(hashlib.sha256(keys[role].encode()).hexdigest() for role in ('primary','peer')))
    sockets={role:QWebSocket() for role in keys};failure=[];done=[]
    def fail(error):
        if not failure:
            failure.append(error)
            target.write_text(json.dumps(protocol.failure_proof(error),indent=2),encoding='utf8')
        app.exit(1)
    def apply(actions):
        for action,role,value in actions:
            if action=='send':
                if sockets[role].sendTextMessage(json.dumps(value,separators=(',',':')))<=0:
                    raise RuntimeError('Actual local signaling send failed')
            elif action=='close':sockets[role].close()
            elif action=='done':done.append(True);app.quit()
            else:raise ValueError('Unknown fixture protocol instruction')
    def callback(action):
        def called(*args):
            try:apply(action(*args))
            except Exception as error:fail(error)
        return called
    for role,socket in sockets.items():
        socket.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
        socket.connected.connect(callback(lambda role=role:protocol.connected(role)))
        socket.textMessageReceived.connect(callback(lambda text,role=role:protocol.message(role,json.loads(text))))
        socket.disconnected.connect(callback(lambda role=role:protocol.disconnected(role)))
        socket.errorOccurred.connect(lambda *_,role=role:fail(RuntimeError('Actual local socket error: '+role)))
        request=QNetworkRequest(QUrl(url));request.setRawHeader(b'Authorization',('Bearer '+keys[role]).encode())
        socket.open(request)
    deadline=QTimer();deadline.setSingleShot(True);deadline.timeout.connect(lambda:fail(TimeoutError('Actual local recent fixture exceeded 25 seconds')))
    deadline.start(25000)
    try:
        status=app.exec()
        if status or failure or not done:
            error=failure[0] if failure else RuntimeError('Actual local signaling send failed')
            target.write_text(json.dumps(protocol.failure_proof(error),indent=2),encoding='utf8')
            raise RuntimeError('Actual local recent fixture failed; bounded progress proof retained')
        proof=protocol.proof()
        target.write_text(json.dumps(proof,indent=2),encoding='utf8')
        print(json.dumps(dict(ok=True,recent_name=RECENT_NAME,online=False,consent_confirmed=True,payload_bytes=0)),flush=True)
    finally:
        deadline.stop()
        for socket in sockets.values():socket.blockSignals(True);socket.abort()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--origin',required=True);parser.add_argument('--profile',type=Path,required=True)
    parser.add_argument('--proof',type=Path,required=True);args=parser.parse_args()
    seed(args.origin,args.profile,args.proof)


if __name__=='__main__':main()
