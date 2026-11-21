"""Local-only file pins; no mutations of cloud metadata or transfer state."""
import json
from pathlib import Path
from PySide6.QtCore import QObject, QThreadPool, Qt, Signal
from PySide6.QtWidgets import QApplication
from account_store import atomic_json
from shelf_journal import ShelfJournal
from sync_files import DiskWork

MAX_PINS=2048


class FilePins(QObject):
    changed=Signal()
    error=Signal(str)
    def __init__(self,base,parent=None,*,autoload=True):
        super().__init__(parent);self.path=Path(base)/'file-pins.json';self.refs=set();self.ready=not autoload
        self.closed=False;self.writable=True;self._edits={};self.job=None
        self.pool=QThreadPool(self);self.pool.setMaxThreadCount(1)
        self.journal=ShelfJournal(atomic_json);self.journal.failed.connect(self._failed);self.journal.saved.connect(self._saved)
        app=QApplication.instance()
        if app:app.aboutToQuit.connect(self.close)
        if autoload:
            self.job=DiskWork(self._read);self.job.signals.done.connect(self._loaded,Qt.QueuedConnection);self.pool.start(self.job)
    def _read(self,cancel):
        try:
            with self.path.open('rb') as handle:raw=handle.read(1024*1024+1)
        except FileNotFoundError:return set()
        if len(raw)>1024*1024:raise ValueError('Pin store exceeds limit')
        data=json.loads(raw.decode('utf-8'))
        if not isinstance(data,dict) or data.get('schema_version')!=1 or not isinstance(data.get('refs'),list):raise ValueError('Invalid pin store')
        return {ref for ref in data['refs'][:MAX_PINS] if self.valid_ref(ref)}
    @staticmethod
    def valid_ref(ref):return isinstance(ref,str) and 0<len(ref)<=4096 and '\0' not in ref
    def _loaded(self,value,error):
        self.job=None
        if self.closed:return
        if error:
            self.writable=False;self.error.emit('星标记录无法读取，已保留原文件；本次星标仅在当前会话生效。')
        else:
            for ref,pinned in self._edits.items():
                if pinned:value.add(ref)
                else:value.discard(ref)
            self.refs=set(list(value)[:MAX_PINS])
        self.ready=True
        if self._edits:self._save()
        self._edits.clear();self.changed.emit()
    def contains(self,ref):return ref in self.refs
    def set(self,ref,pinned):
        if self.closed or not self.valid_ref(ref):return
        if pinned and ref not in self.refs and len(self.refs)>=MAX_PINS:
            self.error.emit('星标数量已达上限，请先取消部分星标。');return
        if pinned:self.refs.add(ref)
        else:self.refs.discard(ref)
        if not self.ready:self._edits[ref]=bool(pinned)
        else:self._save()
        self.changed.emit()
    def remap(self,mapping,*,notify=True):
        updated={mapping.get(ref,ref) for ref in self.refs}
        if updated==self.refs:return
        self.refs=updated
        if not self.ready:
            for old,new in mapping.items():self._edits[old]=False;self._edits[new]=new in updated
        else:self._save()
        if notify:self.changed.emit()
    def _save(self):
        if self.writable:self.journal.submit(self.path,dict(schema_version=1,refs=sorted(self.refs)))
    def _failed(self,path,reason):self.error.emit('星标记录暂未保存，请检查可用磁盘空间后重试。')
    def _saved(self,path,revision):
        if not self.closed:self.error.emit('')
    def close(self):
        if self.closed:return
        self.closed=True
        if self.job:self.job.cancel.set()
        self.pool.clear();self.pool.waitForDone(1500);self.journal.flush(1.5)
