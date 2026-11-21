"""Local history capture and the docked shelf's companion panel."""
from __future__ import annotations

import logging

from PySide6.QtCore import QMimeData, QObject, QPoint, QRect, QThreadPool, QUrl, Qt
from PySide6.QtGui import QDrag, QImage, QPixmap
from PySide6.QtWidgets import QApplication, QLineEdit, QPlainTextEdit, QTextEdit
from shiboken6 import isValid

from clipboard_history_native import front_target, insert_text, private_clipboard
from clipboard_history_panel import ClipboardHistoryPanel
from clipboard_history_store import ClipboardHistoryStore, PAGE
from sync_files import DiskWork


LOG=logging.getLogger(__name__)


def _execute(store,operation,args,cancel):
    if cancel.is_set():return None
    return store.execute(operation,*args)


class ClipboardHistory(QObject):
    def __init__(self,manager, directory):
        super().__init__(manager.app)
        self.manager=manager
        self.store=ClipboardHistoryStore(directory)
        self.panel=None
        self.pool=None
        self.jobs=set()
        self.enabled=False
        self.closed=False
        self.dragging=False
        self.entries=[]
        self.limit=PAGE
        self._refresh_epoch=0
        self._placement=None
        self._action_epoch=0
        manager.clipboard_sync.copy_observed.connect(self.observe)

    def set_enabled(self,enabled):
        enabled=bool(enabled)
        if self.enabled==enabled or self.closed:return
        self.enabled=enabled
        self._action_epoch+=1
        self._refresh_epoch+=1
        self.manager.clipboard_sync.set_local_observation(enabled,source='history')
        if enabled:
            self.limit=PAGE
            self._refresh()
        elif self.panel:
            self.panel.hide()

    def _submit(self,operation,args,callback):
        if self.closed or not self.enabled:return
        if self.pool is None:
            self.pool=QThreadPool(self);self.pool.setMaxThreadCount(1)
        job=DiskWork(_execute,self.store,operation,args)
        self.jobs.add(job)
        def done(result,error):
            self.jobs.discard(job)
            if self.closed:return
            if error:
                LOG.warning('Clipboard history %s failed (%s)',operation,type(error).__name__)
                if self.panel:self.panel.feedback('剪贴板历史暂不可用')
                return
            if self.enabled:callback(result)
        job.signals.done.connect(done,Qt.QueuedConnection)
        self.pool.start(job)

    def observe(self,value):
        if not self.enabled or not value:return
        if isinstance(value[1],dict) and value[1].get('_history_private'):return
        try:
            if private_clipboard():return
        except (OSError,RuntimeError,ImportError) as error:
            LOG.warning('Clipboard history privacy check failed (%s)',type(error).__name__)
            return
        # A blocked disk must not accumulate unbounded clipboard images.
        if len(self.jobs)>=24:
            if self.panel:self.panel.feedback('历史存储繁忙')
            return
        self._submit('remember',(value,),lambda key:self._refresh() if key else None)

    def _refresh(self):
        self._refresh_epoch+=1
        epoch=self._refresh_epoch
        def ready(rows):
            if epoch!=self._refresh_epoch:return
            self.entries=list(rows or ())
            if self.panel:self.panel.set_entries(self.entries[:self.limit],has_more=len(self.entries)>self.limit)
        self._submit('list',(self.limit+1,0),ready)

    def _change(self,operation,*args):
        self._action_epoch+=1
        self._submit(operation,args,lambda _:self._refresh())

    def _more(self):
        self.limit+=PAGE
        self._refresh()

    def _create_panel(self):
        panel=ClipboardHistoryPanel()
        panel.pinned.connect(lambda key:self._change('pin',key))
        panel.removed.connect(lambda key:self._change('remove',key))
        panel.clear_requested.connect(lambda:self._change('clear'))
        panel.more_requested.connect(self._more)
        panel.activated.connect(self._activate)
        panel.preview_requested.connect(self._preview)
        panel.dragged.connect(self._drag)
        panel.set_entries(self.entries[:self.limit],has_more=len(self.entries)>self.limit)
        self.panel=panel

    def _preview(self,key):
        def ready(value):
            if value is None or self.panel is None or not self.panel.isVisible():return
            kind,payload=value
            if kind in ('text','link'):self.panel.set_detail(key,payload['text'])
            elif kind=='file':self.panel.set_detail(key,'\n'.join(payload))
        self._submit('payload',(key,),ready)

    def sync(self,shelf,point,button_down):
        if not self.enabled:return
        valid=(shelf.isVisible() and shelf.presentation_kind=='dock' and not shelf.expanded
               and shelf.visible_state!='hiding' and not shelf.incoming_drop
               and not shelf.geometry_morph.active and not self.manager.settings_open()
               and not self.manager.outgoing_drag and not self.manager.edge_file_drag.prepared)
        if not valid or (button_down and not self.contains(point)):
            self.hide()
            return
        if self.panel is None:self._create_panel()
        anchor=QRect(shelf.card.mapToGlobal(QPoint()),shelf.card.size())
        area=self.manager.edge.display.availableGeometry()
        placement=(anchor,area)
        if placement!=self._placement or not self.panel.isVisible():
            self.panel.place(anchor,area)
            self._placement=placement
        self.panel.set_dark(self.manager.settings.theme=='dark')

    def contains(self,point):
        if self.panel is None or not self.panel.isVisible():return False
        if self.panel.contains_point(point):return True
        rect=self.panel.geometry()
        # Include only the narrow seam between the popup and the history panel.
        if self._placement is None:return False
        anchor=self._placement[0]
        seam=(QRect(rect.left(),anchor.bottom(),rect.width(),9) if rect.top()>anchor.bottom()
              else QRect(rect.left(),anchor.top()-9,rect.width(),9))
        return seam.contains(point)

    def _activate(self,key):
        target=front_target()
        if self.panel is None:return
        editor=QApplication.focusWidget() if target is None else None
        self._action_epoch+=1; epoch=self._action_epoch
        def ready(value):
            if not self.panel.isVisible() or epoch!=self._action_epoch or value is None:return
            kind,payload=value
            try:
                if target is None and editor is not None and isValid(editor) and editor is QApplication.focusWidget():
                    pasted=self._insert_local(editor,kind,payload)
                else:pasted=kind in ('text','link') and insert_text(payload['text'],target)
            except (OSError,RuntimeError,ImportError,ValueError,TypeError) as error:
                LOG.warning('Clipboard history text insertion failed (%s)',type(error).__name__)
                pasted=False
            if pasted:
                self.hide()
                self.manager.shelf.hide_animated(explicit=True)
            elif kind in ('image','file'):self.panel.feedback('拖入目标窗口')
            else:self.panel.feedback('请先聚焦目标输入框，或拖放')
        self._submit('payload',(key,),ready)

    @staticmethod
    def _insert_local(editor,kind,payload):
        if not isinstance(editor,(QLineEdit,QPlainTextEdit,QTextEdit)) or editor.isReadOnly():return False
        if kind in ('text','link'):
            if isinstance(editor,QLineEdit):editor.insert(payload['text'])
            else:editor.insertPlainText(payload['text'])
            return True
        if kind=='image' and isinstance(editor,QTextEdit) and editor.acceptRichText():
            image=QImage.fromData(payload)
            if image.isNull():return False
            editor.textCursor().insertImage(image)
            return True
        return False

    def _drag(self,key):
        def ready(value):
            left_down=QApplication.mouseButtons()&Qt.LeftButton
            if value is None or not left_down or self.panel is None or not self.panel.isVisible():return
            kind,payload=value
            mime=QMimeData()
            if kind=='image':
                image=QImage.fromData(payload)
                if image.isNull():return
                mime.setImageData(image)
            elif kind=='file':
                urls=[QUrl.fromLocalFile(path) for path in payload]
                if not urls:return
                mime.setUrls(urls)
            else:
                mime.setText(payload['text'])
                if payload.get('html'):mime.setHtml(payload['html'])
                if kind=='link':mime.setUrls([QUrl(payload['text'].strip())])
            drag=QDrag(self.panel)
            drag.setMimeData(mime)
            if kind=='image':
                drag.setPixmap(QPixmap.fromImage(image.scaled(88,65,Qt.KeepAspectRatio,Qt.SmoothTransformation)))
            self.dragging=self.manager.outgoing_drag=True
            try:
                drag.exec(Qt.CopyAction,Qt.CopyAction)
            finally:
                self.dragging=self.manager.outgoing_drag=False
                drag.deleteLater()
        self._submit('payload',(key,),ready)

    def hide(self):
        if self.panel is not None and self.panel.isVisible() and not self.dragging:
            self.panel.hide()
            self._action_epoch+=1

    def close(self):
        if self.closed:return
        self.closed=True
        self.manager.clipboard_sync.copy_observed.disconnect(self.observe)
        if self.panel is not None:
            self.panel.close()
            self.panel.deleteLater()
        if self.pool is not None:self.pool.waitForDone(1500)
