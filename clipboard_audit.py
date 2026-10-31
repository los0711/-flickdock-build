"""Bounded, payload-free clipboard provenance. Never persists the HMAC key."""
from __future__ import annotations
import atexit
from collections import deque
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import threading


class ClipboardAudit:
    def __init__(self, base, *, max_bytes=1024*1024, capacity=256):
        self.path=Path(base)/'diagnostics'/'clipboard-writes.jsonl'
        self.max_bytes=max_bytes;self.capacity=capacity
        self._key=os.urandom(32);self._condition=threading.Condition()
        self._queue=deque();self._closed=False;self._last_write=None;self.dropped=0
        self._thread=threading.Thread(target=self._run,name='clipboard-audit',daemon=True)
        self._thread.start()

    def describe(self, mime):
        """Fingerprint native payload, not private file bytes or file names in logs."""
        digest=hmac.new(self._key,digestmod=hashlib.sha256)
        def add(value):
            raw=value.encode('utf8') if isinstance(value,str) else value
            digest.update(len(raw).to_bytes(8,'big'));digest.update(raw)
        kind='empty'
        if mime is not None:
            if mime.hasUrls():
                kind='files' if any(url.isLocalFile() for url in mime.urls()) else 'link'
                for url in mime.urls():add(url.toString())
            elif mime.hasImage():
                kind='image'
                for format in ('image/png','image/jpeg','image/webp','image/bmp','image/tiff'):
                    raw=bytes(mime.data(format))
                    if raw:add(raw);break
                else:
                    from PySide6.QtGui import QImage,QPixmap
                    image=mime.imageData()
                    if isinstance(image,QPixmap):image=image.toImage()
                    if isinstance(image,QImage) and not image.isNull():
                        image=image.convertToFormat(QImage.Format.Format_RGBA8888)
                        add(str((image.width(),image.height())));add(image.constBits())
            elif mime.hasText():
                text=mime.text();kind='link' if text.startswith(('https://','http://')) else 'text'
                add(text)
                if mime.hasHtml():add(mime.html())
            else:kind='unsupported'
        add(kind)
        return kind,digest.hexdigest()

    def fingerprint(self,parts):
        """Reuse the sync engine's existing digest; never reread an image/file."""
        digest=hmac.new(self._key,digestmod=hashlib.sha256)
        def add(value):
            if isinstance(value,(tuple,list)):
                digest.update(b'[')
                for part in value:add(part)
                digest.update(b']');return
            raw=value if isinstance(value,(bytes,memoryview)) else str(value).encode('utf8')
            digest.update(len(raw).to_bytes(8,'big'));digest.update(raw)
        add(parts);return digest.hexdigest()

    def record(self, mime=None, *, module, function, trigger, origin, task_id='', operation='write', result='ok',
               content_type=None, fingerprint_data=None):
        try:
            if fingerprint_data is not None:kind,fingerprint=content_type,self.fingerprint(fingerprint_data)
            else:kind,fingerprint=self.describe(mime)
            # Task identifiers come from server/local UUIDs; never accept a payload as an ID.
            from uuid import UUID
            try:task_id=str(UUID(task_id)) if task_id else ''
            except (ValueError,TypeError,AttributeError):task_id=''
            with self._condition:
                if self._closed:return
                duplicate=operation=='write' and self._last_write==fingerprint
                if operation=='write':self._last_write=fingerprint
                event=dict(time=datetime.now(timezone.utc).isoformat(),pid=os.getpid(),
                    module=module,function=function,trigger=trigger,origin=origin,
                    content_type=kind,fingerprint=fingerprint,task_id=task_id,
                    operation=operation,result=result,duplicate=duplicate,dropped=self.dropped)
                if len(self._queue)>=self.capacity:self.dropped+=1;return
                self._queue.append(event);self._condition.notify()
        except Exception:
            # Diagnostics must never break the system's original Copy/Paste operation.
            pass

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda:self._queue or self._closed)
                if not self._queue:return
                event=self._queue.popleft()
            try:
                self.path.parent.mkdir(parents=True,exist_ok=True)
                line=json.dumps(event,ensure_ascii=True,separators=(',',':'))+'\n'
                if self.path.exists() and self.path.stat().st_size+len(line)>self.max_bytes:
                    self.path.replace(self.path.with_suffix('.previous.jsonl'))
                with self.path.open('a',encoding='utf8') as stream:stream.write(line)
            except OSError:
                with self._condition:self.dropped+=1

    def close(self, timeout=.5):
        with self._condition:self._closed=True;self._condition.notify_all()
        if threading.current_thread() is not self._thread:self._thread.join(timeout)


_audit=None
_lock=threading.Lock()


def start(base):
    global _audit
    with _lock:
        if _audit is None:_audit=ClipboardAudit(base)
        return _audit


def stop():
    global _audit
    with _lock:audit,_audit=_audit,None
    if audit is not None:audit.close()


def observe(clipboard, **context):
    if _audit is not None:
        try:_audit.record(None if context.get('fingerprint_data') is not None else clipboard.mimeData(),**context)
        except Exception:pass


def set_mime(clipboard, mime, **context):
    try:
        clipboard.setMimeData(mime)
    except Exception:
        observe(clipboard,**context,result='failed');raise
    else:observe(clipboard,**context)


def set_text(clipboard, text, **context):
    try:
        clipboard.setText(text)
    except Exception:
        observe(clipboard,**context,result='failed');raise
    else:observe(clipboard,**context)


def clear(clipboard, **context):
    try:
        clipboard.clear()
    except Exception:
        observe(clipboard,**context,result='failed');raise
    else:observe(clipboard,**context)


atexit.register(stop)
