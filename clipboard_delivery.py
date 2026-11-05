"""Delivery receipts for the latest copy; cloud backup is never a device receipt."""
from __future__ import annotations
import json
import logging
import re
from pathlib import Path
from PySide6.QtCore import QObject,QTimer

ROUTES=('lan','p2p','relay','cloud')
MAX_TARGETS=100
RECEIPT_RETRY_INITIAL_MS=2500
RECEIPT_RETRY_MAX_MS=300000
MAX_METADATA_BYTES=65536
UUID=re.compile(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}')
SHA256=re.compile('[a-f0-9]{64}')
LOG=logging.getLogger(__name__)


def sync_progress(state):
    cloud=state.get('cloud_transfer') or (dict(phase='complete',received=1,total=1) if state.get('backed_up') else dict(phase='idle',received=0,total=0))
    counts=state.get('deliveries',{})
    total=sum(row['total'] for row in counts.values());received=sum(row['success'] for row in counts.values())
    active=state.get('value') is not None and bool(state.get('id')) and state.get('phase') not in ('local','size_limit')
    phase='idle' if not active or not total else 'complete' if received==total else 'delivering'
    return dict(cloud=cloud,devices=dict(phase=phase,received=received,total=total))


def route_map(value):
    if not isinstance(value,dict) or len(value)>MAX_TARGETS:raise ValueError('Invalid clipboard delivery targets')
    if any(not isinstance(device,str) or not UUID.fullmatch(device) or route not in ROUTES for device,route in value.items()):
        raise ValueError('Invalid clipboard delivery route')
    return dict(value)


def delivery_state(value):
    if not isinstance(value,dict) or not UUID.fullmatch(str(value.get('id',''))) or not SHA256.fullmatch(str(value.get('sha256',''))):
        raise ValueError('Invalid clipboard receipt identity')
    origin=value.get('origin','')
    if not isinstance(origin,str) or not UUID.fullmatch(origin):raise ValueError('Invalid clipboard origin')
    plan=route_map(value.get('plan',{}));receipts=route_map(value.get('receipts',{}))
    if origin in plan or origin in receipts:raise ValueError('Clipboard origin is not a receiving device')
    return dict(id=value['id'],sha256=value['sha256'],origin=origin,plan=plan,receipts=receipts)


class ClipboardDeliveries(QObject):
    def __init__(self,client,base):
        super().__init__(client);self.client=client;self.path=Path(base)/'clipboard-delivery.json'
        self.current=None;self.cloud_ready=False;self.pending_route=None;self.dirty=False;self.inflight=False;self.attempt=0;self.published=False;self.blocked=False
        self.cloud_snapshot=None
        self.connections=None
        self.retry=QTimer(self);self.retry.setSingleShot(True);self.retry.timeout.connect(self.flush)
        try:
            with self.path.open('rb') as stream:raw=stream.read(MAX_METADATA_BYTES+1)
            if len(raw)>MAX_METADATA_BYTES:raise ValueError('Clipboard delivery metadata exceeds limit')
            saved=json.loads(raw)
            if saved.get('account')==self._account():self.current=delivery_state(saved['delivery'])
        except FileNotFoundError:pass
        except (OSError,ValueError,KeyError,TypeError) as error:LOG.warning('Cannot restore clipboard delivery metadata: %r',error)

    def _account(self):
        identity=self.client._session_identity()
        return identity[0] if identity else ''

    def _changed(self):
        if self.current is not None:
            try:
                self.path.parent.mkdir(parents=True,exist_ok=True)
                temporary=self.path.with_suffix('.tmp')
                temporary.write_text(json.dumps(dict(account=self._account(),delivery=self.current),separators=(',',':')),encoding='utf8')
                temporary.replace(self.path)
            except OSError as error:LOG.warning('Cannot save clipboard delivery metadata: %r',error)
        view=self.client.view
        if self.current and view.get('id')==self.current['id']:
            self.client._set_view(view['value'],view['phase'],view['id'])

    def reset(self):
        self.retry.stop();self.current=None;self.cloud_ready=False;self.pending_route=None;self.dirty=False;self.inflight=False;self.attempt=0;self.published=False;self.blocked=False
        self.cloud_snapshot=None

    def begin(self,record,plan=None):
        origin=record.get('origin_device') or record.get('device')
        candidate=dict(id=record['id'],sha256=record['sha256'],origin=origin,
                       plan=record.get('delivery_plan',{}) if plan is None else plan,receipts={})
        try:candidate=delivery_state(candidate)
        except ValueError:return
        if self.current and (self.current['id'],self.current['sha256'])==(candidate['id'],candidate['sha256']):
            candidate['receipts']=dict(self.current['receipts'])
            candidate['plan']=dict(self.current['plan'],**candidate['plan'])
            if candidate==self.current:return
        else:
            # Cloud commit hints can arrive while the same peer body is still
            # being unpacked. Keep that hint until its exact ID/hash is applied.
            cached=self.cloud_snapshot
            self.reset();self.cloud_snapshot=cached
        if candidate['origin']==self.client.account.device_id:self.dirty=True
        self.current=candidate;self._changed()
        if self.cloud_snapshot is not None:self.merge(self.cloud_snapshot)

    def acknowledge(self,device,route):
        if self.current is None or device==self.current['origin'] or route not in ROUTES or not UUID.fullmatch(str(device)):return
        if self.current['receipts'].get(device)==route:return
        self.current['receipts'][device]=route;self._changed()

    def received(self,record,route):
        self.begin(record)
        if self.current is None or self.current['id']!=record['id']:return
        self.acknowledge(self.client.account.device_id,route)
        self.pending_route=route;self.dirty=True;self.flush()

    def merge(self,value):
        try:incoming=delivery_state(value)
        except ValueError:return
        self.cloud_snapshot=incoming
        if self.current is None or any(incoming[key]!=self.current[key] for key in ('id','sha256','origin')):return
        # This account-authenticated event is emitted only for a committed head;
        # it confirms backup without another body request or a polling delay.
        self.cloud_ready=True
        updated=dict(incoming,plan=dict(self.current['plan'],**incoming['plan']),receipts=dict(self.current['receipts'],**incoming['receipts']))
        if updated!=self.current:self.current=updated;self._changed()
        view=self.client.view
        if view.get('id')==incoming['id'] and view.get('phase')=='peer':
            self.client._set_view(view['value'],'synced',view['id'],backed_up=True)
        self.flush()

    def committed(self,head,delivery=None):
        if not head.get('ready'):return
        self.begin(head)
        if self.current is None or self.current['id']!=head.get('id') or self.current['sha256']!=head.get('sha256'):return
        self.cloud_ready=True
        if delivery is not None:self.merge(delivery)
        if self.current['origin']==self.client.account.device_id and not self.published:self.dirty=True
        self.flush()

    def flush(self):
        state=self.current;client=self.client
        if not self.dirty or not state or self.inflight or self.blocked or self.retry.isActive() or not self.cloud_ready or not client._active():return
        if client.view.get('id')!=state['id']:return
        identity=state['id'];digest=state['sha256'];self.inflight=True
        data=dict(sha256=digest)
        if state['origin']==client.account.device_id:data['plan']=state['plan']
        elif self.pending_route in ROUTES:data['route']=self.pending_route
        else:self.inflight=False;return
        def done(result,error):
            if self.current is None or (self.current['id'],self.current['sha256'])!=(identity,digest):return
            self.inflight=False
            if error:
                if getattr(error,'status',0) in (400,403,404,405,409,410):
                    self.blocked=True;LOG.warning('Clipboard receipt rejected: %s',getattr(error,'code','unknown'));return
                self.attempt=min(self.attempt+1,8)
                self.retry.start(min(RECEIPT_RETRY_MAX_MS,RECEIPT_RETRY_INITIAL_MS*2**(self.attempt-1)));return
            self.attempt=0;self.dirty=False;self.published=True
            if isinstance(result,dict):self.merge(result.get('delivery'))
        client._request('POST',f'/v1/clipboard/{identity}/delivery',done,data=data)

    def summary(self,identity):
        current=self.current if self.current is not None and self.current['id']==identity else None
        receipts=current['receipts'] if current else {}
        if self.connections is None:
            plan=dict(current['plan'],**receipts) if current else {}
        else:
            plan=dict(self.connections)
            if current:
                plan.pop(current['origin'],None)
                # This device is an online recipient, even though its mesh
                # snapshot intentionally contains only the other devices.
                device=self.client.account.device_id
                if device!=current['origin'] and (device in current['plan'] or device in receipts):
                    plan[device]=receipts.get(device,current['plan'].get(device,'cloud'))
                plan={device:receipts.get(device,route) for device,route in plan.items()}
        receipts={device:route for device,route in receipts.items() if device in plan}
        return {route:dict(success=sum(value==route for value in receipts.values()),
                           total=sum(value==route for value in plan.values())) for route in ROUTES}

    def set_connections(self,snapshot):
        if not isinstance(snapshot,dict) or len(snapshot)>MAX_TARGETS:raise ValueError('Invalid device connection snapshot')
        connections={}
        for device,info in snapshot.items():
            if not isinstance(device,str) or not UUID.fullmatch(device) or not isinstance(info,dict):
                raise ValueError('Invalid device connection identity')
            route=info.get('route')
            if route in ('connecting','cloud'):connections[device]='cloud'
            elif route in ROUTES:connections[device]=route
            elif route!='disconnected':raise ValueError('Invalid device connection state')
        if self.connections==connections:return
        self.connections=connections
        view=self.client.view
        self.client._set_view(view['value'],view['phase'],view['id'])
