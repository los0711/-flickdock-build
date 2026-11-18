"""Independent speech download lane and atomic file commit off the GUI thread."""
import os,tempfile
from pathlib import Path
from PySide6.QtCore import QObject,QThreadPool,Qt,Signal
from sync_files import DiskWork


class SpeechExporter(QObject):
    error=Signal(str)
    def __init__(self,api,parent=None):
        super().__init__(parent);self.api=api;self.epoch=0;self.pending=None;self.closed=False
        self.pool=QThreadPool(self);self.pool.setMaxThreadCount(1);self.jobs=set();api.completed.connect(self._generated);api.reset.connect(self.cancel)
    def start(self,path,parts,language,voice,rate,pitch,callback):
        if self.closed:return False
        self.cancel();self.pending=dict(path=str(path),parts=parts,index=0,chunks=[],bytes=0,language=language,
            voice=voice,rate=rate,pitch=pitch,callback=callback);self._next();return True
    def _next(self):
        state=self.pending
        if state is None or self.closed:return
        if state['index']<len(state['parts']):
            self.api.synthesize(self.epoch,state['parts'][state['index']],state['language'],state['voice'],state['rate'],state['pitch']);return
        chunks=state['chunks'];path=Path(state['path']);epoch=self.epoch
        def write(cancel):
            temporary=None
            try:
                with tempfile.NamedTemporaryFile(dir=path.parent,prefix='.flickdock-speech-',suffix='.tmp',delete=False) as stream:
                    temporary=Path(stream.name)
                    for chunk in chunks:
                        if cancel.is_set():raise InterruptedError()
                        stream.write(chunk)
                    stream.flush();os.fsync(stream.fileno())
                if cancel.is_set():raise InterruptedError()
                os.replace(temporary,path);temporary=None;return str(path)
            finally:
                if temporary is not None:temporary.unlink(missing_ok=True)
        job=DiskWork(write);self.jobs.add(job)
        def finished(value,error):
            job.signals.done.disconnect(finished);self.jobs.discard(job)
            if self.closed or epoch!=self.epoch:return
            self.pending=None
            if error is not None:self.error.emit('语音文件未保存，原有文件已保留。')
            elif state['callback']:state['callback'](value)
        job.signals.done.connect(finished,Qt.QueuedConnection);self.pool.start(job)
    def _generated(self,epoch,value,error):
        state=self.pending
        if self.closed or state is None or epoch!=self.epoch:return
        if error:self.pending=None;self.error.emit('语音文件生成失败，请重试。');return
        state['bytes']+=len(value['audio'])
        if state['bytes']>64*1024*1024:self.cancel();self.error.emit('语音文件超过安全大小，请分段导出。');return
        state['chunks'].append(value['audio']);state['index']+=1;self._next()
    def cancel(self):
        self.epoch+=1;self.api.cancel_audio();self.pending=None
        for job in self.jobs:job.cancel.set()
    def close(self):
        if self.closed:return
        self.closed=True;self.cancel();self.api.close();self.pool.clear();self.pool.waitForDone(1500)
