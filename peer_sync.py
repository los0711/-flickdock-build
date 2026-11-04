"""Authenticated account mesh, LAN-first objects and independent cloud backup."""
from __future__ import annotations
from collections import OrderedDict,deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass,field
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

from PySide6.QtCore import QObject,QTimer,Signal
from nearby_transport import NearbyStream,try_nearby
from peer_rpc import PeerRpc,PeerUnavailable,RPC_BLOCK_BYTES
from peer_stream import PeerBroker
from selection_archive import _source_reader

CONNECTION_TIMEOUT_SECONDS=30
DIRECT_CONNECT_GRACE_SECONDS=12
AUTH_LEASE_SECONDS=65
AUTH_REFRESH_SECONDS=20
RETRY_MAX_SECONDS=60
OBJECT_MAX_BYTES=1024**3
NOTIFY_BATCH=32
MAX_BACKGROUND_WORK=32
SOURCE_REGISTRY_LIMIT=4096
FETCH_PIPELINE_REQUESTS=4
LOG=logging.getLogger(__name__)
UDP_RELAY_GRACE_SECONDS=8
PENDING_SIGNAL_LIMIT=128
PENDING_SIGNAL_BYTES=1024*1024
PENDING_SIGNAL_TTL_SECONDS=15.


@dataclass
class PeerFetch:
    identifier:str=field(default_factory=lambda:uuid4().hex)
    cancel:threading.Event=field(default_factory=threading.Event)
    epoch:int=-1
    route:str=''
    sha256:str=''


def valid_object(record):
    return (isinstance(record,dict) and isinstance(record.get('id'),str) and 1<=len(record['id'])<=128
            and type(record.get('size')) is int and 0<=record['size']<=OBJECT_MAX_BYTES
            and isinstance(record.get('sha256'),str) and re.fullmatch('[a-f0-9]{64}',record['sha256']) is not None)


class PeerSync(QObject):
    offered=Signal(str,object,str)
    connected=Signal(str)
    applied=Signal(str,object,str)
    status_changed=Signal(str)
    connections_changed=Signal(object)
    _native=Signal(object)
    _ready=Signal(object)
    _notification=Signal(object)
    _finished=Signal(object)
    _progress=Signal(object)

    def __init__(self,account,wan,parent=None,*,can_upload=lambda _:True,can_receive=lambda _:True,broker_factory=PeerBroker,qa_force_relay=False):
        super().__init__(parent)
        self.account=account;self.wan=wan;self.can_upload=can_upload;self.can_receive=can_receive
        self.broker_factory=broker_factory;self.broker=None;self.closed=False;self.epoch=0;self.identity=None
        self._qa_force_relay=bool(qa_force_relay and os.environ.get('QT_QPA_PLATFORM')=='offscreen')
        self._states={};self._retry={};self._objects={};self._offers={};self._sources=OrderedDict();self._outbox=OrderedDict();self._fetches={}
        self._connection_view={}
        self._prefer_tls={};self._binding_epoch=None
        self._pending_signals=deque();self._pending_signal_bytes=0
        self._registry_lock=threading.RLock();self._source_lock=threading.Lock()
        self._slots=threading.BoundedSemaphore(MAX_BACKGROUND_WORK)
        self._pool=ThreadPoolExecutor(max_workers=8,thread_name_prefix='FlickDock-peer-work')
        self._io_pool=ThreadPoolExecutor(max_workers=8,thread_name_prefix='FlickDock-peer-fetch')
        self._native.connect(self._handle_native);self._ready.connect(self._handle_ready)
        self._notification.connect(self._handle_notification);self._finished.connect(self._handle_finished)
        self._progress.connect(self._handle_progress)
        self.timer=QTimer(self);self.timer.setInterval(1000);self.timer.timeout.connect(self._tick)
        self.flush=QTimer(self);self.flush.setSingleShot(True);self.flush.setInterval(16);self.flush.timeout.connect(self._flush)
        account.account_changed.connect(self._account_changed);wan.peers_changed.connect(self._reconcile)
        wan.sync_signal.connect(self._signal);self._account_changed()

    def _session(self):
        if not self.account.signed_in:return None
        account=self.account.account
        if not isinstance(account,dict) or not isinstance(account.get('id'),str):return None
        return account['id'],self.account.device_id,self.account.api.token

    def _active(self):
        return not self.closed and self.identity is not None and self.identity==self._session()

    def _signaling_ready(self):
        return (not self.closed and self.identity is not None and self.identity==self._session()
                and self.wan._connected and self.wan._bound_identity==self.identity[1:])

    def _account_changed(self,*_):
        identity=self._session()
        if identity==self.identity:return
        self.epoch+=1;self.identity=identity
        self._connection_view={};self.connections_changed.emit({})
        self._pending_signals.clear();self._pending_signal_bytes=0
        for peer in list(self._states):self._drop(peer,retry=False)
        for job,_,_ in self._fetches.values():job.cancel.set()
        with self._registry_lock:self._objects.clear();self._offers.clear();self._sources.clear()
        self._outbox.clear();self._retry.clear();self._prefer_tls.clear();self._binding_epoch=None
        if self.broker is not None:self.broker.close();self.broker=None
        if identity is None:self.timer.stop();self.flush.stop()
        else:self.timer.start();self._reconcile()

    def _submit(self,action,*,io=False):
        if self.closed or not self._slots.acquire(blocking=False):return False
        def run():
            try:action()
            finally:self._slots.release()
        future=(self._io_pool if io else self._pool).submit(run)
        def completed(result):
            if result.cancelled():return
            error=result.exception()
            if error is not None:self._finished.emit(dict(internal_error=repr(error)))
        future.add_done_callback(completed);return True

    def _reconcile(self,*_):
        if not self._active():
            for peer in list(self._states):self._drop(peer,retry=False)
            return
        if not self._signaling_ready():return
        pending=list(self._pending_signals);self._pending_signals.clear();self._pending_signal_bytes=0
        for epoch,binding,when,value in pending:
            if epoch==self.epoch and binding==getattr(self.wan,'_bind_epoch',0) and time.monotonic()-when<PENDING_SIGNAL_TTL_SECONDS:self._signal(value)
        peers={row['id']:row for row in self.wan.peers() if row.get('account_device') and row.get('online') and 'mesh-v1' in row.get('features',[])
               and isinstance(row.get('id'),str) and re.fullmatch('[a-f0-9]{64}',row['id'])}
        binding_epoch=getattr(self.wan,'_bind_epoch',0)
        if self._binding_epoch!=binding_epoch:
            self._binding_epoch=binding_epoch
            for peer,state in self._states.items():
                if state['rpc'] is not None and self.wan.device_id<peer:
                    self.wan._send_signal(dict(type='sync_open',id=state['id'],peer=peer))
        now=time.monotonic()
        for peer in peers:
            if peer in self._states or self.wan.device_id>=peer or self._retry.get(peer,(0,0))[0]>now:continue
            if self._qa_force_relay and not getattr(self.wan,'_sync_ice',[]):continue
            identifier=uuid4().hex
            self._states[peer]=dict(id=identifier,peer=peer,epoch=self.epoch,started=now,leased=now,last_lease=now,
                                    cancel=threading.Event(),raw=None,rpc=None,route='connecting')
            self.wan._send_signal(dict(type='sync_open',id=identifier,peer=peer))

    def _drop(self,peer,*,retry=True):
        state=self._states.pop(peer,None)
        if state is None:return
        state['cancel'].set()
        if state.get('adaptive') is not None:state['adaptive'].shutdown()
        if state['rpc'] is not None:state['rpc'].close()
        elif state['raw'] is not None:state['raw'].shutdown()
        if self.wan._connected:self.wan._send_signal(dict(type='sync_close',id=state['id']))
        if retry:
            attempts=min(6,self._retry.get(peer,(0,0))[1]+1)
            self._retry[peer]=(time.monotonic()+min(RETRY_MAX_SECONDS,2**attempts),attempts)
        for key in list(self._outbox):
            if key[0]==peer:self._outbox.pop(key,None)
        for sources in self._sources.values():sources.discard(peer)

    def _tick(self):
        self._reconcile();now=time.monotonic()
        self._prune_registry()
        for peer,state in list(self._states.items()):
            if (state['rpc'] is None and not state.get('upgrading') and state.get('initiator') and not self._qa_force_relay
                    and self._signaling_ready() and now-state['started']>=3.):self._start_cloud(state)
            if now-state['leased']>=AUTH_LEASE_SECONDS or state['rpc'] is None and now-state['started']>=CONNECTION_TIMEOUT_SECONDS:
                self._drop(peer);continue
            if state['rpc'] is not None and (state['rpc'].closed.is_set() or not self._authorized(state)):
                self._drop(peer);continue
            if (state['rpc'] is None and not state.get('cloud_starting') and not state.get('relay_configured') and self._signaling_ready()
                    and getattr(self.wan,'_sync_ice',[]) and now-state['started']>=DIRECT_CONNECT_GRACE_SECONDS
                    and self.wan.device_id<peer):
                self._drop(peer,retry=False);continue
            if (state['rpc'] is None and not state.get('cloud_starting') and state.get('relay_configured') and state.get('relay_transport')=='udp'
                    and now-state['started']>=UDP_RELAY_GRACE_SECONDS and self.wan.device_id<peer):
                self._prefer_tls[peer]=True;self._drop(peer,retry=False);continue
            if now-state['last_lease']>=AUTH_REFRESH_SECONDS:
                if self._signaling_ready():state['last_lease']=now;self.wan._send_signal(dict(type='sync_lease',id=state['id']))
        snapshot=self.connection_snapshot()
        if snapshot!=self._connection_view:
            self._connection_view=snapshot;self.connections_changed.emit(snapshot)

    def connection_snapshot(self):
        if not self._active():return {}
        result={}
        for row in self.wan.peers():
            if not row.get('account_device'):continue
            identity=row.get('account_device_id')
            if not isinstance(identity,str) or not identity:continue
            info=dict(route='disconnected',latency_ms=None)
            state=self._states.get(row['id'])
            if row.get('online'):
                info['route']='cloud' if 'mesh-v1' not in row.get('features',[]) else 'connecting'
                if state is not None and state['rpc'] is not None and self._authorized(state) and not state['rpc'].closed.is_set():
                    info=state['rpc'].stream.connection_info()
            result[identity]=info
        return result

    def route_for_peer(self,peer):
        state=self._states.get(peer)
        if state is None or state['rpc'] is None or not self._authorized(state):return ''
        return state['rpc'].stream.route

    def _authorized(self,state):
        return (self._active() and state['epoch']==self.epoch and self._states.get(state['peer']) is state
                and not state['cancel'].is_set() and time.monotonic()-state['leased']<AUTH_LEASE_SECONDS)

    def _signal(self,value):
        if not isinstance(value,dict):return
        if not self._signaling_ready():
            if self._active() and self.wan._connected:
                size=len(json.dumps(value,separators=(',',':')).encode('utf8'))
                if len(self._pending_signals)<PENDING_SIGNAL_LIMIT and self._pending_signal_bytes+size<=PENDING_SIGNAL_BYTES:
                    self._pending_signals.append((self.epoch,getattr(self.wan,'_bind_epoch',0),time.monotonic(),dict(value)))
                    self._pending_signal_bytes+=size
            return
        identifier=value.get('id');kind=value.get('type')
        if not isinstance(identifier,str) or not re.fullmatch('[a-f0-9]{32}',identifier):return
        if kind=='sync_begin':
            remote=value.get('peer')
            if not isinstance(remote,dict):return
            peer=remote.get('id')
            # Presence can arrive before or after the bind HTTP response. The
            # server's fresh account/session authorization is authoritative.
            if (value.get('account')!=self.identity[0] or not isinstance(peer,str) or not re.fullmatch('[a-f0-9]{64}',peer)
                    or peer==self.wan.device_id or type(value.get('initiator')) is not bool):return
            old=self._states.get(peer)
            if old is not None and old['id']!=identifier:self._drop(peer,retry=False)
            state=self._states.get(peer)
            if state is None:
                now=time.monotonic();state=dict(id=identifier,peer=peer,epoch=self.epoch,started=now,leased=now,last_lease=now,
                    cancel=threading.Event(),raw=None,rpc=None,route='connecting');self._states[peer]=state
            state['leased']=time.monotonic()
            if state['raw'] is not None:return
            state['initiator']=value['initiator']
            try:
                if self.broker is None or self.broker.closed:self.broker=self.broker_factory()
                raw=self.broker.stream(identifier,lambda event:self._native.emit(dict(state=state,event=event)))
                state['raw']=raw
                servers=[]
                for server in value.get('iceServers',[]):
                    if not isinstance(server,dict):continue
                    urls=server.get('urls',[])
                    if isinstance(urls,list):servers.extend(url for url in urls if url in ('stun:stun.cloudflare.com:3478',))
                from wan_transfer import _turn_http_proxy
                servers.extend(getattr(self.wan,'_sync_ice',[]));state['relay_configured']=any(isinstance(row,dict) for row in servers)
                proxies={str(row['port']):_turn_http_proxy(row['port']) for row in servers if isinstance(row,dict) and row['relayType']=='TurnTls'}
                raw.control(dict(type='init',iceServers=servers,initiator=state['initiator'],
                    preferTls=self._prefer_tls.get(peer,any(proxies.values())),proxies=proxies,forceRelay=self._qa_force_relay))
            except (OSError,ValueError,RuntimeError) as error:
                self.status_changed.emit('同账号直连未就绪：'+str(error)[:160]);self._drop(peer)
            return
        state=next((state for state in self._states.values() if state['id']==identifier),None)
        if state is None:return
        if kind=='sync_signal' and state['raw'] is not None:
            payload=value.get('payload')
            if isinstance(payload,dict) and payload.get('type')=='route':
                if state.get('adaptive') is not None:state['adaptive']._secondary_signal(payload.get('value'))
                return
            if isinstance(payload,dict) and payload.get('type')=='cloud':
                if state.get('initiator') or not re.fullmatch('[a-f0-9]{32}',str(payload.get('id',''))) or not re.fullmatch('[a-f0-9]{64}',str(payload.get('key',''))):return
                api=getattr(self.wan,'cloud_routes',None)
                if api is not None:
                    configured=api.open(state['peer'],dict(id=payload['id'],key=payload['key']),state['cancel'],False)
                    if configured is not None:self._attach_cloud(state,configured[0])
                return
            if not isinstance(payload,dict) or payload.get('type') not in ('description','candidate'):return
            try:state['raw'].control(payload)
            except OSError:self._drop(state['peer'])
        elif kind=='sync_lease':state['leased']=time.monotonic()
        elif kind in ('sync_closed','sync_error'):self._drop(state['peer'])

    def _handle_native(self,value):
        state=value['state'];event=value['event']
        if not self._authorized(state):
            if event.get('type')=='cloud_ready':event['stream'].shutdown()
            return
        kind=event.get('type')
        if kind=='cloud_ready':
            descriptor=event['descriptor']
            self.wan._send_signal(dict(type='sync_signal',id=state['id'],payload=dict(type='cloud',**descriptor)))
            self._attach_cloud(state,event['stream'])
        elif kind=='route_signal':
            if not self.wan._send_signal(dict(type='sync_signal',id=state['id'],payload=dict(type='route',value=event['value']))):
                self._submit(lambda:state['adaptive'].send_inband(event['value']),io=True)
        elif kind in ('description','candidate'):
            self.wan._send_signal(dict(type='sync_signal',id=state['id'],payload=event))
        elif kind=='open':
            if state.get('adaptive') is not None:
                if not state.get('native_attaching'):
                    state['native_attaching']=True
                    self._submit(lambda:state['adaptive'].attach_native(state['raw'],'send' if state['initiator'] else 'receive'),io=True)
                return
            if state.get('upgrading') or state['rpc'] is not None:return
            state['upgrading']=True;state['wire_route']=event.get('route','direct')
            def upgrade():
                try:
                    raw=state['raw']
                    nearby=raw if self._qa_force_relay else try_nearby(raw,'send' if state['initiator'] else 'receive',state['cancel'])
                    self._native.emit(dict(state=state,event=dict(type='native_ready',nearby=nearby)))
                except (OSError,ValueError):self._native.emit(dict(state=state,event=dict(type='closed')))
            if not self._submit(upgrade,io=True):self._drop(state['peer'])
        elif kind=='native_ready':
            nearby=event['nearby']
            if state.get('adaptive') is not None:state['adaptive'].attach_prepared(state['raw'],nearby)
            else:self._handle_ready(dict(state=state,stream=self._adaptive(state,nearby)))
        elif kind=='route':state['route']=event['route']
        elif kind=='path' and state['rpc'] is not None and 'adaptive' not in state:
            state['route']='relay' if event.get('route')=='relay' else 'p2p'
        elif kind=='transport':state['relay_transport']=event.get('relayTransport','direct')
        elif kind in ('closed','error') or kind=='state' and event.get('state') in ('failed','closed'):
            if state.get('adaptive') is not None and not state['adaptive'].closed:return
            if state.get('cloud_starting'):return
            if state['rpc'] is None and not self._qa_force_relay and getattr(self.wan,'cloud_routes',None) is not None:
                if state.get('initiator'):self._start_cloud(state)
                return
            if state['rpc'] is None and state.get('relay_configured'):
                self._prefer_tls[state['peer']]=state.get('relay_transport')!='tls'
            if kind=='error':LOG.warning('Native account peer error: %s',event.get('message',''))
            self._drop(state['peer'])

    def _adaptive(self,state,nearby,*,primary=True):
        from adaptive_stream import AdaptiveStream
        raw=state['raw']
        def alternative(event,configuration):
            if self._qa_force_relay:configuration['forceRelay']=True
            servers=list(getattr(self.wan,'_sync_ice',[]))
            if 'stun:stun.cloudflare.com:3478' not in servers:servers.insert(0,'stun:stun.cloudflare.com:3478')
            if configuration['forceRelay'] and not any(isinstance(row,dict) for row in servers):return None
            from wan_transfer import _turn_http_proxy
            proxies={str(row['port']):_turn_http_proxy(row['port']) for row in servers if isinstance(row,dict) and row['relayType']=='TurnTls'}
            configuration.update(iceServers=servers,proxies=proxies)
            return self.broker.stream(uuid4().hex,event),configuration
        def cloud(descriptor):
            api=getattr(self.wan,'cloud_routes',None)
            return api.open(state['peer'],descriptor,state['cancel'],state['initiator']) if api is not None else None
        def signal(value):
            if not self._signaling_ready():return False
            self._native.emit(dict(state=state,event=dict(type='route_signal',value=value)));return True
        stream=AdaptiveStream(raw,nearby,initiator=state['initiator'],cancel=state['cancel'],primary=primary,
            authorized=lambda:self._authorized(state),secondary=alternative,prefer_tls=state.get('relay_transport')!='tls',signal_out=signal,
            cloud=None if self._qa_force_relay else cloud,
            event=lambda event:self._native.emit(dict(state=state,event=event)))
        state['adaptive']=stream;return stream

    def _start_cloud(self,state):
        api=getattr(self.wan,'cloud_routes',None)
        if (api is None or state.get('cloud_starting') or state.get('cloud_attempts',0)>=3
                or time.monotonic()<state.get('cloud_retry_at',0)):return
        state['cloud_starting']=True;state['cloud_attempts']=state.get('cloud_attempts',0)+1
        def prepare():
            try:
                configured=api.open(state['peer'],None,state['cancel'],True)
                if configured is not None:
                    stream,descriptor=configured
                    self._native.emit(dict(state=state,event=dict(type='cloud_ready',stream=stream,descriptor=descriptor)))
            except (OSError,ValueError) as error:
                state['cloud_error']=str(error);LOG.info('Cloud bootstrap unavailable',exc_info=True)
            finally:state['cloud_starting']=False;state['cloud_retry_at']=time.monotonic()+2**state['cloud_attempts']
        if not self._submit(prepare,io=True):state['cloud_starting']=False;state['cloud_attempts']-=1

    def _attach_cloud(self,state,stream):
        adaptive=state.get('adaptive')
        if adaptive is None:
            adaptive=self._adaptive(state,state['raw'],primary=False)
            adaptive._add('cloud','cloud',stream);self._handle_ready(dict(state=state,stream=adaptive))
        else:
            with adaptive._condition:old=adaptive._paths.pop('cloud',None)
            if old is not None:old.stream.shutdown()
            adaptive._add('cloud','cloud',stream)

    def _handle_ready(self,value):
        state=value['state'];stream=value['stream']
        if not self._authorized(state):stream.shutdown();return
        state['route']=stream.route
        state['rpc']=PeerRpc(stream,self._provide,lambda value:self._notification.emit(dict(state=state,value=value)),
                            self._submit,lambda:self._authorized(state))
        self._retry.pop(state['peer'],None)
        with self._registry_lock:offers=list(self._offers.values())
        for topic,record,valid in offers:
            if valid() and self.can_upload(topic):self._queue_offer(state['peer'],topic,record)
        self.connected.emit(state['peer'])

    def _handle_notification(self,event):
        state=event['state'];value=event['value']
        if not self._authorized(state):return
        topic=value.get('topic');record=value.get('record')
        if topic not in ('shelf','clipboard'):return
        if value.get('kind')=='inventory':
            for offered_topic,offered,valid in tuple(self._offers.values()):
                if topic==offered_topic and valid() and self.can_upload(topic):self._queue_offer(state['peer'],topic,offered)
            return
        if not valid_object(record):return
        if value.get('kind')=='applied':
            with self._registry_lock:offered=self._offers.get((topic,record['id']))
            if offered is not None and offered[1]['sha256']==record['sha256'] and offered[2]():
                route=value.get('delivery_route')
                self.applied.emit(topic,dict(record,delivery_route=route if route in ('lan','p2p','relay','cloud') else ''),state['peer'])
            return
        if value.get('kind')!='offer' or not self.can_receive(topic):return
        self._sources.setdefault((topic,record['sha256']),set()).add(state['peer'])
        self._sources.move_to_end((topic,record['sha256']))
        while len(self._sources)>SOURCE_REGISTRY_LIMIT:self._sources.popitem(last=False)
        self.offered.emit(topic,dict(record,delivery_route=state['rpc'].stream.received_route),state['peer'])

    def refresh(self):
        for state in self.available():
            for topic in ('shelf','clipboard'):
                if self.can_receive(topic):
                    try:state['rpc'].send_notify(dict(kind='inventory',topic=topic),priority=0)
                    except OSError:self._drop(state['peer']);break

    def _queue_offer(self,peer,topic,record):
        self._outbox[peer,topic,record['id']]=dict(kind='offer',topic=topic,record=record)
        if not self.flush.isActive():self.flush.start()

    def _flush(self):
        for _ in range(min(NOTIFY_BATCH,len(self._outbox))):
            key,value=self._outbox.popitem(last=False);state=self._states.get(key[0])
            if not self.can_upload(key[1]):continue
            current=self._offers.get((key[1],key[2]))
            if current is None or current[1]!=value['record'] or not current[2]():continue
            if state is None or state['rpc'] is None:continue
            try:sent=state['rpc'].send_notify(value,priority=0 if key[1]=='clipboard' else 1)
            except OSError:self._drop(key[0]);continue
            if not sent:self._outbox[key]=value;break
        if self._outbox:self.flush.start()

    def publish(self,topic,record,source,*,valid=lambda:True):
        if self.closed or self.identity is None or self.identity!=self._session() or not self.can_upload(topic) or not valid_object(record):return False
        record=dict(record)
        if len(json.dumps(record,ensure_ascii=False).encode('utf8'))>60000:raise ValueError('Peer object metadata exceeds limit')
        if not isinstance(source,(bytes,str,Path)):raise TypeError('Peer object source must be a path or bytes')
        with self._registry_lock:
            previous=self._offers.get((topic,record['id']))
            if topic=='clipboard':
                for key in [key for key in self._offers if key[0]=='clipboard']:
                    self._remove_offer(key)
            old=self._offers.get((topic,record['id']))
            if old is not None and old[1]['sha256']!=record['sha256']:
                self._remove_offer((topic,record['id']))
            self._objects.setdefault((topic,record['sha256']),{})[record['id']]=(source,record['size'],valid)
            self._offers[topic,record['id']]=(topic,record,valid)
        if previous is not None and previous[1]==record:return True
        for peer,state in self._states.items():
            if state['rpc'] is not None:self._queue_offer(peer,topic,record)
        return True

    def _remove_offer(self,key):
        old=self._offers.pop(key)
        objects=self._objects.get((key[0],old[1]['sha256']))
        if objects is not None:
            objects.pop(key[1],None)
            if not objects:self._objects.pop((key[0],old[1]['sha256']),None)

    def _prune_registry(self):
        with self._registry_lock:
            for key,(_,record,valid) in tuple(self._offers.items()):
                if not valid():self._remove_offer(key)

    def confirm(self,topic,record):
        """Promote an already verified body without reading or sending it again."""
        with self._registry_lock:
            old=self._offers.get((topic,record['id']))
            if old is None or old[1]['sha256']!=record.get('sha256'):return False
            source,size,valid=self._objects[topic,record['sha256']][record['id']]
        return self.publish(topic,dict(old[1],**record,provisional=False),source,valid=valid)

    def acknowledge(self,peer,topic,record):
        state=self._states.get(peer)
        if state is None or state['rpc'] is None or not self._authorized(state):return
        summary={key:record[key] for key in ('id','size','sha256')}
        route=record.get('delivery_route') or state['rpc'].stream.received_route
        try:state['rpc'].send_notify(dict(kind='applied',topic=topic,record=summary,delivery_route=route),priority=0)
        except OSError:self._drop(peer)

    def _provide(self,topic,digest,offset,count):
        if not self._active() or not self.can_upload(topic):raise PermissionError('设备同步已关闭。')
        with self._registry_lock:entries=list(self._objects.get((topic,digest),{}).values())
        entry=next((entry for entry in entries if entry[2]()),None)
        if entry is None:raise FileNotFoundError('设备没有此内容。')
        source,size,valid=entry
        if offset+count>size:raise FileNotFoundError('设备内容已更新或移除。')
        if isinstance(source,bytes):data=source[offset:offset+count]
        else:
            with self._source_lock,_source_reader(Path(source)) as stream:stream.seek(offset);data=stream.read(count)
        if len(data)!=count or not valid() or not self._active():raise FileNotFoundError('设备内容已改变。')
        return data

    def available(self):
        return [state for state in self._states.values() if state['rpc'] is not None and self._authorized(state)
                and not state['rpc'].closed.is_set()]

    def fetch(self,topic,record,target,callback,*,preferred=None,progress=None):
        if not valid_object(record):raise ValueError('Invalid peer object metadata')
        sources=self._sources.get((topic,record['sha256']),set())
        states=sorted((state for state in self.available() if state['peer'] in sources or state['peer']==preferred),
                      key=lambda state:({'lan':0,'p2p':1,'relay':2,'cloud':3}[state['route']],state['peer']!=preferred))
        if not states or not self.can_receive(topic):return None
        epoch=self.epoch;job=PeerFetch(epoch=epoch,sha256=record['sha256']);self._fetches[job.identifier]=(job,callback,progress)
        target=Path(target);temporary=target.with_name('.'+target.name+'.peer-'+job.identifier+'.part')
        def download():
            error=PeerUnavailable('在线设备暂时没有可用内容。');result=None
            try:
                target.parent.mkdir(parents=True,exist_ok=True)
                for state in states:
                    if job.cancel.is_set():raise InterruptedError('已取消设备同步。')
                    try:
                        digest=hashlib.sha256();offset=0;last=0.;requested=0;pending=deque();rpc=state['rpc']
                        with temporary.open('wb') as output:
                            try:
                                while offset<record['size']:
                                    if job.cancel.is_set() or epoch!=self.epoch:raise InterruptedError('设备同步授权已改变。')
                                    # Keep bounded block requests in flight, as in BEP,
                                    # so disk work and RTT do not idle the native transport.
                                    while requested<record['size'] and len(pending)<FETCH_PIPELINE_REQUESTS:
                                        count=min(RPC_BLOCK_BYTES,record['size']-requested)
                                        request=rpc.begin_request(topic,record['sha256'],requested,count,job.cancel,wait=not pending)
                                        if request is None:break
                                        pending.append(request);requested+=count
                                    data=rpc.receive_request(pending.popleft(),job.cancel)
                                    output.write(data);digest.update(data);offset+=len(data)
                                    if time.monotonic()-last>=.05:
                                        last=time.monotonic();self._progress.emit((job.identifier,offset,record['size']))
                            finally:
                                for request in pending:rpc.discard_request(request)
                        if digest.hexdigest()!=record['sha256']:raise ValueError('设备文件 SHA256 校验失败。')
                        if job.cancel.is_set() or epoch!=self.epoch or not self.can_receive(topic):raise InterruptedError('已取消设备同步。')
                        os.replace(temporary,target);result=target;job.route=rpc.stream.received_route;error=None;break
                    except InterruptedError:raise
                    except (OSError,ValueError) as problem:error=problem
            except (OSError,ValueError) as problem:error=problem
            finally:
                temporary.unlink(missing_ok=True)
                self._finished.emit(dict(id=job.identifier,result=result,error=error))
        if not self._submit(download,io=True):
            self._fetches.pop(job.identifier,None);return None
        return job

    def _handle_progress(self,value):
        active=self._fetches.get(value[0])
        if active is not None and active[0].epoch==self.epoch and active[2] is not None:active[2](value[1],value[2])

    def _handle_finished(self,value):
        if 'internal_error' in value:
            self.status_changed.emit('设备同步内部错误：'+value['internal_error']);return
        active=self._fetches.pop(value['id'],None)
        if active is not None and active[0].epoch==self.epoch:active[1](value['result'],value['error'])

    def close(self):
        if self.closed:return
        self.closed=True;self.epoch+=1;self.timer.stop();self.flush.stop()
        for peer in list(self._states):self._drop(peer,retry=False)
        for job,_,_ in self._fetches.values():job.cancel.set()
        if self.broker is not None:self.broker.close();self.broker=None
        self._pool.shutdown(wait=False,cancel_futures=True)
        self._io_pool.shutdown(wait=False,cancel_futures=True)
