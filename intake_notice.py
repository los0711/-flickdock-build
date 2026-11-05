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

NOTICE_WIDTH_PX=380
NOTICE_MARGIN_PX=16
NOTICE_RADIUS_PX=18
NOTICE_PADDING_PX=18
NOTICE_DURATION_MS=2200
RESULT_COALESCE_MS=160
HEADER_HEIGHT_PX=25
LOGO_SIZE_PX=23
STATUS_ICON_SIZE_PX=15
PREVIEW_MAX_CHARS=2048


def copy_status(ok,context):
    if not ok:return '同步未完成 · 内容保留在本机'
    if context.get('delivered'):
        route={'lan':'局域网','p2p':'P2P 直连','relay':'TURN 中继','cloud':'云端'}.get(context.get('route'),'设备同步')
        return route+' · 已发送到另一台设备'
    if context.get('backed_up'):return '云端 · 已备份剪贴板'
    return '剪贴板内容已同步'


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
        layout.addSpacing(11)
        self.preview=QLabel(self.card);self.preview.setTextFormat(Qt.PlainText)
        self.preview.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred);layout.addWidget(self.preview)
        layout.addSpacing(8)
        footer=QHBoxLayout();footer.setSpacing(5)
        self.status_icon=QLabel(self.card);self.status_icon.setFixedSize(STATUS_ICON_SIZE_PX,STATUS_ICON_SIZE_PX);footer.addWidget(self.status_icon)
        self.status=QLabel(self.card);self.status.setObjectName('copyStatus');self.status.setTextFormat(Qt.PlainText)
        self.status.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred);footer.addWidget(self.status,1);layout.addLayout(footer)
        self._pending_result=None
        self._result_timer=QTimer(self);self._result_timer.setSingleShot(True);self._result_timer.setInterval(RESULT_COALESCE_MS)
        self._result_timer.timeout.connect(self._deliver_result)
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self.dismiss)
        self._window_fade=WindowFade(self)

    def dismiss(self):
        self.timer.stop();fade_hide(self)

    def supersede(self):
        self._result_timer.stop();self._pending_result=None;self.hide()

    def schedule_result(self,ok,context):
        self._pending_result=(ok,dict(context));self._result_timer.start()

    def _deliver_result(self):
        result,self._pending_result=self._pending_result,None
        if (result is not None and not getattr(self.manager,'_quitting',False)
                and getattr(self.manager,'_clipboard_runtime_enabled',lambda:True)()
                and getattr(self.manager.settings,'clipboard_success_notice',True)):self.show_result(*result)

    def show_result(self,ok,context):
        if not getattr(self.manager.settings,'clipboard_success_notice',True):return
        c=colors(self.manager.settings.theme);tint=c['green' if ok else 'red']
        self.card.setStyleSheet(f'''
            QFrame#clipboardNotice{{background:{c['surface']};border:0;border-radius:{NOTICE_RADIUS_PX}px;}}
            QLabel{{color:{c['text']};border:0;background:transparent;font-size:12px;}}
            QLabel#copyHeading{{font-size:13px;font-weight:600;}}
            QLabel#copyStatus{{font-size:11px;color:{tint};}}
            QPushButton#settingsClose{{background:transparent;border:0;border-radius:5px;}}
            QPushButton#settingsClose:hover{{background:{c['hover']};}}
            QPushButton#settingsClose:pressed{{background:{c['selected']};}}
        ''')
        self.close_button.dark=self.manager.settings.theme=='dark';self.close_button.update()
        self.ensurePolished()
        self.message.setText('复制已同步' if ok else '复制同步失败')
        self.status_icon.setPixmap(icon('circle-check' if ok else 'circle-alert',tint).pixmap(STATUS_ICON_SIZE_PX,STATUS_ICON_SIZE_PX))
        screen=QGuiApplication.screenAt(QPoint(*context.get('point',(0,0)))) or QGuiApplication.primaryScreen();area=screen.availableGeometry()
        self.setFixedWidth(min(NOTICE_WIDTH_PX,max(1,area.width()-2*NOTICE_MARGIN_PX)))
        available=self.width()-2*NOTICE_PADDING_PX
        self.preview.setText(self.preview.fontMetrics().elidedText(copy_preview(context.get('value')),Qt.ElideRight,available))
        status=copy_status(ok,context);self.status.setAccessibleDescription(status)
        self.status.setText(self.status.fontMetrics().elidedText(status,Qt.ElideRight,available-STATUS_ICON_SIZE_PX-5))
        self.adjustSize();self.move(area.right()-self.width()-NOTICE_MARGIN_PX+1,area.top()+NOTICE_MARGIN_PX)
        self.timer.stop();self._window_fade.reopen();self.show();self.raise_();self.timer.start(NOTICE_DURATION_MS)

    def hideEvent(self,event):
        self.timer.stop();super().hideEvent(event)
