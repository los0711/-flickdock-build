"""Peer delivery of shelf revisions; the cloud journal resolves final order."""
from __future__ import annotations
from collections import deque
import hashlib
import logging
import mimetypes
from pathlib import Path
import re
import threading
from PySide6.QtCore import QObject,QTimer,Signal
from peer_rpc import PeerUnavailable
from sync_files import DIRECTORY_MIME,fingerprint,stage_item

EMPTY_SHA256=hashlib.sha256(b'').hexdigest()
LOG=logging.getLogger(__name__)


def _cache_directory(account):
    directory=Path(account)/'peer-cache'
    if any(path.is_symlink() or path.is_junction() for path in (Path(account),directory)):
        raise ValueError('设备同步缓存目录不能是链接。')
    return directory


def _indexed_source(entry,text,directory,cancel):
    if cancel.is_set():raise InterruptedError()
    if fingerprint(entry['ref'],text,entry['name'])!=entry.get('fingerprint'):return None
    if text is not None:
        data=text.encode('utf8')
        if hashlib.sha256(data).hexdigest()!=entry['sha256']:return None
        return dict(source=data,size=len(data),mime_type='text/plain')
    path=Path(entry['ref'])
    if path.is_symlink() or path.is_junction():raise ValueError('链接项目不能提供设备同步。')
    if path.is_dir():
        Path(directory).mkdir(parents=True,exist_ok=True)
        cached=Path(directory)/(entry['sha256']+'.blob')
        if not cached.is_file():
            prepared=stage_item(entry['ref'],None,entry['name'],directory,cancel)
            if prepared['sha256']!=entry['sha256']:
                Path(prepared['path']).unlink(missing_ok=True);return None
            Path(prepared['path']).replace(cached)
        return dict(source=str(cached),size=cached.stat().st_size,mime_type=DIRECTORY_MIME)
    return dict(source=str(path),size=path.stat().st_size,
                mime_type=entry.get('mime_type') or mimetypes.guess_type(entry['name'])[0] or 'application/octet-stream')


class ShelfPeerBridge(QObject):
    _indexed=Signal(object)

    def __init__(self,client,mesh):
        super().__init__(client);self.client=client;self.mesh=mesh;self.downloads={};self._seed_epoch=None
        self._index_queue=deque();self._index_cancel=threading.Event();self._indexing=False;self._cache_generation=0
        self._indexed.connect(self._index_done);mesh.offered.connect(self._offered)
        mesh.applied.connect(self._acknowledged);mesh.connected.connect(lambda _:self.seed())
        QTimer.singleShot(0,self.seed)

    def reset(self):
        for job in self.downloads.values():job.cancel.set()
        self.downloads.clear();self._index_cancel.set();self._index_cancel=threading.Event()
        self._index_queue.clear();self._indexing=False;self._seed_epoch=None
        self._cache_generation+=1

    def _valid(self,identity,digest,epoch):
        client=self.client
        if client.closed or client.epoch!=epoch or not client.upload_enabled or client.state is None:return False
        entry=client.state['entries'].get(identity)
        return bool(entry and not entry.get('deleted') and digest in (entry.get('sha256'),entry.get('pending_hash')))

    def publish(self,snapshot,*,committed=None):
        client=self.client;self._cache_generation+=1
        record={key:snapshot[key] for key in ('id','name','kind','size','sha256','mime_type')}
        record.update(version=committed['version'] if committed is not None else snapshot.get('version',0)+1,
                      provisional=committed is None,origin_device=client.service.device_id)
        source=snapshot['ref'] if snapshot['kind']=='file' and snapshot['mime_type']!=DIRECTORY_MIME else snapshot['path']
        if committed is not None and source==snapshot['path']:
            try:
                directory=_cache_directory(client.root/client.account_id)
                directory.mkdir(parents=True,exist_ok=True)
                cached=directory/(snapshot['sha256']+'.blob');Path(source).replace(cached)
            except (OSError,ValueError):
                LOG.exception('Cannot retain committed peer object %s',snapshot['id'])
            else:source=str(cached)
        epoch=client.epoch
        if committed is not None:QTimer.singleShot(0,self._clean_cache)
        return self.mesh.publish('shelf',record,source,valid=lambda:self._valid(record['id'],record['sha256'],epoch))

    def publish_removal(self,entry,*,committed=None):
        self._cache_generation+=1
        epoch=self.client.epoch
        record={key:entry[key] for key in ('id','name','kind')}
        record.update(size=0,sha256=EMPTY_SHA256,mime_type='application/octet-stream',deleted=True,
                      version=committed['version'] if committed is not None else entry.get('version',0)+1,
                      provisional=committed is None,origin_device=self.client.service.device_id)
        def valid():
            current=self.client.state['entries'].get(entry['id']) if self.client.state is not None else None
            return (not self.client.closed and self.client.epoch==epoch and self.client.upload_enabled
                    and (current is None or current.get('deleted')))
        self.mesh.publish('shelf',record,b'',valid=valid)
        QTimer.singleShot(0,self._clean_cache)

    def confirm(self,record):
        self.mesh.confirm('shelf',{key:record[key] for key in ('id','version','sha256','name')})

    def register_received(self,record,path):
        client=self.client;epoch=client.epoch;self._cache_generation+=1
        offered={key:record[key] for key in ('id','name','kind','size','sha256','mime_type','version')}
        offered.update(provisional=bool(record.get('_peer_provisional')),origin_device=record.get('origin_device',''))
        self.mesh.publish('shelf',offered,Path(path),valid=lambda:self._valid(record['id'],record['sha256'],epoch))

    def _offered(self,topic,record,peer):
        client=self.client
        if (topic!='shelf' or client.closed or not client.receive_enabled or client.state is None or not client.account_id
                or record.get('origin_device')==client.service.device_id):return
        if (not re.fullmatch('[A-Za-z0-9_-]{1,128}',record['id']) or type(record.get('version')) is not int
                or not 1<=record['version']<=2**53-1 or type(record.get('provisional')) is not bool
                or record.get('kind') not in ('file','text','url') or not isinstance(record.get('name'),str)
                or not 1<=len(record['name'])<=255 or any(ord(char)<32 for char in record['name'])
                or not isinstance(record.get('mime_type'),str) or len(record['mime_type'])>127
                or not isinstance(record.get('origin_device'),str) or len(record['origin_device'])>128
                or type(record.get('deleted',False)) is not bool):return
        if record.get('deleted') and (record['size']!=0 or record['sha256']!=EMPTY_SHA256):return
        clean={key:record[key] for key in ('id','version','name','kind','mime_type','size','sha256','origin_device')}
        clean.update(deleted=record.get('deleted',False),_peer_provisional=record['provisional'])
        def completed():
            entry=client.state['entries'].get(clean['id']) if client.state is not None else None
            if entry is not None and (clean['deleted'] and entry.get('deleted') or entry.get('sha256')==clean['sha256']):
                self.mesh.acknowledge(peer,'shelf',clean)
        pending=client.state.get('deferred_downloads',{}).get(clean['id'])
        if pending is not None and pending['record'].get('version',0)<=clean['version']:
            client.state['deferred_downloads'].pop(clean['id'],None)
        client._receive(clean,completed,provisional=record['provisional'])

    def _acknowledged(self,topic,record,peer):
        client=self.client
        if topic!='shelf' or client.state is None:return
        entry=client.state['entries'].get(record['id'])
        if entry is not None and entry.get('pending_hash')==record['sha256']:
            entry['peer_delivered_hash']=record['sha256'];client._notify_transfers()

    def download(self,record,target,callback,cloud):
        client=self.client;epoch=client.epoch;identity=record['id']
        def completed(result,error):
            if self.downloads.get(identity) is job:self.downloads.pop(identity,None)
            if client.closed or epoch!=client.epoch or not client.receive_enabled:return
            if error is None:callback(result,None)
            elif record.get('_peer_provisional'):callback(None,error)
            else:cloud()
        def progress(received,total):
            current=client.download_transfers.get(identity)
            if current is not None:current['_peer_received']=received;client._notify_transfers()
        job=self.mesh.fetch('shelf',record,target,completed,progress=progress)
        if job is None:
            if record.get('_peer_provisional'):callback(None,PeerUnavailable('源设备暂时无法提供此内容。'))
            else:cloud()
        else:self.downloads[identity]=job

    def cancel_download(self,identity):
        job=self.downloads.pop(identity,None)
        if job is not None:job.cancel.set()

    def seed(self):
        client=self.client
        if client.closed or not client.upload_enabled or client.state is None or not client.account_id:return
        if self._seed_epoch==client.epoch:return
        self._seed_epoch=client.epoch
        for entry in client.state['entries'].values():
            if entry.get('deleted') and entry.get('dirty'):self.publish_removal(entry)
        self._index_queue=deque(dict(entry) for entry in client.state['entries'].values()
            if not entry.get('deleted') and not entry.get('dirty') and re.fullmatch('[a-f0-9]{64}',str(entry.get('sha256',''))))
        self._index_next()

    def _index_next(self):
        if self._indexing:return
        if not self._index_queue:self._clean_cache();return
        client=self.client;entry=self._index_queue.popleft();epoch=client.epoch;cancel=self._index_cancel
        text=client.adapter.snapshot().get('texts',{}).get(entry['ref'])
        directory=client.root/client.account_id/'peer-cache';self._indexing=True
        def index():
            result=error=None
            try:result=_indexed_source(entry,text,_cache_directory(directory.parent),cancel)
            except (OSError,ValueError) as problem:error=problem
            self._indexed.emit(dict(epoch=epoch,entry=entry,result=result,error=error,cancel=cancel))
        if not self.mesh._submit(index):
            self._indexing=False;self._index_queue.appendleft(entry);QTimer.singleShot(50,self._index_next)

    def _index_done(self,event):
        client=self.client
        if event['epoch']!=client.epoch or event['cancel'] is not self._index_cancel:return
        self._indexing=False;entry=event['entry'];result=event['result']
        if event['error'] is not None:LOG.warning('Cannot index peer object %s: %s',entry['id'],event['error'])
        if result is not None and self._valid(entry['id'],entry['sha256'],client.epoch):
            record={key:entry[key] for key in ('id','name','kind','sha256','version')}
            record.update(size=result['size'],mime_type=result['mime_type'],provisional=bool(entry.get('peer_provisional')),
                          origin_device=entry.get('peer_origin',''))
            epoch=client.epoch
            self.mesh.publish('shelf',record,result['source'],valid=lambda:self._valid(entry['id'],entry['sha256'],epoch))
        self._index_next()

    def _clean_cache(self):
        client=self.client
        if client.state is None or not client.account_id:return
        generation=self._cache_generation;cancel=self._index_cancel;account=client.root/client.account_id
        keep={str(entry.get(key,'')) for entry in client.state['entries'].values() if not entry.get('deleted')
              for key in ('sha256','pending_hash')}
        def clean():
            try:
                directory=_cache_directory(account)
                if not directory.exists():return
                for path in directory.iterdir():
                    if cancel.is_set() or generation!=self._cache_generation:return
                    if (re.fullmatch('[a-f0-9]{64}\\.blob',path.name) and path.stem not in keep
                            and not path.is_symlink() and not path.is_junction()):
                        # Windows readers deny deletion until the current block closes.
                        with self.mesh._source_lock:
                            if cancel.is_set() or generation!=self._cache_generation:return
                            path.unlink(missing_ok=True)
            except (OSError,ValueError):LOG.warning('Cannot clean unused peer cache',exc_info=True)
        self.mesh._submit(clean)
