"""One expiring, nonactivating card for the latest local copy result."""
from pathlib import Path
from PySide6.QtCore import QPoint,Qt,QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget,QFrame,QHBoxLayout,QLabel,QVBoxLayout,QSizePolicy
from app_settings import SettingsCloseButton
from flickdock_icons import app_icon
from native_ui_common import colors,icon
from platform_native import keep_tool_visible_when_inactive
from ui_transitions import WindowFade,fade_hide

NOTICE_WIDTH_PX=320
NOTICE_MARGIN_PX=16
NOTICE_RADIUS_PX=14
NOTICE_PADDING_PX=12
NOTICE_DURATION_MS=2200
NOTICE_PROGRESS_DURATION_MS=6000
RESULT_COALESCE_MS=160
HEADER_HEIGHT_PX=22
LOGO_SIZE_PX=20
STATUS_ICON_SIZE_PX=13
BODY_GAP_PX=6
STATUS_GAP_PX=5
PREVIEW_MAX_CHARS=2048


def copy_status(ok,context):
    if context.get('phase')=='size_limit':return '超过 20 MB 同步上限 · 内容保留在本机'
    if context.get('phase')=='superseded':return '本次同步已由更新的复制接替'
    if ok is False:return '同步未完成 · 内容保留在本机'
    if ok is None or context.get('phase')=='uploading':return '正在同步到云端'
    return '已同步到云端'



def copy_preview(value):
    if not value:return '剪贴板内容'
    kind,data=value
    if kind=='text':return str(data['text'])[:PREVIEW_MAX_CHARS].replace('\r',' ').replace('\n',' ')
    if kind=='image':return '图片'
    if kind=='files' and data:
        return Path(data[0]).name+(f' · 共 {len(data)} 项' if len(data)>1 else '')
    return '剪贴板内容'


class ClipboardNotice(QWidget):
    """Sender-only feedback; closing the card never changes clipboard jobs."""
    def __init__(self,manager):
        super().__init__();self.manager=manager
        self.setWindowFlags(Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint|Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating);self.setAttribute(Qt.WA_TranslucentBackground)
        keep_tool_visible_when_inactive(self);self.setWindowTitle('FlickDock · 复制同步提示')
        root=QVBoxLayout(self);root.setContentsMargins(0,0,0,0)
        self.card=QFrame(self);self.card.setObjectName('clipboardNotice');root.addWidget(self.card)
        layout=QVBoxLayout(self.card);layout.setContentsMargins(*([NOTICE_PADDING_PX]*4));layout.setSpacing(0)
        head=QHBoxLayout();head.setSpacing(9)
        self.logo=QLabel(self.card);self.logo.setFixedSize(LOGO_SIZE_PX,LOGO_SIZE_PX)
        self.logo.setPixmap(app_icon().pixmap(LOGO_SIZE_PX,LOGO_SIZE_PX));head.addWidget(self.logo)
        self.message=QLabel(self.card);self.message.setObjectName('copyHeading');self.message.setTextFormat(Qt.PlainText);head.addWidget(self.message,1)
        self.close_button=SettingsCloseButton(self.card,glyph_size=13);self.close_button.setFixedSize(HEADER_HEIGHT_PX,HEADER_HEIGHT_PX)
        self.close_button.setFocusPolicy(Qt.NoFocus);self.close_button.setAccessibleName('关闭复制同步提示');self.close_button.setToolTip('关闭提示')
        self.close_button.clicked.connect(self.dismiss);head.addWidget(self.close_button);layout.addLayout(head)
        layout.addSpacing(BODY_GAP_PX)
        self.preview=QLabel(self.card);self.preview.setTextFormat(Qt.PlainText)
        self.preview.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred);layout.addWidget(self.preview)
        layout.addSpacing(STATUS_GAP_PX)
        footer=QHBoxLayout();footer.setSpacing(STATUS_GAP_PX)
        self.status_icon=QLabel(self.card);self.status_icon.setFixedSize(STATUS_ICON_SIZE_PX,STATUS_ICON_SIZE_PX);footer.addWidget(self.status_icon)
        self.status=QLabel(self.card);self.status.setObjectName('copyStatus');self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred);footer.addWidget(self.status,1);layout.addLayout(footer)
        from transfer_visuals import ClipboardProgressRow
        self.progress=ClipboardProgressRow(self.card);layout.addWidget(self.progress)
        self._pending_result=None
        self._current_result=None
        self._dismissed_id=None
        self._result_timer=QTimer(self);self._result_timer.setSingleShot(True);self._result_timer.setInterval(RESULT_COALESCE_MS)
        self._result_timer.timeout.connect(self._deliver_result)
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self.dismiss)
        self._window_fade=WindowFade(self)

    def dismiss(self):
        context=self._current_result[1] if self._current_result is not None else self._pending_result[1] if self._pending_result is not None else {}
        self._dismissed_id=context.get('id');self._result_timer.stop();self._pending_result=None
        self.timer.stop();fade_hide(self)

    def supersede(self):
        if self._current_result is not None:self._dismissed_id=self._current_result[1].get('id')
        self._result_timer.stop();self._pending_result=None;self._current_result=None;self.hide()

    def schedule_result(self,ok,context):
        if context.get('id') and context['id']==self._dismissed_id:return
        self._pending_result=(ok,dict(context));self._result_timer.start()

    def update_delivery(self,state):
        if not self.manager.settings.clipboard_success_notice:self.supersede();return
        identity=state.get('id')
        if not identity or identity==self._dismissed_id:return
        if self._pending_result is not None:
            ok,context=self._pending_result
            if context.get('id')==identity:context.update(state)
        if self._current_result is None or not self.isVisible():
            if self._pending_result is None and state.get('local_copy') and state.get('phase')=='uploading':
                self.schedule_result(None,state)
            return
        ok,context=self._current_result
        if context.get('id')!=identity:return
        self.show_result(ok,dict(context,**state))

    def _render_status(self,ok,context):
        text=copy_status(ok,context);self.status.setAccessibleDescription(text);self.status.setText(text)

    def _place(self,area):
        self.move(area.left()+(area.width()-self.width())//2,area.bottom()-self.height()-NOTICE_MARGIN_PX+1)

    def _deliver_result(self):
        result,self._pending_result=self._pending_result,None
        if (result is not None and not getattr(self.manager,'_quitting',False)
                and getattr(self.manager,'_clipboard_runtime_enabled',lambda:True)()
                and getattr(self.manager.settings,'clipboard_success_notice',True)):self.show_result(*result)

    def show_result(self,ok,context):
        if not getattr(self.manager.settings,'clipboard_success_notice',True):return
        if context.get('id') and context['id']==self._dismissed_id:return
        same=bool(self.isVisible() and self._current_result is not None and context.get('id')==self._current_result[1].get('id'))
        was_active=self.progress.active
        self.progress.set_state(context)
        c=colors(self.manager.settings.theme);limited=context.get('phase')=='size_limit';superseded=context.get('phase')=='superseded'
        tint=c['link'] if limited or superseded or ok is None or self.progress.active else c['green' if ok else 'red']
        self.card.setStyleSheet(f'''
            QFrame#clipboardNotice{{background:{c['surface']};border:0;border-radius:{NOTICE_RADIUS_PX}px;}}
            QLabel{{color:{c['text']};border:0;background:transparent;font-size:11px;}}
            QLabel#copyHeading{{font-size:12px;font-weight:600;color:{tint if limited else c['text']};}}
            QLabel#copyStatus{{font-size:11px;color:{tint};}}
            QPushButton#settingsClose{{background:transparent;border:0;border-radius:5px;}}
            QPushButton#settingsClose:hover{{background:{c['hover']};}}
            QPushButton#settingsClose:pressed{{background:{c['selected']};}}
        ''')
        self.close_button.dark=self.manager.settings.theme=='dark';self.close_button.update()
        self.ensurePolished()
        kind=context.get('value',('text',{}))[0]
        self.message.setText(('此文件不能同步' if kind=='files' else '此图片不能同步' if kind=='image' else '此内容不能同步') if limited else '已有更新的复制' if superseded else '正在同步剪贴板' if ok is None or self.progress.active else '复制已同步' if ok else '复制同步失败')
        self.status_icon.setVisible(ok is not None)
        self.status_icon.setPixmap(icon('circle-check' if ok else 'circle-alert',tint).pixmap(STATUS_ICON_SIZE_PX,STATUS_ICON_SIZE_PX))
        screen=QGuiApplication.screenAt(QPoint(*(context.get('point') or (0,0)))) or QGuiApplication.primaryScreen();area=screen.availableGeometry()
        self.setFixedWidth(min(NOTICE_WIDTH_PX,max(1,area.width()-2*NOTICE_MARGIN_PX)))
        available=self.width()-2*NOTICE_PADDING_PX
        self.preview.setText(self.preview.fontMetrics().elidedText(copy_preview(context.get('value')),Qt.ElideRight,available))
        self.status.setFixedWidth(max(1,available-STATUS_ICON_SIZE_PX-STATUS_GAP_PX))
        self._render_status(ok,context)
        self.adjustSize()
        self._place(area)
        self._current_result=(ok,dict(context))
        if not same:
            self._window_fade.reopen();self.show();self.raise_()
            self.timer.start(NOTICE_PROGRESS_DURATION_MS if self.progress.active else NOTICE_DURATION_MS)
        elif was_active and not self.progress.active:self.timer.start(NOTICE_DURATION_MS)

    def hideEvent(self,event):
        self.timer.stop();self._current_result=None;super().hideEvent(event)
