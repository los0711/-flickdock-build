"""Two small views of the same current clipboard; no second clipboard watcher."""
from pathlib import Path
from PySide6.QtCore import QObject,Signal,QThreadPool,QBuffer,QIODevice,QSize,Qt,QRectF
from PySide6.QtGui import QImage,QImageReader,QPainter,QPainterPath,QColor,QPen
from PySide6.QtWidgets import QPushButton,QLabel,QHBoxLayout,QPlainTextEdit,QStackedWidget
from app_settings import ReferenceModalDialog,SettingsCloseButton
from native_ui_common import colors,icon
from sync_files import DiskWork
from transfer_visuals import draw_cloud_status_badge,CloudStatusBadge,CLOUD_STATUS_LABELS
import ui_theme


def read_clip_image(value,size,cancel):
    if cancel.is_set() or value is None:return QImage()
    kind,data=value
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


class ClipboardPresenter(QObject):
    changed=Signal()
    def __init__(self,client,parent=None):
        super().__init__(parent);self.client=client;self.state={};self.thumbnail=QImage();self.large=QImage()
        self.pool=QThreadPool(self);self.pool.setMaxThreadCount(1);self.jobs=set();self.generation=0;self.closed=False
        self._large_requested=False;self._decoding=set()
        client.view_changed.connect(self._update);self._update(client.view)

    def _update(self,state):
        value=state.get('value');changed=state.get('revision')!=self.state.get('revision');self.state=dict(state)
        if changed:
            self.generation+=1;self.thumbnail=QImage();self.large=QImage();self._decoding.clear()
            for job in self.jobs:job.cancel.set()
            if value and value[0] in ('image','files'):self._decode(40)
            if self._large_requested:self.request_large()
        self.changed.emit()

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
        action=getattr(self.manager,'show_clipboard_preview',None)
        if action:action()

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


class ClipboardPreview(ReferenceModalDialog):
    def __init__(self,manager):
        super().__init__(manager.shelf,surface_width=600,modal=False)
        self.setWindowFlag(Qt.WindowStaysOnTopHint,True)
        self.manager=manager;self.presenter=manager.clipboard_presenter
        self.setWindowTitle('FlickDock · 剪贴板预览')
        head=QHBoxLayout();self.title=QLabel('剪贴板预览',self.body);head.addWidget(self.title,1)
        self.badge=CloudStatusBadge(self.body);head.addWidget(self.badge)
        self.close_button=SettingsCloseButton(self.body,glyph_size=14);self.close_button.setFixedSize(28,28)
        self.close_button.setAccessibleName('关闭剪贴板预览');self.close_button.setToolTip('关闭剪贴板预览')
        self.close_button.clicked.connect(self.close);head.addWidget(self.close_button)
        self.body_layout.addLayout(head)
        self.content=QStackedWidget(self.body);self.content.setMinimumHeight(260);self.body_layout.addWidget(self.content)
        self.text=QPlainTextEdit(self.content);self.text.setReadOnly(True);self.content.addWidget(self.text)
        from image_preview import ImagePreview
        self.image=ImagePreview(self.content);self.content.addWidget(self.image)
        self.info=QLabel(self.content);self.info.setWordWrap(True);self.info.setAlignment(Qt.AlignCenter);self.content.addWidget(self.info)
        self.status=QLabel(self.body);self.body_layout.addWidget(self.status)
        self.presenter.changed.connect(self.refresh);self.refresh()

    def refresh(self):
        model=self.presenter;value=model.state.get('value');phase=model.state.get('phase','local')
        c=colors(self.manager.settings.theme);self.set_surface_theme(self.manager.settings.theme)
        self.close_button.dark=self.manager.settings.theme=='dark'
        self.setStyleSheet(f"QDialog{{background:transparent;}} QLabel{{background:transparent;color:{c['text']};font-size:12px;}} QPlainTextEdit{{background:{c['card']};color:{c['text']};border:1px solid {c['border']};border-radius:8px;padding:10px;}} QPushButton#settingsClose{{background:transparent;border:0;border-radius:8px;padding:0;}} QPushButton#settingsClose:hover{{background:{c['hover']};}} QPushButton#settingsClose:pressed{{background:{c['control']};}}")
        self.badge.set_status(phase);self.status.setText(CLOUD_STATUS_LABELS.get(phase,'仅保存在本机'))
        if not value:self.info.setText('剪贴板暂无可预览内容');self.content.setCurrentWidget(self.info)
        elif value[0]=='text':
            text=value[1]['text']
            if len(text)>262144:
                text=text[:262144];self.status.setText(self.status.text()+' · 预览前 256K 字符，剪贴板保留完整内容')
            if self.text.toPlainText()!=text:self.text.setPlainText(text)
            self.content.setCurrentWidget(self.text)
        elif not model.large.isNull():
            self.image.image=model.large;self.image.update();self.content.setCurrentWidget(self.image)
        else:
            self.info.setText('正在准备图片预览…' if value[0]=='image' else '\n'.join(Path(path).name for path in value[1]))
            self.content.setCurrentWidget(self.info)

    def showEvent(self,event):
        self.presenter.request_large();self.refresh();super().showEvent(event)

    def closeEvent(self,event):
        self.presenter._large_requested=False;super().closeEvent(event)
