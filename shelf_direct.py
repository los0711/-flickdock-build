"""Reuse verified same-account file transfers for a newly dropped shelf file."""
from __future__ import annotations

from collections import deque
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
    path: str | None = None
    ready: object = None
    count: int = 0


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
    def __init__(self,shelf,wan):
        super().__init__(shelf)
        self.shelf=shelf
        self.wan=wan
        self._target=None
        self._pending_drops=set()
        self._queue=deque()
        self._outgoing=None
        self._incoming={}
        self._timer=QTimer(self);self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        wan.progress.connect(self._progress)
        wan.completed.connect(self._completed)

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
        self._pending_drops.clear()
        for _,path,_,_ in self._queue:self._discard(path.parent)
        self._queue.clear()
        for identifier in tuple(self._incoming):self._release(identifier,fallback=False,cancel=True)
        if self._outgoing is not None:self.wan.cancel(self._outgoing[0])
        self._target=None

    def close(self):
        self.change_account()
        self._timer.stop()
        self.wan.progress.disconnect(self._progress)
        self.wan.completed.disconnect(self._completed)

    def _discard(self,path):
        def done(_value,error):
            if error:_LOG.warning('Shelf direct cleanup failed (%s)',type(error).__name__)
        self.shelf._work(_remove_directory,(path,),done)

    def staged(self,entry,snapshot,continue_upload):
        if entry.pop('direct_drop_seq',None)!=snapshot.get('seq') or snapshot.get('version',0):
            continue_upload();return
        account=self.shelf.service
        peers=[peer['id'] for peer in self.wan.peers() if peer.get('online') and peer.get('account_device') is True
               and peer.get('account_device_id')!=account.device_id and _CAPABILITY in peer.get('features',())] if self._authenticated() else []
        if not peers or len(self._queue)>=_MAX_PENDING:
            continue_upload();return
        directory=self._account_root()/'direct-outbox'/uuid4().hex
        identity=str(self.shelf.account_id)
        info=dict(id=entry['id'],version=1,sha256=snapshot['sha256'],name=snapshot['name'],
                  mime_type=snapshot['mime_type'])
        def retained(path,error):
            if not error:
                self._queue.append((peers,path,info,identity))
                self._timer.start()
                self._tick()
            elif not isinstance(error,InterruptedError):
                _LOG.info('Shelf direct could not retain staged file (%s)',type(error).__name__)
            continue_upload()
        self.shelf._work(_retain_file,(Path(snapshot['path']),directory),retained)

    def offer(self,offer):
        """Returns True for internal offers, including rejected ones."""
        if 'shelf_intake' not in offer:return False
        identifier=offer['id'];info=offer['shelf_intake']
        size=offer.get('total')
        if not (isinstance(info,dict) and isinstance(size,int) and 0<=size<=MAX_FILE_BYTES
                and offer.get('count')==1 and self.shelf.receive_enabled and self._authenticated()
                and offer.get('account_id')==self.shelf.account_id and offer.get('account_device') is True
                and re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',str(info.get('id','')))
                and re.fullmatch('[a-f0-9]{64}',str(info.get('sha256',''))) and info.get('version')==1
                and isinstance(info.get('name'),str) and 0<len(info['name'])<=255
                and re.fullmatch(r'[A-Za-z0-9.+_-]+/[A-Za-z0-9.+_-]+',str(info.get('mime_type','')))):
            self.wan.reject(identifier);return True
        identity=info['id']
        entry=self.shelf.state['entries'].get(identity)
        ignored=self.shelf.state.get('ignored_versions',{}).get(identity,0)
        if (entry or ignored or identity in self.shelf._receive_fences
                or any(task.item_id==identity for task in self._incoming.values())
                or len(self._incoming)>=_MAX_PENDING):
            self.wan.reject(identifier);return True
        root=self._account_root()
        if self._target!=root:
            self._target=root
            self.shelf._work(_prune_direct_folders,(root,),lambda *_:None)
        path=root/'direct-inbox'/identifier
        record=dict(id=identity,version=1,sha256=info['sha256'],kind='file',size=size,
                    name=info['name'],mime_type=info['mime_type'])
        task=Incoming(identity,info['sha256'],size,record,path,monotonic()+_IDLE_SECONDS)
        self._incoming[identifier]=task
        self._timer.start()
        def directory_ready(result,error):
            if identifier not in self._incoming:
                if result:self._discard(result)
                return
            try:
                accepted=not error and self._authenticated() and self.wan.accept(identifier,result)
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
            def adopt(path):
                if not path:
                    cloud_download();return
                def promoted(value,error):
                    self._discard(task.directory)
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
        if cancel:self.wan.cancel(identifier)
        if success and task.ready:
            task.ready(task.path)
        else:
            self._discard(task.directory)
            if fallback and task.ready:task.ready(None)

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
                self._release(identifier);return
            received=result.get('received',0)
            if received>task.count:
                task.count=received
                task.deadline=monotonic()+_IDLE_SECONDS
        if self._outgoing and self._outgoing[0]==identifier and state in ('complete','error','cancelled'):
            self._outgoing=None

    def _tick(self):
        now=monotonic()
        for identifier,task in tuple(self._incoming.items()):
            if now>=task.deadline:self._release(identifier,cancel=task.path is None)
        while self._queue and self._outgoing is None:
            peers,path,info,account=self._queue[0]
            if not peers or account!=self.shelf.account_id or not self._authenticated():
                self._queue.popleft();self._discard(path.parent);continue
            try:
                identifier=self.wan.send(peers[0],[str(path)],shelf_intake=info)
            except ValueError:
                if sum(row.active and row.direction=='send' for row in self.wan._transfers.values())>=2:break
                peers.pop(0);continue
            peers.pop(0)
            self._outgoing=(identifier,path)
        if not self._queue and self._outgoing is None and not self._incoming:self._timer.stop()
