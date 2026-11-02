"""Two small views of the same current clipboard; no second clipboard watcher."""
from pathlib import Path
from PySide6.QtCore import QObject,Signal,QThreadPool,QBuffer,QIODevice,QSize,Qt,QRectF,QPoint
from PySide6.QtGui import QImage,QImageReader,QPainter,QPainterPath,QColor,QPen,QPixmap
from PySide6.QtWidgets import QPushButton,QLabel,QHBoxLayout,QPlainTextEdit,QStackedWidget,QDialog,QWidget
from app_settings import ReferenceModalDialog,SettingsCloseButton
from native_ui_common import colors,icon
from sync_files import DiskWork
from transfer_visuals import draw_cloud_status_badge,CloudStatusBadge,CLOUD_STATUS_LABELS
import ui_theme


def read_clip_image(value,size,cancel):
    if cancel.is_set() or value is None:return QImage()
    kind,data=value
    content=data.get('local_content') if kind=='image' else None
    if content is not None:
        with content.acquire() as original:
            part=next(part for part in original.parts if part['name']=='image')
            return read_clip_image(('files',[part['path']]),size,cancel)
    if kind=='image' and data.get('image') is not None:
        return data['image'].scaled(size,size,Qt.KeepAspectRatio,Qt.SmoothTransformation)
    buffer=None
    if kind=='image':
        buffer=QBuffer();buffer.setData(data.get('raw',b''));buffer.open(QIODevice.ReadOnly);reader=QImageReader(buffer)
    elif kind=='files':reader=QImageReader(data[0])
    else:return QImage()
    dimensions=reader.size()
    if not dimensions.isValid() or dimensions.width()*dimensions.height()>100_000_000:return QImage()
    reader.setAutoTransform(True);reader.setScaledSize(dimensions.scaled(QSize(size,size),Qt.KeepAspectRatio))
    image=reader.read()
    return QImage() if cancel.is_set() else image


def read_clip_text(content,cancel):
    """Read only the established preview budget; keep complete originals."""
    with content.acquire() as original:
        part=next(part for part in original.parts if part['name']=='text')
        if cancel.is_set():return None
        with open(part['path'],'r',encoding=part['encoding'],errors='replace',newline='') as source:
            text=source.read(262145).split('\0',1)[0]
        return None if cancel.is_set() else (text[:262144],len(text)>262144)


class ClipboardPresenter(QObject):
    changed=Signal()
    def __init__(self,client,parent=None):
        super().__init__(parent);self.client=client;self.state={};self.thumbnail=QImage();self.large=QImage()
        self.pool=QThreadPool(self);self.pool.setMaxThreadCount(1);self.jobs=set();self.generation=0;self.closed=False
        self._large_requested=False;self._decoding=set();self.text_preview=None;self.text_truncated=False;self.text_error=''
        client.view_changed.connect(self._update);self._update(client.view)

    def _update(self,state):
        value=state.get('value');changed=state.get('revision')!=self.state.get('revision');self.state=dict(state)
        if changed:
            self.generation+=1;self.thumbnail=QImage();self.large=QImage();self._decoding.clear()
            self.text_preview=None;self.text_truncated=False;self.text_error=''
            for job in self.jobs:job.cancel.set()
            if value and value[0] in ('image','files'):self._decode(40)
            if value and value[0]=='text' and value[1].get('local_content') is not None:self._decode_text(value[1]['local_content'])
            if self._large_requested:self.request_large()
        self.changed.emit()

    def _decode_text(self,content):
        if self.closed:return
        generation=self.generation;job=DiskWork(read_clip_text,content);self.jobs.add(job)
        def ready(value,error):
            self.jobs.discard(job)
            if self.closed or generation!=self.generation:return
            if error or value is None:
                if not job.cancel.is_set():self.text_error='无法读取剪贴板预览';self.changed.emit()
                return
            self.text_preview,self.text_truncated=value;self.changed.emit()
        job.signals.done.connect(ready);self.pool.start(job,-1)

    def _decode(self,size):
        value=self.state.get('value');generation=self.generation
        key=(generation,size)
        if self.closed or key in self._decoding:return
        self._decoding.add(key)
        job=DiskWork(read_clip_image,value,size);self.jobs.add(job)
        def ready(image,error):
            self.jobs.discard(job);self._decoding.discard(key)
            if self.closed or generation!=self.generation or error or not isinstance(image,QImage):return
            if size==40:self.thumbnail=image
            else:self.large=image
            self.changed.emit()
        job.signals.done.connect(ready);self.pool.start(job,-1)

    def request_large(self):
        self._large_requested=True
        value=self.state.get('value')
        if value and value[0] in ('image','files') and self.large.isNull():self._decode(1400)

    def close(self):
        self.closed=True
        for job in self.jobs:job.cancel.set()


class ClipboardButton(QPushButton):
    def __init__(self,manager,parent=None):
        super().__init__(parent);self.manager=manager;self.presenter=None
        self.setFixedSize(32,32);self.setAutoDefault(False);self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName('放大查看剪贴板');self.clicked.connect(self._open);self.refresh()

    def _open(self):
        dialog=getattr(self.manager,'clipboard_preview_dialog',None)
        needs_permission=bool(self.presenter is not None and self.presenter.state.get('permission_required'))
        if not needs_permission and dialog is not None and getattr(dialog,'owner_window',None) is self.window() and dialog._popup_motion.source is self and dialog._popup_motion.target:
            dialog.dismiss();return
        action=getattr(self.manager,'show_clipboard_preview',None)
        if action:
            from inspect import signature
            try:signature(action).bind(self)
            except (TypeError,ValueError):action()
            else:action(self)

    def refresh(self):
        presenter=getattr(self.manager,'clipboard_presenter',None)
        if presenter is not self.presenter:
            if self.presenter is not None:
                try:self.presenter.changed.disconnect(self.refresh)
                except RuntimeError:pass
            self.presenter=presenter
            if presenter is not None:presenter.changed.connect(self.refresh)
        state=presenter.state if presenter is not None else {}
        self.setToolTip('剪贴板 · '+('需要设置 Mac 剪贴板权限，点击前往' if state.get('permission_required') else
            CLOUD_STATUS_LABELS.get(state.get('phase'),'仅保存在本机') if state.get('value') else '暂无可预览内容'))
        self.update()

    def paintEvent(self,event):
        from reference_widgets import ink
        c=colors(ui_theme.current_theme());p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(ink(c['border']),1));p.setBrush(ink(c['hover'] if self.underMouse() or self.isDown() else c['control']))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(.5,.5,-.5,-.5),8,8)
        model=self.presenter;value=model.state.get('value') if model else None
        if model and not model.thumbnail.isNull():
            clip=QPainterPath();clip.addRoundedRect(QRectF(5,5,22,22),4,4);p.setClipPath(clip)
            image=model.thumbnail;size=image.size().scaled(QSize(22,22),Qt.KeepAspectRatio)
            p.drawImage(QRectF(16-size.width()/2,16-size.height()/2,size.width(),size.height()),image);p.setClipping(False)
        else:
            glyph='text' if value and value[0]=='text' else 'file' if value else 'copy'
            icon(glyph,c['secondary']).paint(p,7,7,18,18)
        if model and (value or model.state.get('permission_required')):
            draw_cloud_status_badge(p,QRectF(21,21,10,10),'error' if model.state.get('permission_required') else model.state.get('phase','local'))
        p.end()


class _PreviewText(QPlainTextEdit):
    def sizeHint(self):
        # This is a compact, read-only preview, not a full text editor.
        # Qt's editor default varies by platform/font and needlessly grows
        # the popup when the sync-limit explanation is present.
        return QSize(super().sizeHint().width(),160)


class _PreviewContent(QStackedWidget):
    def sizeHint(self):
        # A never-shown stacked page can retain a stale QWidgetItem hint.
        # Read each real page before reserving the fixed animation host, so
        # late image decode cannot introduce a scrollbar at the live handoff.
        result=super().sizeHint()
        for index in range(self.count()):
            result=result.expandedTo(self.widget(index).sizeHint())
        return result


class ClipboardPreview(ReferenceModalDialog):
    def __init__(self,manager):
        super().__init__(manager.shelf,surface_width=420,modal=False)
        self.setWindowFlag(Qt.WindowStaysOnTopHint,True)
        self.manager=manager;self.presenter=manager.clipboard_presenter
        self.setWindowTitle('FlickDock · 剪贴板预览')
        head=QHBoxLayout();self.title=QLabel('剪贴板预览',self.body);head.addWidget(self.title,1)
        self.badge=CloudStatusBadge(self.body);head.addWidget(self.badge)
        self.close_button=SettingsCloseButton(self.body,glyph_size=14);self.close_button.setFixedSize(28,28)
        self.close_button.setAccessibleName('关闭剪贴板预览');self.close_button.setToolTip('关闭剪贴板预览')
        self.close_button.clicked.connect(self.close);head.addWidget(self.close_button)
        self.body_layout.addLayout(head)
        self.content=_PreviewContent(self.body);self.content.setMinimumHeight(160);self.body_layout.addWidget(self.content)
        self.text=_PreviewText(self.content);self.text.setReadOnly(True);self.text.setTextInteractionFlags(Qt.NoTextInteraction)
        self.text.setFocusPolicy(Qt.NoFocus);self.text.setContextMenuPolicy(Qt.NoContextMenu);self.content.addWidget(self.text)
        from input_focus import install_focus
        install_focus(self.text,theme=lambda:self.manager.settings.theme)
        from image_preview import ImagePreview
        self.image=ImagePreview(self.content);self.image.setFocusPolicy(Qt.NoFocus);self.image.setContextMenuPolicy(Qt.NoContextMenu);self.content.addWidget(self.image)
        self.info=QLabel(self.content);self.info.setWordWrap(True);self.info.setAlignment(Qt.AlignCenter);self.content.addWidget(self.info)
        self.status=QLabel(self.body);self.body_layout.addWidget(self.status)
        self.sync_limit_note=QLabel('仅支持同步 100 MB 以内的剪贴板内容。\n超过 100 MB 的内容不会同步给其他设备。',self.body)
        self.sync_limit_note.setWordWrap(True);self.sync_limit_note.setTextFormat(Qt.PlainText)
        self.sync_limit_note.setTextInteractionFlags(Qt.NoTextInteraction);self.sync_limit_note.setFocusPolicy(Qt.NoFocus)
        self.body_layout.addWidget(self.sync_limit_note)
        # Retain the original content/scroll tree, but lay its surface out at
        # one final size inside a larger fixed native presentation reservation.
        from shiboken6 import delete
        self.layout().removeWidget(self.surface);delete(self.layout())
        self._window_fade.automatic=False;self.removeEventFilter(self._window_fade)
        from clipboard_popup_motion import ClipboardPopupMotion
        self._popup_motion=ClipboardPopupMotion(self);self._closing_result=QDialog.Rejected
        self.presenter.changed.connect(self.refresh);self.refresh()

    @property
    def owner_window(self):return self._popup_motion.owner

    def open_from(self,source=None):
        if source is None:
            shelf=self.manager.shelf;popup=getattr(shelf,'popup',None)
            source=getattr(popup,'clipboard_button',None) if shelf.expanded and popup is not None else shelf.zones['permanent'].clipboard_button
        self._popup_motion.open(source)

    def show(self):self.open_from()

    def _show_host(self):QDialog.show(self)

    def _complete_close(self):
        self.presenter._large_requested=False;QDialog.done(self,self._closing_result)

    def dismiss(self,*,immediate=False):self._popup_motion.dismiss(immediate=immediate)

    def contains_global(self,point):return self._popup_motion.contains_global(point)

    def done(self,result):
        self._closing_result=result;self.dismiss()

    def adjustSize(self):
        if not hasattr(self,'_popup_motion'):return super().adjustSize()
        self._layout_surface()

    def sizeHint(self):
        hint=super().sizeHint()
        if not hasattr(self,'body_layout'):return hint
        width=getattr(self,'_presentation_width',hint.width()-40)
        # QLabel's natural wrapped hint can assume a narrower width than
        # this surface (notably with macOS font metrics). Reserve its real
        # height for this width, not that heuristic's extra blank lines.
        height=self.body_layout.totalHeightForWidth(width)
        if height<0:height=hint.height()-40
        return QSize(width+40,max(height,self.body_layout.minimumSize().height())+40)

    def _layout_surface(self):
        if self.surface.layout():self.surface.layout().activate()
        self.body_layout.activate()

    def _fit_preview_layout(self,width,height):
        # Fit before reserving the native animation host. On short screens
        # keep the explanation visible rather than making a click-to-dismiss
        # preview depend on scrolling to reach its final line.
        self._presentation_width=width
        self.body_layout.setContentsMargins(20,20,20,20);self.body_layout.setSpacing(16)
        if self.sizeHint().height()-40>height:
            self.body_layout.setContentsMargins(16,12,16,12);self.body_layout.setSpacing(12)

    def _capture_surface(self):
        self._layout_surface();dpr=self.devicePixelRatioF()
        image=QPixmap(round(self.surface.width()*dpr),round(self.surface.height()*dpr));image.setDevicePixelRatio(dpr);image.fill(Qt.transparent)
        p=QPainter(image);p.setRenderHint(QPainter.Antialiasing)
        rect=QRectF(self.surface.rect()).adjusted(.5,.5,-.5,-.5)
        p.setBrush(QColor('#1C1C1E' if self._surface_dark else '#FFFFFF'))
        import sys
        radius=16 if sys.platform=='darwin' else 10
        p.setPen(QPen(QColor(255,255,255,20) if self._surface_dark else QColor(0,0,0,23),1));p.drawRoundedRect(rect,radius,radius)
        self.surface.render(p,QPoint(),renderFlags=QWidget.DrawChildren);p.end();return image

    def refresh(self):
        model=self.presenter;value=model.state.get('value');phase=model.state.get('phase','local')
        c=colors(self.manager.settings.theme);self.set_surface_theme(self.manager.settings.theme)
        self.close_button.dark=self.manager.settings.theme=='dark'
        self.setStyleSheet(f"QDialog{{background:transparent;}} QLabel{{background:transparent;color:{c['text']};font-size:12px;}} QPlainTextEdit{{background:{c['card']};color:{c['text']};border:1px solid {c['border']};border-radius:8px;padding:10px;}} QPushButton#settingsClose{{background:transparent;border:0;border-radius:8px;padding:0;}} QPushButton#settingsClose:hover{{background:{c['hover']};}} QPushButton#settingsClose:pressed{{background:{c['control']};}}")
        self.badge.set_status(phase);self.status.setText(model.state.get('error_message') or CLOUD_STATUS_LABELS.get(phase,'仅保存在本机'))
        self.sync_limit_note.setStyleSheet(f"color:{c['secondary']};font-size:11px;background:transparent;")
        if not value:self.info.setText('剪贴板暂无可预览内容');self.content.setCurrentWidget(self.info)
        elif value[0]=='text':
            local=value[1].get('local_content') is not None
            text=model.text_preview if local else value[1]['text']
            if text is None:
                self.info.setText(model.text_error or '正在准备文本预览…');self.content.setCurrentWidget(self.info)
                if hasattr(self,'_popup_motion'):self._popup_motion.refresh_snapshot()
                return
            if len(text)>262144 or local and model.text_truncated:
                text=text[:262144];self.status.setText(self.status.text()+' · 预览前 256K 字符，剪贴板保留完整内容')
            if self.text.toPlainText()!=text:self.text.setPlainText(text)
            self.content.setCurrentWidget(self.text)
        elif not model.large.isNull():
            self.image.image=model.large;self.image.update();self.content.setCurrentWidget(self.image)
        else:
            self.info.setText('正在准备图片预览…' if value[0]=='image' else '\n'.join(Path(path).name for path in value[1]))
            self.content.setCurrentWidget(self.info)
        if hasattr(self,'_popup_motion'):self._popup_motion.refresh_snapshot()

    def showEvent(self,event):
        self.presenter.request_large();self.refresh();QDialog.showEvent(self,event)
        owner=self.owner_window
        if owner is not None and self.windowHandle() and owner.windowHandle():self.windowHandle().setTransientParent(owner.windowHandle())
        from platform_native import remove_host_window_shadow
        remove_host_window_shadow(self)

    def paintEvent(self,event):
        if hasattr(self,'_popup_motion') and self._popup_motion.transitioning:
            painter=QPainter(self);self._popup_motion.paint(painter);painter.end()
        else:super().paintEvent(event)

    def closeEvent(self,event):
        event.ignore();self.done(QDialog.Rejected)
