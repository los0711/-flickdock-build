"""Local history capture and the docked shelf's companion panel."""
from __future__ import annotations

import logging
import time

from PySide6.QtCore import QMimeData, QObject, QPoint, QRect, QThreadPool, QTimer, QUrl, Qt
from PySide6.QtGui import QDrag, QImage, QPixmap
from PySide6.QtWidgets import QApplication, QLineEdit, QPlainTextEdit, QTextEdit
from shiboken6 import isValid

from clipboard_history_native import (front_target, insert_image, insert_text, mac_paste_allowed,
                                      private_clipboard, restore_mac_target)
from clipboard_history_panel import ClipboardHistoryPanel
from clipboard_history_store import ClipboardHistoryStore, PAGE
from sync_files import DiskWork
from ui_transitions import motion_allowed


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
        self.total=0
        self.limit=PAGE
        self._refresh_epoch=0
        self._placement=None
        self._companion_shelf=None
        self._action_epoch=0
        self._insert_job=None
        self._foreground_target=None
        self._pointer_inside=False
        from screenshot_history import SavedScreenshotHistory
        self.screenshots=SavedScreenshotHistory(self,base=directory)
        self.screenshots.received.connect(self._saved_screenshot)
        self.screenshots.failed.connect(self._screenshot_error)
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
            self.screenshots.start()
        else:
            self.screenshots.stop()
            if self._insert_job is not None:self._insert_job.cancel.set()
            self.hide()

    def _submit(self,operation,args,callback):
        return self._work(DiskWork(_execute,self.store,operation,args),operation,callback)

    def _work(self,job,operation,callback):
        if self.closed or not self.enabled:return None
        if self.pool is None:
            self.pool=QThreadPool(self);self.pool.setMaxThreadCount(1)
        self.jobs.add(job)
        def done(result,error):
            self.jobs.discard(job)
            if self.closed:return
            if error:
                LOG.warning('Clipboard history %s failed (%s)',operation,type(error).__name__,
                            exc_info=(type(error),error,error.__traceback__))
                if self.panel:self.panel.feedback('剪贴板历史暂不可用')
                return
            if self.enabled:callback(result)
        job.signals.done.connect(done,Qt.QueuedConnection)
        self.pool.start(job)
        return job

    def set_screenshot_folders(self,folders):
        from screenshot_history import save_folders
        def ready(saved):
            if saved and self.enabled:
                self.screenshots.stop();self.screenshots.start()
        self._work(DiskWork(save_folders,self.store.root,folders),'screenshot-folders',ready)

    def _saved_screenshot(self,value):
        if self.enabled and not self.closed:
            self._submit('remember',(value,),lambda key:self._refresh() if key else None)

    def _screenshot_error(self,message):
        if self.panel:self.panel.feedback(message)

    def observe(self,value):
        if not self.enabled or not value:return
        if isinstance(value[1],dict) and value[1].get('_history_private'):return
        try:
            if private_clipboard(image=value[0]=='image'):return
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
        def ready(page):
            if epoch!=self._refresh_epoch or page is None:return
            self.entries,self.total=page
            if self.panel is None:self._create_panel()
            else:self.panel.set_entries(self.entries,total=self.total)
        self._submit('page',(self.limit,0),ready)

    def _change(self,operation,*args):
        self._action_epoch+=1
        self._submit(operation,args,lambda _:self._refresh())

    def _more(self):
        self.limit+=PAGE
        self._refresh()

    def _create_panel(self):
        if self.panel is not None:return
        panel=ClipboardHistoryPanel()
        panel.pinned.connect(lambda key:self._change('pin',key))
        panel.removed.connect(lambda key:self._change('remove',key))
        panel.clear_requested.connect(lambda:self._change('clear'))
        panel.more_requested.connect(self._more)
        panel.activated.connect(self._activate)
        panel.preview_requested.connect(self._preview)
        panel.dragged.connect(self._drag)
        panel.set_entries(self.entries,total=self.total)
        self.panel=panel

    def _preview(self,key):
        def ready(value):
            if value is None or self.panel is None or not self.panel.isVisible():return
            kind,payload=value
            if kind in ('text','link'):self.panel.set_detail(key,payload['text'])
            elif kind=='file':self.panel.set_detail(key,'\n'.join(payload))
        self._submit('payload',(key,),ready)

    def _alpha(self,alpha,closing):
        if self.manager.motion and motion_allowed():
            return alpha
        return 0. if closing else 1.

    def sync(self,shelf,point=None):
        if not self.enabled:return False
        valid=(shelf.isVisible() and shelf.presentation_kind=='dock' and not shelf.expanded
               and not shelf._dock_drag_reveal and not shelf.incoming_drop and not shelf.geometry_morph.active and not self.manager.settings_open()
               and not self.manager.outgoing_drag and not self.manager.edge_file_drag.prepared)
        if not valid:
            self.hide()
            return False
        if shelf.visible_state=='hiding':
            return point is not None and self.contains(point)
        if self.panel is None:self._create_panel()
        self.panel.set_dark(self.manager.settings.theme=='dark')
        anchor=QRect(shelf.card.mapToGlobal(QPoint()),shelf.card.size())
        screen=QApplication.screenAt(anchor.center()) or self.manager.edge.display
        area=screen.availableGeometry()
        placement=(anchor,area)
        if placement!=self._placement or shelf is not self._companion_shelf:
            if self.panel._cocoa_hover:self._foreground_target=front_target()
            alpha=self._alpha(shelf.presentation.values['alpha'],False)
            if self.panel.place(anchor,area,shelf_alpha=alpha,motion=shelf.presentation.values,front=shelf):
                self._placement=placement
                self._companion_shelf=shelf
            else:
                self._placement=None
                self._companion_shelf=None
        if point is None:return False
        self.panel.reconcile_pointer(point)
        inside=self.contains(point)
        if inside and not self._pointer_inside and self.panel._cocoa_hover:
            current=front_target()
            if current is not None:self._foreground_target=current
        self._pointer_inside=inside
        return inside

    def follow_shelf_presentation(self,shelf,alpha,values):
        if self.enabled and self.panel is not None and shelf is self._companion_shelf:
            closing=shelf.visible_state=='hiding'
            self.panel.follow_shelf(self._alpha(alpha,closing),closing,motion=values,front=shelf)

    def contains(self,point):
        return (self.panel is not None and self._placement is not None
                and self.panel.contains_companion(point))

    def _activate(self,key):
        if self.panel is None:return
        target=front_target()
        if self._insert_job is not None:self._insert_job.cancel.set()
        editor=QApplication.focusWidget() if target is None else None
        recover_focus=(target is None and self.panel._cocoa_hover and
                       not isinstance(editor,(QLineEdit,QPlainTextEdit,QTextEdit)))
        if recover_focus:target=self._foreground_target
        self._action_epoch+=1; epoch=self._action_epoch
        def complete(pasted,kind):
            if epoch!=self._action_epoch or not self.panel.isVisible():return
            if pasted:
                self.manager.shelf.hide_animated(explicit=True)
            elif kind in ('image','file'):self.panel.feedback('拖入目标窗口')
            else:self.panel.feedback('请先聚焦目标输入框，或拖放')
        def ready(value):
            if not self.panel.isVisible() or epoch!=self._action_epoch or value is None:return
            kind,payload=value
            if target is None and editor is not None and isValid(editor) and editor is QApplication.focusWidget():
                complete(self._insert_local(editor,kind,payload),kind)
            elif target is None or kind not in ('text','link','image'):complete(False,kind)
            else:
                if self.panel._cocoa_hover and not mac_paste_allowed():
                    self.panel.feedback('请完成 Mac 粘贴授权')
                    show=getattr(self.manager,'show_mac_permissions',None)
                    if show is not None:QTimer.singleShot(0,self,show)
                    return
                insert,data=(insert_image,payload) if kind=='image' else (insert_text,payload['text'])
                def dispatch(deadline):
                    if self.closed or epoch!=self._action_epoch or not self.panel.isVisible():return
                    current=front_target()
                    if current!=target:
                        if recover_focus and current is None and time.monotonic()<deadline:
                            QTimer.singleShot(24,self,lambda:dispatch(deadline))
                        else:complete(False,kind)
                        return
                    job=DiskWork(lambda cancelled:insert(data,target,cancel=cancelled))
                    def finished(pasted):
                        if self._insert_job is job:self._insert_job=None
                        complete(pasted,kind)
                    self._insert_job=self._work(job,'insert',finished)
                waiting=recover_focus and front_target() is None and restore_mac_target(target)
                dispatch(time.monotonic()+(.40 if waiting else 0.))
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
        if self.dragging:return
        self._companion_shelf=None
        self._placement=None
        self._foreground_target=None
        self._pointer_inside=False
        if self.panel is not None and self.panel.isVisible():
            self.panel.hide()
            self._action_epoch+=1

    def close(self):
        if self.closed:return
        self.closed=True
        self.screenshots.close()
        if self._insert_job is not None:self._insert_job.cancel.set()
        self.manager.clipboard_sync.copy_observed.disconnect(self.observe)
        if self.panel is not None:
            self.panel.close()
            self.panel.deleteLater()
        if self.pool is not None:self.pool.waitForDone(1500)
