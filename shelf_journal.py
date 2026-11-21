"""Ordered, asynchronous persistence for shelf state snapshots.

Each profile has a monotonically increasing revision.  A remote mutation may
start only after the snapshot on which it depends has reached atomic_json.
The shared coordinator also makes rapid account switching and replacement of
the ShelfSync instance see queued snapshots rather than an older disk file.
"""
from collections import OrderedDict
import copy
from pathlib import Path
import threading
import time

from PySide6.QtCore import QObject, Signal

_coordinators={}
_registry_lock=threading.Lock()


def coordinator(root,writer):
    key=str(Path(root).resolve())
    with _registry_lock:
        result=_coordinators.get(key)
        if result is None:
            result=ShelfJournal(writer);_coordinators[key]=result
        return result


class ShelfJournal(QObject):
    saved=Signal(str,int)
    failed=Signal(str,str)

    def __init__(self,writer):
        super().__init__()
        self._writer=writer;self._condition=threading.Condition()
        self._latest={};self._written={};self._pending=OrderedDict()
        self._revision=0;self._thread=None

    def submit(self,path,value):
        path=str(path);snapshot=copy.deepcopy(value)
        with self._condition:
            self._revision+=1;revision=self._revision
            self._latest[path]=(revision,snapshot)
            self._pending[path]=(revision,snapshot)
            self._start_locked()
            return revision

    def _start_locked(self):
        if self._thread is None:
            self._thread=threading.Thread(target=self._run,name='FlickDock-shelf-journal',daemon=False)
            self._thread.start()
        self._condition.notify_all()

    def retry(self):
        with self._condition:
            if self._pending:self._start_locked()

    def latest(self,path):
        with self._condition:
            row=self._latest.get(str(path))
            return copy.deepcopy(row[1]) if row else None

    def written(self,path):
        with self._condition:return self._written.get(str(path),0)

    def required(self,path):
        with self._condition:
            row=self._latest.get(str(path))
            return row[0] if row else 0

    def flush(self,timeout):
        deadline=time.monotonic()+timeout
        with self._condition:
            while self._thread is not None and time.monotonic()<deadline:
                self._condition.wait(deadline-time.monotonic())
            return not self._pending and self._thread is None

    def _emit(self,name,*args):
        try:getattr(self,name).emit(*args)
        except RuntimeError:pass

    def _run(self):
        failed=False
        try:
            while True:
                with self._condition:
                    if not self._pending:return
                    path,(revision,value)=self._pending.popitem(last=False)
                try:self._writer(Path(path),value)
                except Exception as error:
                    failed=True
                    with self._condition:
                        current=self._pending.get(path)
                        if current is None or current[0]<revision:self._pending[path]=(revision,value)
                    self._emit('failed',path,type(error).__name__)
                    return
                with self._condition:
                    self._written[path]=max(self._written.get(path,0),revision)
                self._emit('saved',path,revision)
        finally:
            with self._condition:
                self._thread=None
                if self._pending and not failed:self._start_locked()
                self._condition.notify_all()
