"""Bounded, display-only file information. Never decides transfer success."""
from collections import OrderedDict
from pathlib import Path
import queue,stat,threading,time,sys
from PySide6.QtCore import QObject,QTimer,Qt,Signal
from PySide6.QtWidgets import QApplication
from download_writer import _DiskPool

_pool=_DiskPool(2,'FlickDock file info')


def _local_volume(ref):
    """Only a confirmed local volume may turn missing info into removal."""
    if str(ref).startswith(('\\\\','//')):return False
    try:
        if sys.platform=='win32':
            import ctypes
            drive=ctypes.WinDLL('kernel32',use_last_error=True).GetDriveTypeW
            drive.argtypes=[ctypes.c_wchar_p];drive.restype=ctypes.c_uint
            return drive(str(Path(ref).anchor)) in (2,3,6)
        if sys.platform=='darwin':
            source=Path(ref)
            if source.is_symlink():return None
            import objc
            from Foundation import NSURL,NSURLVolumeIsLocalKey,NSURLVolumeURLKey
            parent=source.parent.resolve(strict=True)
            if not stat.S_ISDIR(parent.stat().st_mode):return None
            # Query the existing parent, never walk a disconnected share up to
            # an unrelated local ancestor. Release native objects on this worker.
            try:
                with objc.autorelease_pool():
                    url=NSURL.fileURLWithPath_(str(parent))
                    ok,value,_=url.getResourceValue_forKey_error_(None,NSURLVolumeIsLocalKey,None)
                    if not ok or value is None:return None
                    local=bool(value)
                    candidate=(parent/source.name).as_posix()
                    if local and candidate.startswith('/Volumes/'):
                        expected='/Volumes/'+candidate.split('/')[2]
                        ok,volume,_=url.getResourceValue_forKey_error_(None,NSURLVolumeURLKey,None)
                        if not ok or volume is None or str(volume.path()).rstrip('/')!=expected:return None
                    return local
            except (RuntimeError,objc.error):return None
        import os
        local=getattr(os,'ST_LOCAL',None)
        if local is not None:return bool(os.statvfs(Path(ref).parent).f_flag & local)
    except (ImportError,AttributeError,OSError,ValueError,TypeError):pass
    return None


class _Result(QObject):done=Signal(str,int,object)


class FileMetadataCache(QObject):
    ready=Signal(str)
    deferred=Signal(str)
    def __init__(self,parent=None):
        super().__init__(parent);self._cache=OrderedDict();self._waiting=OrderedDict();self._active={}
        self._tokens={};self._sequence=0;self.closed=False
        self._result=_Result(self);self._result.done.connect(self._completed,Qt.QueuedConnection)
        self._retry=QTimer(self);self._retry.setSingleShot(True);self._retry.timeout.connect(self._pump)
        QApplication.instance().aboutToQuit.connect(self.close)
        if parent is not None:parent.destroyed.connect(self.close)

    def request(self,ref):
        ref=str(ref);cached=self._cache.get(ref)
        if cached is not None:self._cache.move_to_end(ref)
        if (not self.closed and ref not in self._active and ref not in self._waiting
                and (cached is None or time.monotonic()-cached['checked_at']>5)):
            self._sequence+=1;self._tokens[ref]=self._sequence;self._waiting[ref]=self._sequence
            while len(self._waiting)>4096:
                retired,_=self._waiting.popitem(last=False);self._tokens.pop(retired,None);self.deferred.emit(retired)
            self._pump()
        return cached

    def _pump(self):
        if self.closed:return
        while self._waiting and len(self._active)<2:
            ref=next((value for value in self._waiting if value not in self._active),None)
            if ref is None:return
            generation=self._waiting.pop(ref);cancel=threading.Event();signals=self._result
            def read(ref=ref,generation=generation,cancel=cancel):
                value=dict(size=None,is_dir=False,exists=False,checked_at=time.monotonic())
                try:
                    if cancel.is_set():return
                    info=Path(ref).stat();value.update(size=info.st_size,is_dir=stat.S_ISDIR(info.st_mode),exists=True,mtime_ns=info.st_mtime_ns)
                except OSError as error:
                    value.update(error=type(error).__name__,errno=error.errno)
                    if error.errno in (2,20):value['local_volume']=_local_volume(ref)
                finally:
                    value['checked_at']=time.monotonic()
                    try:signals.done.emit(ref,generation,None if cancel.is_set() else value)
                    except RuntimeError:pass
            try:future=_pool.submit(read)
            except queue.Full:
                self._waiting[ref]=generation;self._waiting.move_to_end(ref,last=False);self._retry.start(200);return
            self._active[ref]=(generation,cancel,future)

    def _completed(self,ref,generation,value):
        active=self._active.get(ref)
        if active and active[0]==generation:self._active.pop(ref,None)
        if not self.closed and value is not None and self._tokens.get(ref)==generation:
            self._cache[ref]=value;self._cache.move_to_end(ref)
            while len(self._cache)>2048:
                retired,_=self._cache.popitem(last=False)
                if retired not in self._active and retired not in self._waiting:self._tokens.pop(retired,None)
            self.ready.emit(ref)
        self._pump()

    def invalidate(self,ref):
        ref=str(ref);self._tokens.pop(ref,None);self._cache.pop(ref,None);self._waiting.pop(ref,None)

    def refresh(self,ref):
        if self.closed:return
        ref=str(ref);self._sequence+=1;self._tokens[ref]=self._sequence;self._waiting[ref]=self._sequence
        if ref in self._cache:self._cache[ref]['checked_at']=0
        while len(self._waiting)>4096:
            retired,_=self._waiting.popitem(last=False);self._tokens.pop(retired,None);self.deferred.emit(retired)
        self._pump()

    def prime(self,ref,info):
        """Reuse a stat already required by intake validation; no new disk read."""
        ref=str(ref);self._tokens.pop(ref,None);self._waiting.pop(ref,None)
        self._cache[ref]=dict(size=info.st_size,is_dir=stat.S_ISDIR(info.st_mode),exists=True,
            mtime_ns=info.st_mtime_ns,checked_at=time.monotonic())
        self._cache.move_to_end(ref)
        while len(self._cache)>2048:
            retired,_=self._cache.popitem(last=False)
            if retired not in self._active and retired not in self._waiting:self._tokens.pop(retired,None)
        self.ready.emit(ref)

    def close(self):
        if self.closed:return
        self.closed=True;self._retry.stop();self._waiting.clear()
        for _,cancel,future in self._active.values():cancel.set();future.cancel()
        self._active.clear();self._cache.clear();self._tokens.clear()
