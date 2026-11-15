"""Account-scoped clipboard receive journal; never persist clipboard bodies."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import threading
import time

from PySide6.QtCore import QObject, Signal
from account_store import atomic_json

MAX_BYTES=256*1024


def _empty():
    return dict(seen_version=0,fetched_version=0,applied_version=0,pending=None,needs_refresh=False)


def _merge(older,newer):
    """Merge startup disk state with events received before the disk read."""
    result=_empty()
    for key in ('seen_version','fetched_version','applied_version'):
        result[key]=max(*(value if type(value) is int and 0<=value<=2**53-1 else 0
                          for value in (older.get(key),newer.get(key))))
    candidates=[value for value in (older.get('pending'),newer.get('pending'))
                if isinstance(value,dict) and type(value.get('version')) is int
                and value['version']>result['applied_version'] and isinstance(value.get('id'),str)]
    result['pending']=copy.deepcopy(max(candidates,key=lambda value:value['version'])) if candidates else None
    result['needs_refresh']=bool(older.get('needs_refresh') or newer.get('needs_refresh'))
    return result


class ClipboardInbox(QObject):
    loaded=Signal()
    saved=Signal(int)
    failed=Signal(str)

    def __init__(self,base,parent=None):
        super().__init__(parent)
        self.path=Path(base)/'clipboard-inbox.json'
        self._lock=threading.Lock();self._wake=threading.Event()
        self._records={};self._revision=0;self._saved_revision=0
        self._delivery=None;self._delivery_revision=0;self._delivery_saved=0
        self.ready=False;self._closing=False;self._thread=None

    def start(self):
        # A closing app can return immediately while Python lets this short
        # writer drain its journal before process exit.
        self._thread=threading.Thread(target=self._run,name='FlickDock-clipboard-inbox',daemon=False)
        self._thread.start()

    def _emit(self,name,*values):
        try:getattr(self,name).emit(*values)
        except RuntimeError:pass  # The owner may have been deleted at shutdown.

    @property
    def saved_revision(self):
        with self._lock:return self._saved_revision

    def snapshot(self,account):
        with self._lock:return copy.deepcopy(self._records.get(account,_empty()))

    def save_delivery(self,value):
        with self._lock:
            self._delivery=copy.deepcopy(value);self._delivery_revision+=1;self._wake.set()

    def _change(self,account,update):
        if not isinstance(account,str) or not account or len(account)>256:return 0
        with self._lock:
            value=self._records.setdefault(account,_empty())
            before=copy.deepcopy(value);update(value)
            if value!=before:self._revision+=1;self._wake.set()
            return self._revision

    def received(self,account,head):
        version=head.get('version');identity=head.get('id')
        if type(version) is not int or not 1<=version<=2**53-1 or not isinstance(identity,str) or len(identity)!=36:return 0
        def update(row):
            row['seen_version']=max(row['seen_version'],version)
            previous=row.get('pending')
            if version>row['applied_version'] and (not previous or version>previous['version']):
                row['pending']=dict(version=version,id=identity,received_at=time.time(),stage='received',attempts=0,error='')
        return self._change(account,update)

    def hint(self,account):return self._change(account,lambda row:row.update(needs_refresh=True))
    def clear_hint(self,account):return self._change(account,lambda row:row.update(needs_refresh=False))

    def fetched(self,account,head):
        version=head.get('version',0)
        def update(row):
            row['fetched_version']=max(row['fetched_version'],version)
            pending=row.get('pending')
            if pending and (pending['id'],pending['version'])==(head.get('id'),version):pending.update(stage='fetched',error='')
        return self._change(account,update)

    def failure(self,account,head,error,attempts,*,terminal=False):
        def update(row):
            pending=row.get('pending')
            if pending and (pending['id'],pending['version'])==(head.get('id'),head.get('version')):
                pending.update(stage='needs_confirmation' if terminal else 'retrying',error=str(error)[:80],attempts=attempts)
        return self._change(account,update)

    def retry(self,account,head):
        def update(row):
            pending=row.get('pending')
            if pending and (pending['id'],pending['version'])==(head.get('id'),head.get('version')):
                pending.update(stage='received',error='',attempts=0)
        return self._change(account,update)

    def applied(self,account,head):
        version=head.get('version',0)
        if type(version) is not int or version<1:return 0
        def update(row):
            row['seen_version']=max(row['seen_version'],version)
            row['fetched_version']=max(row['fetched_version'],version)
            row['applied_version']=max(row['applied_version'],version)
            if row.get('pending') and row['pending']['version']<=version:row['pending']=None
        return self._change(account,update)

    def _run(self):
        try:
            try:
                with self.path.open('rb') as stream:raw=stream.read(MAX_BYTES+1)
                if len(raw)>MAX_BYTES:raise ValueError('clipboard inbox exceeds limit')
                parsed=json.loads(raw);disk=parsed.get('accounts',{})
                if not isinstance(disk,dict) or len(disk)>100:raise ValueError('invalid clipboard inbox')
            except FileNotFoundError:disk={}
            except (OSError,ValueError,TypeError) as error:
                disk={};self._emit('failed',type(error).__name__)
            with self._lock:
                for key,row in disk.items():
                    if isinstance(key,str) and len(key)<=256 and isinstance(row,dict):
                        self._records[key]=_merge(row,self._records.get(key,{}))
                self.ready=True
            self._emit('loaded')
            while True:
                self._wake.wait();self._wake.clear()
                with self._lock:
                    revision=self._revision;closing=self._closing
                    snapshot=copy.deepcopy(self._records)
                    delivery=copy.deepcopy(self._delivery);delivery_revision=self._delivery_revision
                if revision>self.saved_revision:
                    try:atomic_json(self.path,dict(schema=1,accounts=snapshot))
                    except OSError as error:
                        self._emit('failed',type(error).__name__)
                        if closing:return
                        self._wake.wait(.5);self._wake.set();continue
                    with self._lock:self._saved_revision=max(self._saved_revision,revision)
                    self._emit('saved',revision)
                if delivery is not None and delivery_revision>self._delivery_saved:
                    try:atomic_json(self.path.parent/'clipboard-delivery.json',delivery)
                    except OSError as error:
                        self._emit('failed',type(error).__name__)
                        if closing:return
                        self._wake.wait(.5);self._wake.set();continue
                    with self._lock:self._delivery_saved=max(self._delivery_saved,delivery_revision)
                if closing:return
        except RuntimeError:
            # Qt may already be shutting down while the daemon drains writes.
            return

    def close(self):
        with self._lock:self._closing=True
        self._wake.set()
