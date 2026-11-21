"""Reuse verified same-account file transfers for a newly dropped shelf file."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
from pathlib import Path
import os
import re
from shutil import rmtree
from time import monotonic, time
from uuid import uuid4

from PySide6.QtCore import QObject,QTimer

from sync_files import MAX_FILE_BYTES,_source_reader


_LOG=logging.getLogger(__name__)
_CAPABILITY='shelf-drop-v1'
_IDLE_SECONDS=35.
_LAN_HANDOFF_SECONDS=10.
_CACHED_SECONDS=600.
_MAX_PENDING=32


@dataclass
class Incoming:
    item_id: str
    digest: str
    size: int
    record: dict
    directory: Path
    deadline: float
    service: object
    sender: str
    path: str | None = None
    ready: object = None
    count: int = 0


@dataclass
class Outbox:
    path: Path
    info: dict
    pending: dict[str, bool]
    in_flight: set[str]


def _make_directory(path,cancel):
    if cancel.is_set():raise InterruptedError()
    path.mkdir(parents=True,exist_ok=False)
    return path


def _retain_file(path,directory,cancel):
    if cancel.is_set():raise InterruptedError()
    directory.mkdir(parents=True,exist_ok=False)
    try:
        result=directory/'shelf-file.blob'
        result.hardlink_to(path)
        return result
    except OSError:
        directory.rmdir()
        raise


def _promote_file(source,target,record,cancel):
    source,target=Path(source),Path(target)
    count=0
    sha=hashlib.sha256()
    with _source_reader(source) as stream:
        while chunk:=stream.read(1024*1024):
            if cancel.is_set():raise InterruptedError()
            count+=len(chunk)
            if count>record['size']:raise ValueError('搁架直传文件大小不匹配。')
            sha.update(chunk)
    if count!=record['size'] or sha.hexdigest()!=record['sha256']:
        raise ValueError('搁架直传文件校验不匹配。')
    if cancel.is_set():raise InterruptedError()
    target.parent.mkdir(parents=True,exist_ok=True)
    os.replace(source,target)
    return target


def _remove_directory(directory,_cancel):
    try:rmtree(directory)
    except FileNotFoundError:return


def _prune_direct_folders(root,cancel):
    for kind in ('direct-inbox','direct-outbox'):
        base=root/kind
        if not base.is_dir() or base.is_symlink():continue
        for folder in base.iterdir():
            if cancel.is_set():return
            if not re.fullmatch('[a-f0-9]{32}',folder.name) or folder.is_symlink() or not folder.is_dir():continue
            if time()-folder.stat().st_mtime>86400:_remove_directory(folder,cancel)


class ShelfDirectDelivery(QObject):
    def __init__(self,shelf,wan,lan=None):
        super().__init__(shelf)
        self.shelf=shelf
        self.wan=wan
        self.lan=lan
        self._target=None
        self._pending_drops=set()
        self._queue=[]
        self._retaining=0
        self._epoch=0
        self._closed=False
        self._outgoing={}
        self._incoming={}
        self._timer=QTimer(self);self._timer.setInterval(250)
        self._timer.timeout.connect(self._tick)
        for service in (wan,lan):
            if service is not None:
                service.progress.connect(self._progress)
                service.completed.connect(self._completed)

    def mark_drop(self,refs):
        if self.shelf.upload_enabled and self.shelf.account_id:
            for ref in refs:
                if len(self._pending_drops)>=_MAX_PENDING:break
                self._pending_drops.add(ref)

    def local_entry(self,entry):
        if entry['ref'] not in self._pending_drops:return
        self._pending_drops.discard(entry['ref'])
        if entry.get('kind')=='file' and not entry.get('version') and not entry.get('manual_upload_only'):
            entry['direct_drop_seq']=entry['seq']

    def _account_root(self):
        return self.shelf.root/self.shelf.account_id

    def _authenticated(self):
        account=self.shelf.service
        return bool(self.shelf.account_id and getattr(account,'signed_in',False)
                    and self.wan._bound_identity==(account.device_id,account.api.token))

    def change_account(self):
        self._epoch+=1
        self._pending_drops.clear()
        for batch in tuple(self._queue):
            batch.pending.clear()
            self._retire(batch)
        for identifier in tuple(self._incoming):self._release(identifier,fallback=False,cancel=True)
        for identifier,(service,_,_,_) in tuple(self._outgoing.items()):
            service.cancel(identifier)
        self._target=None

    def close(self):
        if self._closed:return
        self._closed=True
        self.change_account()
        self._timer.stop()
        for service in (self.wan,self.lan):
            if service is not None:
                service.progress.disconnect(self._progress)
                service.completed.disconnect(self._completed)

    def _discard(self,path):
        def done(_value,error):
            if error:_LOG.warning('Shelf direct cleanup failed (%s)',type(error).__name__)
        self.shelf._work(_remove_directory,(path,),done)

    def _prepare_account_root(self):
        root=self._account_root()
        if self._target!=root:
            self._target=root
            self.shelf._work(_prune_direct_folders,(root,),lambda *_:None)
        return root

    def staged(self,entry,snapshot,continue_upload):
        if entry.pop('direct_drop_seq',None)!=snapshot.get('seq') or snapshot.get('version',0):
            continue_upload();return
        peers=self._online_peers() if not self._closed and self._authenticated() else {}
        if not peers or len(self._queue)+self._retaining>=_MAX_PENDING:
            continue_upload();return
        directory=self._prepare_account_root()/'direct-outbox'/uuid4().hex
        epoch=self._epoch
        info=dict(id=entry['id'],version=1,sha256=snapshot['sha256'],name=snapshot['name'],
                  mime_type=snapshot['mime_type'])
        self._retaining+=1
        def retained(path,error):
            self._retaining-=1
            if epoch!=self._epoch:
                if path:self._discard(path.parent)
                return
            if path and not error:
                self._queue.append(Outbox(path,info,dict.fromkeys(peers,False),set()))
                self._timer.start()
                self._tick()
            elif not isinstance(error,InterruptedError):
                _LOG.info('Shelf direct could not retain staged file (%s)',type(error).__name__)
            continue_upload()
        self.shelf._work(_retain_file,(Path(snapshot['path']),directory),retained)

    def _online_peers(self):
        local=self.shelf.service.device_id
        return {peer['id']:peer for peer in self.wan.peers() if peer.get('online') and peer.get('account_device') is True
                and peer.get('account_device_id')!=local and _CAPABILITY in peer.get('features',())}

    def offer_lan(self,offer):
        return self.offer(offer,service=self.lan)

    def offer(self,offer,*,service=None):
        """Returns True for internal offers, including rejected ones."""
        if 'shelf_intake' not in offer:return False
        service=service if service is not None else self.wan
        decline=service.decline_shelf_offer if service is self.lan else service.reject
        identifier=offer['id'];info=offer['shelf_intake']
        size=offer.get('total')
        if not (not self._closed and isinstance(info,dict) and isinstance(size,int) and 0<=size<=MAX_FILE_BYTES
                and offer.get('count')==1 and self.shelf.receive_enabled and self._authenticated()
                and offer.get('account_id')==self.shelf.account_id and offer.get('account_device') is True
                and re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',str(info.get('id','')))
                and re.fullmatch('[a-f0-9]{64}',str(info.get('sha256',''))) and info.get('version')==1
                and isinstance(info.get('name'),str) and 0<len(info['name'])<=255
                and re.fullmatch(r'[A-Za-z0-9.+_-]+/[A-Za-z0-9.+_-]+',str(info.get('mime_type','')))):
            decline(identifier);return True
        identity=info['id']
        entry=self.shelf.state['entries'].get(identity)
        ignored=self.shelf.state.get('ignored_versions',{}).get(identity,0)
        previous_id,previous=next(((key,task) for key,task in self._incoming.items() if task.item_id==identity),(None,None))
        # The same authenticated sender may switch to P2P after losing LAN.
        # Transfer any waiting cloud callback to the new receiver, preserving
        # its receive fence while the old socket and staging are closed.
        handoff=bool(previous is not None and previous.service is self.lan and service is self.wan
                     and previous.sender and previous.sender==offer.get('peer_id') and not previous.path
                     and (previous.digest,previous.size)==(info['sha256'],size))
        if (entry or ignored or ((identity in self.shelf._receive_fences or previous is not None) and not handoff)
                or (len(self._incoming)>=_MAX_PENDING and not handoff)):
            decline(identifier);return True
        root=self._prepare_account_root()
        path=root/'direct-inbox'/identifier
        record=dict(id=identity,version=1,sha256=info['sha256'],kind='file',size=size,
                    name=info['name'],mime_type=info['mime_type'])
        task=Incoming(identity,info['sha256'],size,record,path,monotonic()+_IDLE_SECONDS,service,offer.get('peer_id',''))
        if handoff:
            task.ready=previous.ready
            self._release(previous_id,fallback=False,cancel=True)
        self._incoming[identifier]=task
        self._timer.start()
        def directory_ready(result,error):
            if identifier not in self._incoming:
                if result:self._discard(result)
                return
            try:
                accepted=not error and self._authenticated() and service.accept(identifier,result)
            except (OSError,ValueError) as reason:
                _LOG.info('Shelf direct could not accept offer (%s)',type(reason).__name__)
                accepted=False
            if not accepted:self._release(identifier,cancel=True)
        self.shelf._work(_make_directory,(path,),directory_ready)
        return True

    def consume(self,record,target,downloaded,cloud_download):
        for identifier,task in self._incoming.items():
            if (task.item_id,task.digest,task.size,1)!=(record['id'],record['sha256'],record['size'],record['version']):
                continue
            def adopt(path,directory):
                if not path:
                    cloud_download();return
                def promoted(value,error):
                    self._discard(directory)
                    if error:cloud_download()
                    else:downloaded(value,None)
                self.shelf._work(_promote_file,(path,target,record),promoted)
            task.ready=adopt
            if task.path:self._release(identifier,success=True)
            return True
        return False

    def _release(self,identifier,*,success=False,fallback=True,cancel=False):
        task=self._incoming.pop(identifier,None)
        if task is None:return
        if cancel:task.service.cancel(identifier)
        if success and task.ready:
            task.ready(task.path,task.directory)
        else:
            self._discard(task.directory)
            if fallback and task.ready:task.ready(None,task.directory)

    def _completed(self,result):
        identifier=result['id']
        task=self._incoming.get(identifier)
        if task is None:return
        paths=result.get('paths',[])
        if result.get('direction')!='receive' or len(paths)!=1:
            self._release(identifier);return
        path=Path(paths[0])
        if path.parent!=task.directory:
            self._release(identifier);return
        task.path=str(path)
        task.deadline=monotonic()+_CACHED_SECONDS
        if task.ready:
            self._release(identifier,success=True)
        elif self._authenticated() and self.shelf.receive_enabled:
            self.shelf._receive(task.record,lambda:None,peer_only=True)

    def _progress(self,result):
        identifier=result['id'];state=result['state']
        task=self._incoming.get(identifier)
        if task is not None:
            if state in ('error','cancelled'):
                if task.service is self.lan and state=='error':
                    task.deadline=monotonic()+_LAN_HANDOFF_SECONDS
                else:
                    self._release(identifier)
                return
            received=result.get('received',0)
            if received>task.count:
                task.count=received
                task.deadline=monotonic()+_IDLE_SECONDS
        if identifier in self._outgoing and state in ('complete','error','cancelled'):
            service,batch,peer,epoch=self._outgoing.pop(identifier)
            batch.in_flight.discard(identifier)
            if (service is self.lan and state=='error' and result.get('phase')!='declined'
                    and epoch==self._epoch and not self._closed):
                batch.pending[peer]=True
            self._retire(batch)
            self._tick()

    def _retire(self,batch):
        if not batch.pending and not batch.in_flight and batch in self._queue:
            self._queue.remove(batch)
            self._discard(batch.path.parent)

    def _send_pending(self,batch,online):
        for peer,fallback in tuple(batch.pending.items()):
            if peer not in online:
                del batch.pending[peer]
                continue
            nearby=self.wan.nearby_peer(peer) if self.lan is not None and not fallback else None
            service,target=(self.lan,nearby['id']) if nearby and nearby.get('shelf_drop') else (self.wan,peer)
            if not service.has_send_capacity():continue
            try:
                identifier=service.send(target,[str(batch.path)],shelf_intake=batch.info)
            except ValueError:
                if service is self.lan:
                    batch.pending[peer]=True
                else:
                    del batch.pending[peer]
                continue
            del batch.pending[peer]
            batch.in_flight.add(identifier)
            self._outgoing[identifier]=(service,batch,peer,self._epoch)
        self._retire(batch)

    def _tick(self):
        now=monotonic()
        for identifier,task in tuple(self._incoming.items()):
            if now>=task.deadline:self._release(identifier,cancel=task.path is None)
        online=self._online_peers() if not self._closed and self._authenticated() else {}
        for batch in tuple(self._queue):
            self._send_pending(batch,online)
        if not self._queue and self._outgoing is None and not self._incoming:self._timer.stop()
