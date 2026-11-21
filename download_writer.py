"""Bounded download disk lane. No file IO or hashing runs on Qt's GUI thread."""
from collections import deque
from concurrent.futures import Future
import hashlib
import os
from pathlib import Path
import re
import threading
import weakref
import queue
from PySide6.QtCore import QObject,Signal

_targets=weakref.WeakValueDictionary()
_targets_guard=threading.Lock()


class _DiskPool:
    """Fixed daemon workers; a blocked OS disk cannot keep app exit alive."""
    def __init__(self,count,name):
        self.count=count;self.name=name;self.queue=queue.Queue(maxsize=16)
        self.lock=threading.Lock();self.started=False

    def submit(self,action):
        future=Future()
        self.queue.put_nowait((future,action))
        with self.lock:
            if not self.started:
                self.started=True
                for index in range(self.count):
                    threading.Thread(target=self._run,name=f'{self.name} {index}',daemon=True).start()
        return future

    def _run(self):
        while True:
            future,action=self.queue.get()
            try:
                if future.set_running_or_notify_cancel():
                    try:future.set_result(action())
                    except BaseException as error:future.set_exception(error)
            finally:
                self.queue.task_done();del action,future


_disk_pool=_DiskPool(2,'FlickDock disk')
_clipboard_pool=_DiskPool(1,'FlickDock clipboard disk')


class _Target:
    def __init__(self):self.lock=threading.Lock();self.latest=None


class DownloadWriter(QObject):
    finished=Signal(object)
    CAPACITY=2*1024*1024

    def __init__(self,target,key,parent=None,*,priority='normal'):
        super().__init__(parent);self.target=Path(target)
        self.part=self.target.with_name('.'+self.target.name+'-'+key+'.part')
        self._condition=threading.Condition();self._queue=deque();self._buffered=0
        self._end=None;self._cancelled=False
        with _targets_guard:
            identity=os.path.normcase(os.path.abspath(target))
            self._target=_targets.get(identity) or _Target();_targets[identity]=self._target
            previous=self._target.latest
            self._target.latest=weakref.ref(self)
        try:self._future=(_clipboard_pool if priority=='high' else _disk_pool).submit(self._run)
        except BaseException:
            with _targets_guard:
                if self._target.latest and self._target.latest() is self:self._target.latest=previous
            self.setParent(None);self.deleteLater();raise

    def capacity(self):
        with self._condition:return max(0,self.CAPACITY-self._buffered)

    def write(self,chunk):
        with self._condition:
            if self._cancelled:return
            self._queue.append(chunk);self._buffered+=len(chunk);self._condition.notify()

    def finish(self,size,digest):
        with self._condition:self._end=(size,digest);self._condition.notify()

    def cancel(self):
        with self._condition:
            if self._cancelled:return
            self._cancelled=True;self._queue.clear();self._buffered=0;self._condition.notify()
        if self._future.cancel():self.finished.emit(None)

    def _run(self):
        stream=None;result=None;published=False
        try:
            if self._cancelled:return
            self.target.parent.mkdir(parents=True,exist_ok=True)
            stream=self.part.open('xb');digest=hashlib.sha256();received=0
            while True:
                with self._condition:
                    self._condition.wait_for(lambda:self._cancelled or self._queue or self._end is not None)
                    if self._cancelled:return
                    chunk=self._queue.popleft() if self._queue else None
                    end=self._end
                if chunk is None:
                    if end is not None:break
                    continue
                stream.write(chunk);digest.update(chunk);received+=len(chunk)
                with self._condition:self._buffered-=len(chunk)
            size,expected=end
            valid=bool(re.fullmatch(r'[0-9a-f]{64}',expected or ''))
            if received!=size or not valid or digest.hexdigest()!=expected:
                code=('DOWNLOAD_TRUNCATED' if received!=size else
                      'DOWNLOAD_HEADER_INVALID' if not valid else 'DOWNLOAD_HASH_MISMATCH')
                result=dict(code=code,received_size=received,expected_size=size,
                            hash_matches=digest.hexdigest()==expected,header_digest_valid=valid)
            else:
                stream.flush();os.fsync(stream.fileno());stream.close();stream=None
                # Only disk workers acquire this lock. Cancel never waits for
                # filesystem IO on Qt; its acknowledgement comes after this
                # worker finishes. A newer request for the same destination
                # cannot be overwritten by an older generation.
                with self._target.lock:
                    if self._cancelled or self._target.latest() is not self:
                        result=dict(code='cancelled');return
                    os.replace(self.part,self.target);published=True
                    if self._cancelled or self._target.latest() is not self:
                        self.target.unlink(missing_ok=True);published=False
                        result=dict(code='cancelled');return
                    result=dict(path=self.target,sha256=expected)
        except Exception as error:
            result=dict(code='DOWNLOAD_SAVE_FAILED',phase='download_save',cause=type(error).__name__)
            if isinstance(error,OSError) and isinstance(error.errno,int):result['errno']=error.errno
        finally:
            if stream is not None:
                try:stream.close()
                except OSError:pass
            if not published:
                try:self.part.unlink(missing_ok=True)
                except OSError:pass
            # The owner can be destroyed during application shutdown. All disk
            # cleanup still completes; no late signal may revive a request.
            try:self.finished.emit(result)
            except RuntimeError:pass
