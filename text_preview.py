"""Bounded file text previews; disk reads never run in a widget callback."""
import codecs,queue,threading
from pathlib import Path
from PySide6.QtCore import QObject,QTimer,Qt,Signal
from file_metadata import _pool

TEXT_PREVIEW_BYTES=64*1024

def read_text_preview(ref,cancel):
    if cancel.is_set():return None
    try:
        with Path(ref).open('rb') as stream:data=stream.read(TEXT_PREVIEW_BYTES+1)
        truncated=len(data)>TEXT_PREVIEW_BYTES;data=data[:TEXT_PREVIEW_BYTES]
        try:text=codecs.getincrementaldecoder('utf-8-sig')().decode(data,final=not truncated)
        except UnicodeDecodeError:text=data.decode('gb18030',errors='replace')
        return None if cancel.is_set() else dict(text=text,truncated=truncated)
    except OSError as error:return dict(error=type(error).__name__)

class TextPreviewLoader(QObject):
    ready=Signal(str,object)
    _result=Signal(str,int,object)
    def __init__(self,parent):
        super().__init__(parent);self._epoch=0;self._cancel=threading.Event();self._pending=None
        self._retry=QTimer(self);self._retry.setSingleShot(True);self._retry.timeout.connect(self._submit)
        self._result.connect(self._completed,Qt.QueuedConnection)
        parent.destroyed.connect(self.cancel)

    def cancel(self,*_):
        self._epoch+=1;self._cancel.set();self._retry.stop();self._pending=None

    def request(self,ref):
        self.cancel();self._cancel=threading.Event()
        self._pending=(str(ref),self._epoch,self._cancel);self._submit()

    def _submit(self):
        if self._pending is None:return
        ref,epoch,cancel=self._pending;signal=self._result
        def read():
            value=read_text_preview(ref,cancel)
            if not cancel.is_set():signal.emit(ref,epoch,value)
        try:_pool.submit(read)
        except queue.Full:self._retry.start(100);return
        self._pending=None

    def _completed(self,ref,epoch,value):
        if epoch==self._epoch and not self._cancel.is_set():self.ready.emit(ref,value)
