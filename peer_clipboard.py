"""Latest-copy semantics shared by device delivery and durable cloud backup."""
from __future__ import annotations
import base64
import re
from PySide6.QtCore import QObject

from clipboard_sync import MAX_BYTES,MAX_ARCHIVE_BYTES,signature,unpack,unpack_inline
from network_diagnostics import record_event


class ClipboardPeerBridge(QObject):
    def __init__(self,client,mesh):
        super().__init__(client);self.client=client;self.mesh=mesh;self.jobs={};self.latest=None;self.confirmed=None;self.incoming=None
        mesh.offered.connect(self._offered);mesh.applied.connect(self._acknowledged)
        mesh.connections_changed.connect(client.deliveries.set_connections)
        client.deliveries.set_connections(mesh.connection_snapshot())

    def reset(self):
        self.cancel_fetches();self.latest=None;self.confirmed=None;self.incoming=None
        self.client.deliveries.set_connections(self.mesh.connection_snapshot())

    def cancel_fetches(self):
        for job in self.jobs.values():job.cancel.set()
        self.jobs.clear();self.client._priority_flags(peer_receiving=False)

    def calibrate(self):
        anchor=getattr(self.mesh.wan,'server_clock_anchor',None)
        if anchor is not None and self.mesh._active():self.client._clock_anchor=anchor

    def publish(self,pending):
        client=self.client;item=pending['package'];at=pending['intent']['at']
        if pending.get('peer_published') or type(at) is not int:return
        record={key:item[key] for key in ('kind','size','sha256')}
        record.update(id=pending['id'],copied_at=at,origin_device=client.account.device_id)
        record['delivery_plan']={device:info['route'] if info['route'] in ('lan','p2p','relay') else 'cloud'
                                 for device,info in self.mesh.connection_snapshot().items()
                                 if info['route']!='disconnected'}
        client.deliveries.begin(record)
        if item.get('inline'):
            record['inline']=item['inline']['body'];source=base64.b64decode(record['inline'],validate=True)
        else:source=item['path']
        epoch=client._epoch
        # Observation generations also change while another app delays native
        # rendering. Only a confirmed new copy/session epoch or a new logical
        # clipboard ID revokes a body that other devices are already fetching.
        valid=lambda:(client._active() and client._epoch==epoch
                      and client.view.get('id')==record['id'])
        if self.mesh.publish('clipboard',record,source,valid=valid):pending['peer_published']=True
        if pending.get('peer_published'):record_event(client._clock_path.parent,'remote','clipboard_send','OBJECT_PUBLISHED')

    def _acknowledged(self,topic,record,peer):
        client=self.client;pending=client._pending
        if topic!='clipboard' or record['id']!=client.view.get('id'):return
        client.deliveries.acknowledge(self.mesh.account_device_for_peer(peer),record.get('delivery_route',''))
        if pending is None:
            client._set_view(client.view['value'],'synced' if client.view.get('backed_up') else 'peer',record['id'],route=record.get('delivery_route',''))
            return
        if pending['id']!=record['id']:return
        pending['peer_delivered']=True;pending['delivery_route']=record.get('delivery_route','')
        client._set_view(client.view['value'],'peer',pending['id'],route=pending['delivery_route'],backed_up=False)
        client._copy_result(pending,True,phase='peer');client._priority_flags(waiting=True)

    def in_flight(self,head):
        job=self.jobs.get(head['id'])
        return job is not None and not job.cancel.is_set() and job.sha256==head['sha256']

    def accept_cloud_head(self,head):
        if not isinstance(head,dict):return True
        copied_at=head.get('copied_at',head.get('at',0))
        if type(copied_at) is not int:return self.latest is None
        if self.latest is not None and copied_at<self.latest[0]:return False
        if head.get('ready'):
            self.confirmed=(copied_at,head.get('id'))
            if self.latest is not None and copied_at>=self.latest[0]:self.latest=None
        return True

    def _eligible(self,head):
        client=self.client
        if not client._active() or head.get('origin_device')==client.account.device_id:return False
        if head['id']==client._seen:return False
        order=head['copied_at'],head['id']
        if self.latest is not None and order<=self.latest:return False
        if self.confirmed is not None and (order[0]<self.confirmed[0] or order[0]==self.confirmed[0] and order[1]!=self.confirmed[1]):return False
        if order[0]<client._observed_clock:return False
        if client._local_fence is not None:
            fence=client._copy_timestamp(client._local_fence)
            if fence is None or order[0]<=fence:return False
        return True

    def _offered(self,topic,head,peer):
        if topic!='clipboard':return
        if (head.get('kind') not in ('text','files','image') or type(head.get('copied_at')) is not int
                or not 0<head['copied_at']<=2**53-1 or not re.fullmatch('[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',head['id'])
                or not isinstance(head.get('origin_device'),str) or not 1<=len(head['origin_device'])<=128
                or not 0<head['size']<(MAX_ARCHIVE_BYTES if head.get('kind') in ('text','files') else MAX_BYTES)
                or not self._eligible(head)):return
        client=self.client;epoch=client._epoch
        record_event(client._clock_path.parent,'remote','clipboard_receive','OBJECT_OFFERED')
        order=head['copied_at'],head['id']
        if self.incoming is not None and order<self.incoming:return
        self.incoming=order
        def ready(value,directory):
            def valid():
                return epoch==client._epoch and self.incoming==order and self._eligible(head)
            if not valid():return
            def applied():
                self.latest=order
                client.deliveries.received(head,head.get('delivery_route',''))
                self.mesh.acknowledge(peer,'clipboard',head)
            client._accept_received(head,value,directory,valid=valid,applied=applied)
        if head['kind']=='text' and isinstance(head.get('inline'),str) and head['size']<=32768:
            try:value=unpack_inline(head)
            except (ValueError,TypeError,UnicodeError):return
            ready(value,client.cache);return
        if head['id'] in self.jobs:return
        self.cancel_fetches()
        try:directory=client._directory()
        except OSError:return
        path=directory/'peer-clipboard.bin'
        def downloaded(result,error):
            self.jobs.pop(head['id'],None)
            client._priority_flags(peer_receiving=bool(self.jobs))
            if epoch!=client._epoch:return
            if error is not None:
                record_event(client._clock_path.parent,'remote','clipboard_receive','OBJECT_FETCH_FAILED',details=dict(cause=type(error).__name__))
                client.refresh();return
            def prepared(value):
                if value is not None:
                    head['delivery_route']=job.route;ready(value,directory)
            client._work(lambda _:unpack(head['kind'],path,directory),prepared)
        job=self.mesh.fetch('clipboard',head,path,downloaded,preferred=peer,
                            progress=lambda received,total:client._priority_flags() if epoch==client._epoch else None)
        if job is not None:
            self.jobs[head['id']]=job;client._priority_flags(peer_receiving=True)

    def fetch_cloud_body(self,head,path,callback):
        generation=self.client._generation
        return self.mesh.fetch('clipboard',head,path,callback,
            progress=lambda received,total:self.client._priority_flags() if generation==self.client._generation else None)
