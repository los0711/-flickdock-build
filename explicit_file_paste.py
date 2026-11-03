"""Transfer file Paste owns an intent, never the system clipboard or cloud head.

The Windows HDROP fast path reads only native format metadata on the GUI.
Unknown aliases keep the legacy path until their native compatibility is
verified; this adapter does not classify an absent HDROP bit as non-file data.
"""
from __future__ import annotations
import sys
from uuid import uuid4
from PySide6.QtCore import QObject,QEvent
from clipboard_references import ReferenceReadPurpose


def windows_file_hint(provider):
    """Metadata only. This is neither an empty proof nor an upload authority."""
    before=provider.sequence()
    available=provider.available(15)
    after=provider.sequence()
    return before if type(before)is int and 0<before<=0xffffffff and before==after and available else None


class TransferFilePaste(QObject):
    def __init__(self,dialog,*,enabled=None,hint=None,reader_factory=None):
        super().__init__(dialog)
        if enabled is None:
            from platform_native import native_input_available
            enabled=sys.platform=='win32' and native_input_available()
        self.dialog=dialog;self.enabled=enabled;self._hint=hint
        self._reader_factory=reader_factory;self._reader=None;self._native=None
        self._request=None;self._generation=0;self._closed=False
        self.dialog.closing_intent.connect(self.invalidate)
        self.dialog.destroyed.connect(self.close)

    def _sequence_hint(self):
        if self._hint is not None:return self._hint()
        from win_clipboard_native import NativeClipboard
        if self._native is None:self._native=NativeClipboard()
        return windows_file_hint(self._native)

    def maybe_handle(self,event):
        if not self.enabled or self._closed:return None
        try:sequence=self._sequence_hint()
        except OSError:return None
        # Returning None preserves unverified aliases and native text controls;
        # only positively advertised local files enter the isolated fast path.
        if sequence is None:return None
        if event.type()==QEvent.Type.KeyPress and not event.isAutoRepeat():self.request(sequence)
        event.accept();return True

    def request(self,sequence):
        if self._closed:raise RuntimeError('Explicit paste owner is closed')
        if self._reader is None:
            if self._reader_factory is None:
                from win_clipboard_io import WindowsClipboardReader
                self._reader=WindowsClipboardReader(self)
            else:self._reader=self._reader_factory(self)
        self._reader.dispatch_ready()
        if self._closed or not self.dialog.isVisible():return
        self.invalidate()
        request=uuid4().hex;generation=self._generation
        purpose=ReferenceReadPurpose(request,sequence)
        # Publish intent before read(): a controlled backend may complete inline.
        self._request=(request,None)
        def completed(value,error):
            if self._closed or generation!=self._generation:return
            if self._request is None or self._request[0]!=request:return
            self._request=None
            from shiboken6 import isValid
            from PySide6.QtWidgets import QApplication
            if not isValid(self.dialog) or not self.dialog.isVisible() or QApplication.activeModalWidget() is not None:return
            if error is not None:raise RuntimeError('Reference reader returned an untyped error')
            if value['request']!=request:raise RuntimeError('Reference result crossed request ownership')
            if value['status']=='paths':self.dialog.panel.queue_paths(value['paths'])
            elif value['code'] not in ('CANCELLED','SUPERSEDED','CLOSED'):
                detail={'CHANGED':'剪贴板内容已变化，请重新粘贴',
                    'BUSY':'剪贴板正在使用，请重试粘贴',
                    'DEADLINE':'读取剪贴板文件超时，请重试粘贴',
                    'REFERENCE_LIMIT':'文件引用列表超出可用读取资源'}.get(value['code'],'未能读取剪贴板文件，请重新粘贴')
                self.dialog.panel.status_label.setText(detail)
        token=self._reader.read(completed,replace=True,purpose=purpose)
        if self._request is not None and self._request[0]==request:self._request=(request,token)

    def invalidate(self,*_):
        self._generation+=1
        request,self._request=self._request,None
        if request is not None and request[1] is not None:self._reader.cancel(request[1])

    def close(self,*_):
        if self._closed:return
        self.invalidate();self._closed=True
        if self._reader is not None:self._reader.close()
