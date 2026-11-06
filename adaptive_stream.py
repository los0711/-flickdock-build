"""Bounded, acknowledged records over existing encrypted LAN/WebRTC streams.

Each direction chooses independently. Sequence numbers permit an interrupted
record to be retried on another connection without duplicating application bytes.
TLS/DTLS still provide encryption, authentication and congestion control.
"""
from collections import OrderedDict,deque
from dataclasses import dataclass,field
import json
import logging
import os
import re
import socket
import struct
import threading
import time
from uuid import uuid4

from route_policy import RoutePolicy,ROUTE_ORDER,preferred_routes

HEADER=struct.Struct('!BQI')
DATA,ACK,PROBE,PROBE_ACK,SIGNAL,PING,PONG=range(7)
MAX_RECORD_BYTES=64*1024
BLOCK_BYTES=MAX_RECORD_BYTES-HEADER.size
WINDOW_BYTES=16*1024*1024
RECEIVE_BYTES=32*1024*1024
PROBE_BYTES=256*1024
PROBE_INTERVAL_SECONDS=30.
PROBE_TIMEOUT_SECONDS=12.
STALL_SECONDS=8.
PATH_CONNECT_SECONDS=15.
PATH_RETRY_SECONDS=30.
EARLY_SECONDARY_SIGNALS=64
EARLY_SECONDARY_BYTES=128*1024
RETIRED_SECONDARY_ATTEMPTS=64
ACTIVE_SECONDS=5.
BULK_BYTES=128*1024
LATENCY_MESSAGE_BYTES=16*1024
PING_INTERVAL_SECONDS=30.
CLOUD_IDLE_PING_SECONDS=300.
LOG=logging.getLogger(__name__)


@dataclass
class PathState:
    key:str
    route:str
    stream:object
    lock:object=field(default_factory=threading.Lock)
    dead:bool=False
    sent:int=0
    received:int=0
    probed:float=0.
    probes:int=0
    pinged:float=0.
    ping_number:int=0
    latency:float=0.
    sample_at:float=0.
    sample_bytes:int=0
    unacknowledged:int=0
    progress_at:float=0.


class AdaptiveStream:
    def __init__(self,raw,nearby,*,initiator,cancel,authorized=lambda:True,
                 secondary=None,cloud=None,primary=True,prefer_tls=False,signal_out=None,event=lambda value:None,allow_lan=True):
        self.raw=raw;self.cancel=cancel;self.authorized=authorized;self.secondary=secondary
        self.initiator=initiator;self.event=event;self.policy=RoutePolicy()
        self.cloud=cloud;self._cloud_starting=False;self._cloud_retry=0.
        self.signal_out=signal_out
        self.allow_lan=allow_lan
        self._condition=threading.Condition(threading.RLock());self._send_lock=threading.Lock()
        self._paths={};self._pending=OrderedDict();self._reorder={};self._input=deque()
        self._input_bytes=0;self._pending_bytes=0;self._sequence=0;self._received_sequence=0
        self._sent_bytes=0;self._acked_bytes=0;self._received_bytes=0
        self.sent_route='';self.received_route=''
        self._payload=0;self._acked_payload=0;self._offset=0;self._payload_origin=0
        self._timeout=1.;self._closed=False;self._probe=None;self._probe_number=0
        self._last_send=0.;self._empty_since=0.;self._secondary=None;self._secondary_id='';self._secondary_ready=False
        self._secondary_started=0.;self._secondary_retry={'p2p':time.perf_counter()+1.,'relay':0.};self._secondary_route='';self._prefer_tls=prefer_tls
        self._early_secondary=deque();self._early_secondary_bytes=0
        self._retired_secondary=deque(maxlen=RETIRED_SECONDARY_ATTEMPTS)
        self._reported='';self._selected_key=None;self._next_probe=0.;self._probe_data=os.urandom(PROBE_BYTES)
        if nearby is not raw:
            raw.release_owned_socket(nearby.stream)
            self._add('lan','lan',nearby.stream)
        if primary:self._add('primary','relay' if raw.route=='relay' else 'p2p',raw)
        self._thread(self._monitor,'routes')

    def _thread(self,work,name):
        threading.Thread(target=work,daemon=True,name='FlickDock-adaptive-'+name).start()

    @property
    def closed(self):return self._closed

    @property
    def route(self):
        with self._condition:
            key=self.policy.current
            return self._paths[key].route if key in self._paths and not self._paths[key].dead else ''

    def settimeout(self,value):self._timeout=value

    def _live(self):return {key:path.route for key,path in self._paths.items() if not path.dead}

    def _add(self,key,route,stream):
        with self._condition:
            if self._closed:stream.shutdown();return
            if key in self._paths:raise ValueError('Duplicate adaptive connection')
            stream.settimeout(.25);path=PathState(key,route,stream);self._paths[key]=path
            self.policy.choose(self._live());self._empty_since=0.;self._condition.notify_all()
        self._thread(lambda:self._reader(path),'read-'+key)

    def _alive(self):return not self._closed and not self.cancel.is_set() and self.authorized()

    def attach_native(self,raw,role):
        from nearby_transport import try_nearby
        nearby=try_nearby(raw,role,self.cancel) if self.allow_lan else raw
        self.attach_prepared(raw,nearby)

    def attach_prepared(self,raw,nearby):
        if nearby is not raw:
            raw.release_owned_socket(nearby.stream)
            with self._condition:existing=self._paths.get('lan')
            if existing is None or existing.dead:
                with self._condition:self._paths.pop('lan',None)
                self._add('lan','lan',nearby.stream)
            else:nearby.stream.close()
        with self._condition:present='primary' in self._paths
        if not present:self._add('primary','relay' if raw.route=='relay' else 'p2p',raw)

    def _frame(self,path,kind,sequence,body=b''):
        if path.dead or not self._alive():raise ConnectionError('传输线路不可用。')
        packet=HEADER.pack(kind,sequence,len(body))+body
        try:
            with path.lock:path.stream.sendall(packet)
            with self._condition:path.sent+=len(packet)
        except (OSError,ValueError):self._failed(path);raise

    def _control(self,kind,sequence,body=b'',path=None):
        with self._condition:
            choices=[path] if path is not None and not path.dead else sorted(
                (p for p in self._paths.values() if not p.dead),key=lambda p:ROUTE_ORDER[p.route])
        for candidate in choices:
            try:self._frame(candidate,kind,sequence,body);return
            except (OSError,ValueError):continue
        raise ConnectionError('没有可用的传输线路。')

    def _signal(self,value):
        if self.signal_out is not None and self.signal_out(value):return
        self.send_inband(value)

    def send_inband(self,value):
        self._control(SIGNAL,0,json.dumps(value,separators=(',',':')).encode('utf8'))

    def _failed(self,path):
        with self._condition:
            if path.dead:return
            path.dead=True
            for record in self._pending.values():
                if record['path']==path.key:record['path']=None
            path.unacknowledged=0
            self.policy.choose(self._live());self._condition.notify_all()
        LOG.info('Adaptive %s connection ended',path.route)
        try:
            if hasattr(path.stream,'shutdown'):path.stream.shutdown()
            else:path.stream.close()
        except OSError:LOG.debug('Ended route cleanup failed',exc_info=True)

    def _exact(self,path,size):
        result=bytearray()
        while len(result)<size:
            if not self._alive() or path.dead:raise ConnectionError('传输已关闭。')
            try:chunk=path.stream.recv(size-len(result))
            except socket.timeout:continue
            if not chunk:raise EOFError('传输线路已关闭。')
            result.extend(chunk)
        return bytes(result)

    def _reader(self,path):
        try:
            while self._alive() and not path.dead:
                kind,sequence,size=HEADER.unpack(self._exact(path,HEADER.size))
                limit=PROBE_BYTES if kind==PROBE else MAX_RECORD_BYTES
                if kind not in (DATA,ACK,PROBE,PROBE_ACK,SIGNAL,PING,PONG) or size>limit:
                    raise ValueError('Invalid adaptive frame')
                body=self._exact(path,size)
                with self._condition:path.received+=HEADER.size+size
                if kind==DATA:self._receive_data(path,sequence,body)
                elif kind==ACK:self._ack(sequence,body)
                elif kind==PROBE:
                    if len(body)!=PROBE_BYTES:raise ValueError('Invalid route probe')
                    self._control(PROBE_ACK,sequence,struct.pack('!I',len(body)),path)
                elif kind==PROBE_ACK:self._probe_ack(path,sequence,body)
                elif kind==PING:
                    if body:raise ValueError('Invalid route ping')
                    self._control(PONG,sequence,path=path)
                elif kind==PONG:
                    if body:raise ValueError('Invalid route pong')
                    if sequence==path.ping_number:path.latency=time.perf_counter()-path.pinged
                elif kind==SIGNAL:
                    if sequence:raise ValueError('Invalid route signal')
                    self._secondary_signal(json.loads(body))
        except (OSError,EOFError,ValueError,TypeError,KeyError,RecursionError) as error:
            if self._alive():LOG.debug('Adaptive connection ended: %r',error)
        finally:self._failed(path)

    def _receive_data(self,path,sequence,body):
        with self._condition:
            self.received_route=path.route
            if not body or not sequence or sequence>self._received_sequence+WINDOW_BYTES//BLOCK_BYTES+1:
                raise ValueError('Invalid adaptive sequence')
            if sequence>self._received_sequence:
                if sequence not in self._reorder:self._reorder[sequence]=body
                while self._received_sequence+1 in self._reorder:
                    chunk=self._reorder[self._received_sequence+1]
                    if self._input_bytes+len(chunk)>RECEIVE_BYTES:
                        if not self._alive():raise ConnectionError('传输已关闭。')
                        self._condition.wait(.1);continue
                    self._received_sequence+=1;self._reorder.pop(self._received_sequence)
                    self._input.append(chunk);self._input_bytes+=len(chunk);self._received_bytes+=len(chunk)
                    self._condition.notify_all()
            received=self._received_sequence;count=self._received_bytes
        self._control(ACK,received,struct.pack('!Q',count),path)

    def _ack(self,sequence,body):
        if len(body)!=8:raise ValueError('Invalid adaptive acknowledgement')
        count=struct.unpack('!Q',body)[0]
        with self._condition:
            if sequence>self._sequence or count>self._sent_bytes:raise ValueError('Invalid adaptive acknowledgement')
            if count<=self._acked_bytes:return
            numbers=[number for number in self._pending if number<=sequence]
            removed=sum(len(self._pending[number]['body']) for number in numbers)
            if self._acked_bytes+removed!=count:raise ValueError('Inconsistent adaptive acknowledgement')
            for number in numbers:
                record=self._pending.pop(number)
                self._acked_payload+=record['payload'];self._pending_bytes-=len(record['body'])
                path=self._paths.get(record['path'])
                if path is not None:
                    path.unacknowledged-=len(record['body']);path.progress_at=time.perf_counter()
                    if path.unacknowledged<0:raise RuntimeError('Adaptive pending-byte accounting diverged')
                    if not path.sample_at:path.sample_at=record['sent']
                    path.sample_bytes+=len(record['body']);elapsed=time.perf_counter()-path.sample_at
                    if elapsed>=1. and path.sample_bytes>=BULK_BYTES:
                        self.policy.observe(path.key,path.sample_bytes,elapsed);path.sample_at=time.perf_counter();path.sample_bytes=0
            self._acked_bytes=count;self._condition.notify_all()

    def _send(self,data,payload):
        with self._send_lock:
            self._last_send=time.perf_counter()
            view=memoryview(data)
            for offset in range(0,len(view),BLOCK_BYTES):
                body=bytes(view[offset:offset+BLOCK_BYTES])
                with self._condition:
                    while self._pending_bytes+len(body)>WINDOW_BYTES or len(self._pending)>=WINDOW_BYTES//BLOCK_BYTES or not self._live():
                        if not self._alive():raise ConnectionError('传输已关闭。')
                        self._condition.wait(.1)
                    if not self._alive():raise ConnectionError('传输已关闭。')
                    self._sequence+=1;sequence=self._sequence;self._sent_bytes+=len(body)
                    self._pending_bytes+=len(body);self._last_send=time.perf_counter()
                    record=dict(body=body,payload=len(body) if payload else 0,path=None,sent=0.)
                    self._pending[sequence]=record
                self._transmit(sequence,record)

    def _transmit(self,sequence,record):
        with self._condition:
            key=self.policy.current;path=self._paths.get(key)
            if len(record['body'])<=LATENCY_MESSAGE_BYTES:
                eligible=preferred_routes(self._live())
                quick=[p for p in self._paths.values() if p.key in eligible
                       and p.latency>0 and time.perf_counter()-p.pinged<PING_INTERVAL_SECONDS*2]
                if quick:path=min(quick,key=lambda p:p.latency);key=path.key
            if path is None or path.dead:return
            if record['path']!=key:
                previous=self._paths.get(record['path'])
                if previous is not None:previous.unacknowledged-=len(record['body'])
                if not path.unacknowledged:path.progress_at=time.perf_counter()
                path.unacknowledged+=len(record['body'])
            record.update(path=key,sent=time.perf_counter())
        try:
            self._frame(path,DATA,sequence,record['body'])
            self.sent_route=path.route
        except (OSError,ValueError):pass

    def sendall(self,data):self._send(data,False)
    def send_payload(self,data):self._send(data,True)

    def set_payload_offset(self,value):
        with self._condition:self._offset=value;self._payload_origin=self._acked_payload

    def payload_progress(self):
        with self._condition:return self._offset+self._acked_payload-self._payload_origin

    def recv(self,size):
        target=bytearray(size);count=self.recv_into(target);return bytes(target[:count])

    def recv_into(self,target):
        if not len(target):return 0
        with self._condition:
            deadline=time.perf_counter()+self._timeout
            while not self._input:
                if not self._alive():return 0
                remaining=deadline-time.perf_counter()
                if remaining<=0:raise socket.timeout()
                self._condition.wait(remaining)
            data=self._input.popleft();count=min(len(target),len(data));target[:count]=data[:count]
            if count<len(data):self._input.appendleft(data[count:])
            self._input_bytes-=count;self._condition.notify_all();return count

    def _probe_ack(self,path,number,body):
        with self._condition:
            probe=self._probe
            if probe is None or probe[:2]!=(path.key,number):return
            if body!=struct.pack('!I',PROBE_BYTES):raise ValueError('Invalid route probe acknowledgement')
            self.policy.observe(path.key,PROBE_BYTES,time.perf_counter()-probe[2]);self._probe=None
            self.policy.choose(self._live())

    def _launch_secondary(self,identifier,*,direct_only):
        if not self.secondary:return
        with self._condition:
            if self._secondary is not None or self._secondary_id:return
            self._secondary_id=identifier;self._secondary_started=time.perf_counter();self._secondary_ready=False
            target='p2p' if direct_only else 'relay';self._secondary_route=target
        def failed():
            with self._condition:
                if identifier!=self._secondary_id:return
                self._retire_secondary(identifier)
                candidate=self._secondary;self._secondary=None;self._secondary_id='';self._secondary_ready=False
                self._secondary_retry[target]=time.perf_counter()+PATH_RETRY_SECONDS
            if candidate is not None:candidate.shutdown()
        def event(value):
            if not self._alive() or identifier!=self._secondary_id:return
            kind=value.get('type')
            try:
                if kind in ('description','candidate'):
                    self._signal(dict(kind='candidate',id=identifier,payload=value))
                elif kind=='open':
                    candidate=self._secondary
                    def connected():
                        from nearby_transport import try_nearby
                        if direct_only and candidate.route=='relay':failed();return
                        with self._condition:lan=self._paths.get('lan')
                        nearby=candidate if not self.allow_lan or candidate.route=='relay' or lan is not None and not lan.dead else try_nearby(candidate,'send' if self.initiator else 'receive',self.cancel)
                        if not self._alive() or identifier!=self._secondary_id:
                            if nearby is not candidate:nearby.stream.close()
                            candidate.shutdown();return
                        if nearby is not candidate:
                            candidate.release_owned_socket(nearby.stream)
                            with self._condition:old=self._paths.pop('lan',None)
                            if old is not None:old.stream.close()
                            self._add('lan','lan',nearby.stream)
                        route='relay' if candidate.route=='relay' else 'p2p';key='secondary-'+route
                        with self._condition:
                            existing=self._paths.get(key)
                            if existing is not None and not existing.dead:candidate.shutdown()
                            else:
                                self._paths.pop(key,None);self._add(key,route,candidate)
                            self._retire_secondary(identifier)
                            self._secondary=None;self._secondary_id='';self._secondary_ready=False
                            self._secondary_retry[target]=time.perf_counter()+PATH_RETRY_SECONDS
                    self._thread(connected,'candidate-ready')
                elif kind in ('closed','error') or kind=='state' and value.get('state')=='failed':
                    failed()
            except (OSError,ValueError):LOG.debug('Alternative route signaling ended',exc_info=True)
        configured=self.secondary(event,dict(type='init',initiator=self.initiator,preferTls=self._prefer_tls,
            forceRelay=not direct_only,directOnly=direct_only))
        if configured is None:
            failed();return
        candidate,configuration=configured
        with self._condition:
            if not self._alive() or identifier!=self._secondary_id:candidate.shutdown();return
            self._secondary=candidate
        candidate.control(configuration)
        with self._condition:
            if not self._alive() or identifier!=self._secondary_id:return
            self._secondary_ready=True;pending=self._take_secondary_signals(identifier)
        for payload in pending:
            if not self._alive() or identifier!=self._secondary_id:return
            candidate.control(payload)

    def _take_secondary_signals(self,identifier):
        now=time.perf_counter();ready=[];keep=deque();size=0
        for when,identity,payload,length in self._early_secondary:
            if now-when>=PATH_CONNECT_SECONDS:continue
            if identity==identifier:ready.append(payload)
            else:keep.append((when,identity,payload,length));size+=length
        self._early_secondary=keep;self._early_secondary_bytes=size
        return ready

    def _retire_secondary(self,identifier):
        if identifier and identifier not in self._retired_secondary:self._retired_secondary.append(identifier)
        self._take_secondary_signals(identifier)

    def _queue_secondary_signal(self,identifier,payload):
        self._take_secondary_signals('')
        if identifier in self._retired_secondary:return
        if any(identity==identifier and value==payload for _,identity,value,_ in self._early_secondary):return
        length=len(json.dumps(payload,separators=(',',':')).encode('utf8'))
        if len(self._early_secondary)>=EARLY_SECONDARY_SIGNALS or self._early_secondary_bytes+length>EARLY_SECONDARY_BYTES:
            self._retire_secondary(identifier)
            LOG.warning('Alternative route signal buffer reached its bound')
            return
        self._early_secondary.append((time.perf_counter(),identifier,payload,length));self._early_secondary_bytes+=length

    def _secondary_signal(self,value):
        if not isinstance(value,dict) or not re.fullmatch('[a-f0-9]{32}',str(value.get('id',''))):raise ValueError('Invalid alternative route identity')
        if not self._alive():return
        if value.get('kind')=='cloud':
            if self.initiator or not self.cloud or not re.fullmatch('[a-f0-9]{64}',str(value.get('key',''))):return
            configured=self.cloud(dict(id=value['id'],key=value['key']))
            if configured is not None:
                with self._condition:old=self._paths.pop('cloud',None)
                if old is not None:old.stream.shutdown()
                self._add('cloud','cloud',configured[0])
            return
        if value.get('kind')=='start':
            if self.initiator:raise ValueError('Unexpected alternative route initiator')
            if not isinstance(value.get('directOnly'),bool):raise ValueError('Invalid alternative route mode')
            with self._condition:
                if value['id'] in self._retired_secondary or value['id']==self._secondary_id:return
                old=self._secondary
                if self._secondary_id:self._retire_secondary(self._secondary_id)
                self._secondary=None;self._secondary_id='';self._secondary_ready=False
            if old is not None:old.shutdown()
            self._launch_secondary(value['id'],direct_only=value['directOnly'])
        elif value.get('kind')=='candidate':
            payload=value.get('payload')
            if not isinstance(payload,dict) or payload.get('type') not in ('description','candidate'):raise ValueError('Invalid alternative route signal')
            if len(json.dumps(payload,separators=(',',':')).encode('utf8'))>32*1024:raise ValueError('Alternative route signal exceeds limit')
            with self._condition:
                if value['id'] in self._retired_secondary:return
                candidate=self._secondary if value['id']==self._secondary_id and self._secondary_ready else None
                if candidate is None:self._queue_secondary_signal(value['id'],payload);return
            candidate.control(payload)
        # Signals from an earlier connection generation may already be in flight.

    def _open_cloud(self):
        try:
            configured=self.cloud(None)
            if configured is None:return
            stream,descriptor=configured
            if not self._alive():stream.shutdown();return
            self._signal(dict(kind='cloud',**descriptor))
            with self._condition:self._paths.pop('cloud',None)
            self._add('cloud','cloud',stream)
        except (OSError,ValueError):LOG.debug('Cloud alternative unavailable',exc_info=True)
        finally:self._cloud_starting=False;self._cloud_retry=time.perf_counter()+PATH_RETRY_SECONDS

    def _monitor(self):
        while self._alive():
            now=time.perf_counter()
            try:
                with self._condition:
                    expired=[path for path in self._paths.values() if not path.dead
                             and path.unacknowledged and now-path.progress_at>=STALL_SECONDS]
                # A queued window can take longer than the stall deadline on a
                # healthy WAN. Only absence of acknowledgement progress fails it.
                for failed in expired:self._failed(failed)
                with self._condition:
                    for key,path in self._paths.items():
                        if key!='primary' and not key.startswith('secondary-'):continue
                        if path is not None and not path.dead and path.stream.route in ('direct','relay'):
                            path.route='relay' if path.stream.route=='relay' else 'p2p'
                    live=self._live();self.policy.choose(live)
                    if not live:
                        if not self._empty_since:self._empty_since=now
                        if now-self._empty_since>PATH_CONNECT_SECONDS:break
                    route=self.route
                    changed=self._selected_key!=self.policy.current
                    self._selected_key=self.policy.current
                    if route and route!=self._reported:
                        self._reported=route;self.event(dict(type='route',route=route))
                    retry=[(number,record) for number,record in self._pending.items()
                           if record['path'] not in live
                           or changed and record['path']!=self.policy.current and len(record['body'])>LATENCY_MESSAGE_BYTES]
                    active=bool(self._pending) or self._last_send>0 and now-self._last_send<ACTIVE_SECONDS
                for number,record in retry:self._transmit(number,record)
                with self._condition:
                    cloud_path=self._paths.get('cloud')
                    native_available=any(path.route!='cloud' and not path.dead for path in self._paths.values())
                    open_cloud=(self.initiator and self.cloud and active and not native_available and not self._cloud_starting and now>=self._cloud_retry
                                and (cloud_path is None or cloud_path.dead))
                    if open_cloud:self._cloud_starting=True
                if open_cloud:self._thread(self._open_cloud,'cloud-open')
                if self.initiator and self.secondary:
                    if self._secondary is not None and now-self._secondary_started>PATH_CONNECT_SECONDS:
                        with self._condition:
                            self._retire_secondary(self._secondary_id)
                            candidate=self._secondary;self._secondary=None;self._secondary_id='';self._secondary_ready=False
                            self._secondary_retry[self._secondary_route]=now+PATH_RETRY_SECONDS
                        candidate.shutdown()
                    with self._condition:
                        routes=set(self._live().values())
                        missing=next((route for route in ('p2p','relay') if route not in routes and (route!='relay' or not routes.intersection(('lan','p2p')))
                            and (route=='p2p' or active) and now>=self._secondary_retry[route]),None)
                    if self._secondary is None and missing:
                        identifier=uuid4().hex;direct=missing=='p2p'
                        self._signal(dict(kind='start',id=identifier,directOnly=direct));self._launch_secondary(identifier,direct_only=direct)
                with self._condition:
                    if self._probe and now-self._probe[2]>PROBE_TIMEOUT_SECONDS:self._probe=None
                    pings=[p for p in self._paths.values() if not p.dead and now-p.pinged>(CLOUD_IDLE_PING_SECONDS if p.route=='cloud' and self.route!='cloud' else PING_INTERVAL_SECONDS)]
                    for candidate in pings:candidate.ping_number+=1;candidate.pinged=now
                    eligible=preferred_routes(self._live())
                    # The active route is measured by real payload ACKs. A
                    # bulk probe on its reverse ordered stream can hold all
                    # subsequent ACKs behind retransmission of the probe.
                    choices=sorted((p for p in self._paths.values() if p.key in eligible and p.key!=self.policy.current
                                    and now-p.probed>=(2. if p.probes<2 else PROBE_INTERVAL_SECONDS)),
                        key=lambda p:p.probed)
                    path=choices[0] if active and self._probe is None and choices and now>=self._next_probe else None
                    if path:
                        self._probe_number+=1;number=self._probe_number
                        self._probe=(path.key,number,now);path.probed=now;path.probes+=1;self._next_probe=now+1.
                if path:self._frame(path,PROBE,number,self._probe_data)
                for candidate in pings:self._frame(candidate,PING,candidate.ping_number)
            except (OSError,ValueError):LOG.debug('Route check did not complete',exc_info=True)
            self.cancel.wait(.1)
        self.shutdown()

    def traffic(self,*,refresh=False):
        with self._condition:return dict(sent=sum(p.sent for p in self._paths.values()),received=sum(p.received for p in self._paths.values()))

    def relay_traffic(self):
        with self._condition:return sum(p.sent+p.received for p in self._paths.values() if p.route=='relay')

    def measurements(self):
        with self._condition:
            return [dict(route=p.route,available=not p.dead,latency_ms=p.latency*1000 if p.latency else None,
                         bytes_per_second=self.policy.samples[p.key].rate if p.key in self.policy.samples and time.perf_counter()-self.policy.samples[p.key].when<60. else None)
                    for p in self._paths.values()]

    def connection_info(self):
        with self._condition:
            path=self._paths.get(self.policy.current)
            if path is None or path.dead:return dict(route='connecting',latency_ms=None)
            fresh=path.latency>0 and time.perf_counter()-path.pinged<PING_INTERVAL_SECONDS*2
            return dict(route=path.route,latency_ms=round(path.latency*1000) if fresh else None)

    def shutdown(self,*_):
        with self._condition:
            if self._closed:return
            self._closed=True;self._early_secondary.clear();self._early_secondary_bytes=0
            self._condition.notify_all();paths=list(self._paths.values())
        for path in paths:
            if hasattr(path.stream,'shutdown'):path.stream.shutdown()
            else:path.stream.close()
        if self._secondary is not None:self._secondary.shutdown()
        self.raw.shutdown()

    def close(self):
        deadline=time.perf_counter()+2.
        with self._condition:
            while self._pending and self._alive() and time.perf_counter()<deadline:self._condition.wait(.05)
        self.shutdown()
