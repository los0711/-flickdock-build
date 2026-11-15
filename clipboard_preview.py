"""Two small views of the same current clipboard; no second clipboard watcher."""
from pathlib import Path
from PySide6.QtCore import QObject,Signal,QThreadPool,QBuffer,QIODevice,QSize,Qt,QRectF,QEvent,QMimeData
from PySide6.QtGui import QImage,QImageReader,QPainter,QPainterPath,QColor,QPen,QFont
from PySide6.QtWidgets import QApplication,QPushButton,QLabel,QHBoxLayout,QPlainTextEdit,QStackedWidget,QWidget
from app_settings import ReferenceModalDialog,SettingsCloseButton
from native_ui_common import colors,icon,keyboard_focus_visible,RADIUS_CONTROL_PX,RADIUS_CARD_PX
from sync_files import DiskWork
from transfer_visuals import CloudStatusBadge,CLOUD_STATUS_LABELS
from translation_languages import is_link_line
from shiboken6 import isValid
import ui_theme

MAX_CLIPBOARD_LINK_CHARS=65536


def clipboard_is_link(value):
    if not value or value[0]!='text' or not isinstance(value[1],dict):return False
    text=value[1].get('text','')
    return isinstance(text,str) and len(text)<=MAX_CLIPBOARD_LINK_CHARS and is_link_line(text.strip())


def clipboard_status(state):
    pending=state.get('receive_state') or {}
    if state.get('receive_error'):return state['receive_error']
    if pending.get('error')=='CLOUD_CONTENT_EXPIRED':return '云端内容已过期 · 请在来源设备重新复制'
    if pending.get('error')=='REMOTE_FILE_PLACEHOLDER':return '远控文件尚未就绪 · 请在来源设备重新复制'
    if pending and not state.get('receive_active',True):return '已收到同步提示 · 等待同步恢复'
    if pending.get('stage')=='needs_confirmation':return '剪贴板写入未完成 · 点击重试'
    if pending.get('stage')=='retrying':return '剪贴板接收遇到问题 · 正在重试'
    if pending.get('stage')=='received':return '已收到同步提示 · 正在获取内容'
    if state.get('native_format_issue')=='REMOTE_FILE_PLACEHOLDER':return '远控文件尚未就绪 · 未同步占位文件'
    if state.get('native_format_issue') in ('NATIVE_CLIPBOARD_BUSY','NATIVE_READ_TIMEOUT'):
        return '剪贴板正被其他应用占用，正在重试读取'
    if state.get('native_format_issue')=='VIRTUAL_FILE_SIZE_LIMIT':return '此文件不能同步 · 超过 20 MB 同步上限'
    if state.get('native_format_issue')=='VIRTUAL_FILE_SINGLE_ONLY':return '仅支持同步单个文件 · 保留上次已确认内容'
    if state.get('native_format_issue') in ('VIRTUAL_FILE_INVALID_NAME','VIRTUAL_FILE_INVALID_DESCRIPTOR','VIRTUAL_FILE_SIZE_UNKNOWN'):
        return '当前文件格式无法读取 · 保留上次已确认内容'
    if str(state.get('native_format_issue','')).startswith('VIRTUAL_FILE_'):return '正在读取跨应用文件 · 保留上次已确认内容'
    if state.get('receive_pending'):return '内容已接收 · 正在等待写入系统剪贴板'
    if state.get('native_read_pending'):return '正在读取系统剪贴板 · 保留上次已确认内容'
    if state.get('phase')=='superseded':return '本次同步已由更新的复制接替'
    if state.get('phase')=='size_limit':
        value=state.get('value')
        return ('此文件不能同步' if value and value[0]=='files' else '此内容不能同步')+' · 超过 20 MB 同步上限'
    counts=state.get('delivery_counts') or {}
    if state.get('local_copy') and counts.get('total',0):
        detail=f"已送达 {counts['delivered']} 台 · 已应用 {counts['applied']} 台"
        return detail+(' · 部分设备未送达' if counts.get('late') else '')
    return CLOUD_STATUS_LABELS.get(state.get('phase'),'仅保存在本机')


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
        self.setAccessibleName('打开或关闭剪贴板预览');self.clicked.connect(self._open);self.refresh()

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
        value=state.get('value')
        self._glyph='text' if value and value[0]=='text' else 'file' if value else 'copy'
        if clipboard_is_link(value):self._glyph='link'
        self.setToolTip('剪贴板 · '+('需要设置 Mac 剪贴板权限，点击前往' if state.get('permission_required') else
            clipboard_status(state) if state.get('value') else '暂无可预览内容'))
        self.setAccessibleDescription(clipboard_status(state))
        self.update()

    def paintEvent(self,event):
        from reference_widgets import ink
        c=colors(ui_theme.current_theme());p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        fill=c['control_pressed'] if self.isDown() else c['control_hover'] if self.underMouse() else c['control']
        from native_ui_common import pixel_aligned_border
        border,stroke=pixel_aligned_border(p,QRectF(self.rect()))
        p.setPen(QPen(ink(c['border']),stroke));p.setBrush(ink(fill))
        p.drawRoundedRect(border,RADIUS_CONTROL_PX,RADIUS_CONTROL_PX)
        model=self.presenter;value=model.state.get('value') if model else None
        if model and value and value[0] in ('image','files') and not model.thumbnail.isNull():
            clip=QPainterPath();clip.addRoundedRect(QRectF(5,5,22,22),4,4);p.setClipPath(clip)
            image=model.thumbnail;size=image.size().scaled(QSize(22,22),Qt.KeepAspectRatio)
            p.drawImage(QRectF(16-size.width()/2,16-size.height()/2,size.width(),size.height()),image);p.setClipping(False)
        else:
            icon(self._glyph,c['secondary']).paint(p,7,7,18,18)
        if keyboard_focus_visible(self):
            p.setPen(QPen(QColor(c['link']),1.5));p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(2,2,-2,-2),7,7)
        p.end()


class ClipboardPreview(ReferenceModalDialog):
    def __init__(self,manager):
        # Qt can dispatch virtual events while the base dialog is still
        # constructing its body and native flags. Those callbacks already
        # resolve this subclass, so their dependencies must exist first.
        self.manager=manager;self.presenter=manager.clipboard_presenter
        self._initializing=True
        # Repeated open/close must never leave a zoom effect intercepting input
        # over the shelf. This transient view closes synchronously instead.
        super().__init__(manager.shelf,surface_width=600,modal=False,animate=False)
        self.setWindowFlag(Qt.WindowStaysOnTopHint,True)
        self.manager=manager;self.presenter=manager.clipboard_presenter
        self._moving=False;self._moved_revision=None
        self.setWindowTitle('FlickDock · 剪贴板预览')
        head=QHBoxLayout();self.link_icon=QLabel(self.body);self.link_icon.setFixedSize(18,18)
        self.link_icon.setAccessibleName('链接');self.link_icon.hide();head.addWidget(self.link_icon)
        self.title=QLabel('剪贴板预览',self.body);head.addWidget(self.title,1)
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
        from transfer_visuals import ClipboardProgressRow
        self.progress=ClipboardProgressRow(self.body);self.body_layout.addWidget(self.progress)
        footer=QHBoxLayout();self.status=QLabel(self.body);footer.addWidget(self.status,1)
        self.status.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.status.linkActivated.connect(lambda _:self.presenter.client.retry_received())
        from reference_widgets import button
        self.shelf_button=button('转入搁架文件','plus',variant='secondary',parent=self.body)
        self.shelf_button.clicked.connect(self._move_to_shelf);footer.addWidget(self.shelf_button);self.body_layout.addLayout(footer)
        self.presenter.changed.connect(self.refresh);self.refresh()
        manager.shelf.installEventFilter(self)
        self._initializing=False

    def eventFilter(self,watched,event):
        if getattr(self,'_initializing',True):return False
        if watched is self.manager.shelf and event.type() in (QEvent.Hide,QEvent.Close):
            self.hide()
            return super().eventFilter(watched,event)
        return super().eventFilter(watched,event)

    def _move_to_shelf(self):
        value=self.presenter.state.get('value');revision=self.presenter.state.get('revision')
        if value is None or self._moving or revision==self._moved_revision:return
        self._moving=True;self.refresh()
        def finished():
            if not isValid(self):return
            self._moving=False;self._moved_revision=revision;self.refresh()
        def failed(error='无法转入搁架，请重试。'):
            if not isValid(self):return
            self._moving=False;self.refresh();self.status.setText(str(error))
        kind,data=value
        if kind=='files':
            try:self.manager.shelf.import_paths('permanent',list(data),on_committed=finished,on_failed=failed,force_copy=True)
            except (OSError,ValueError) as error:failed(error)
            return
        mime=QMimeData()
        if kind=='text':
            mime.setText(data['text'])
            if data.get('html'):mime.setHtml(data['html'])
        elif data.get('raw'):mime.setData(data.get('mime') or 'image/png',data['raw'])
        else:mime.setImageData(data['image'])
        try:accepted=self.manager.shelf.receive_mime('permanent',mime,source='paste')
        except (OSError,ValueError) as error:failed(error);return
        if accepted:finished()
        else:failed()

    def refresh(self):
        model=self.presenter;value=model.state.get('value');phase=model.state.get('phase','local')
        c=colors(self.manager.settings.theme);self.set_surface_theme(self.manager.settings.theme)
        self.link_icon.setPixmap(icon('link',c['secondary']).pixmap(QSize(18,18)))
        self.link_icon.setVisible(clipboard_is_link(value))
        self.close_button.dark=self.manager.settings.theme=='dark'
        self.setStyleSheet(f"QDialog{{background:transparent;}} QLabel{{background:transparent;color:{c['text']};font-size:12px;}} QPlainTextEdit{{background:{c['card']};color:{c['text']};border:0;border-radius:{RADIUS_CARD_PX}px;padding:10px;}} QPushButton#settingsClose{{background:transparent;border:0;border-radius:{RADIUS_CONTROL_PX}px;padding:0;}} QPushButton#settingsClose:hover{{background:{c['hover']};}} QPushButton#settingsClose:pressed{{background:{c['control']};}}")
        self.badge.set_status(phase);status=clipboard_status(model.state)
        pending=model.state.get('receive_state') or {}
        if pending.get('stage')=='needs_confirmation' and pending.get('error')=='NATIVE_WRITE_UNCONFIRMED':
            from html import escape
            self.status.setText(escape(status).replace('点击重试','<a href="retry">点击重试</a>'))
        else:self.status.setText(status)
        self.progress.set_state(model.state)
        moved=self._moved_revision==model.state.get('revision')
        self.shelf_button.setEnabled(value is not None and not self._moving and not moved)
        self.shelf_button.setText('正在转入…' if self._moving else '已转入搁架' if moved else '转入搁架文件')
        from reference_widgets import refresh_tree
        refresh_tree(self.shelf_button)
        self.badge.setToolTip(status);self.badge.setAccessibleName(status)
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

    def hideEvent(self,event):
        self.presenter._large_requested=False;super().hideEvent(event)
