"""An expiring, nonactivating cloud-retention hint, independent of upload jobs."""
from PySide6.QtCore import QPoint,Qt,QTimer,Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget,QFrame,QHBoxLayout,QLabel,QPushButton,QVBoxLayout
from platform_native import keep_tool_visible_when_inactive


class IntakeNotice(QWidget):
    remote_requested=Signal(list)
    def __init__(self,manager):
        super().__init__();self.manager=manager;self.paths=[]
        self.setWindowFlags(Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint|Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating);self.setAttribute(Qt.WA_TranslucentBackground)
        keep_tool_visible_when_inactive(self)
        root=QVBoxLayout(self);root.setContentsMargins(0,0,0,0)
        self.card=QFrame(self);root.addWidget(self.card)
        row=QHBoxLayout(self.card);row.setContentsMargins(14,11,12,11);row.setSpacing(12)
        self.message=QLabel(self.card);self.message.setWordWrap(True);row.addWidget(self.message,1)
        self.remote_button=QPushButton('远程互传',self.card);self.remote_button.setMinimumSize(78,30)
        self.remote_button.setAutoDefault(False);row.addWidget(self.remote_button)
        self.remote_button.clicked.connect(self._remote)
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self.dismiss)
        from ui_transitions import WindowFade
        self._window_fade=WindowFade(self)
        self.setFixedWidth(390)
    def dismiss(self):
        if not self._window_fade.defer_done(0,lambda _:self.hide()):self.hide()
    def _remote(self):
        paths=list(self.paths);self.hide();self.remote_requested.emit(paths)
    def show_retention(self,paths,large):
        from native_ui_common import colors
        c=colors(self.manager.settings.theme);self.paths=list(paths)
        self.card.setStyleSheet(f"QFrame{{background:{c['surface']};border:1px solid {c['border']};border-radius:10px;}} QLabel{{color:{c['text']};border:0;font-size:12px;}} QPushButton{{background:{c['blue']};color:white;border:0;border-radius:7px;padding:4px 8px;font-size:12px;}}")
        self.message.setText('较大文件云端上传成功后仅保留 2 小时，建议使用远程互传。' if large else '文件云端上传成功后保留 24 小时，本地原文件不受影响。')
        self.remote_button.setVisible(large);self.adjustSize()
        shelf=self.manager.shelf;anchor=shelf.visual_geometry()
        screen=QGuiApplication.screenAt(anchor.center()) or QGuiApplication.primaryScreen();area=screen.availableGeometry()
        self.move(max(area.left()+8,min(anchor.right()-self.width(),area.right()-self.width()-8)),
                  max(area.top()+8,min(anchor.bottom()+8,area.bottom()-self.height()-8)))
        self.show();self.raise_();self.timer.start(8000 if large else 3500)
    def hideEvent(self,event):
        self.timer.stop();super().hideEvent(event)
        QTimer.singleShot(0,self.manager._offer_next_large_intake)


class ClipboardNotice(QWidget):
    """Short sender-only feedback; never owns keyboard, pointer or Dock focus."""
    def __init__(self,manager):
        super().__init__();self.manager=manager
        self.setWindowFlags(Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint|Qt.WindowDoesNotAcceptFocus|Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_ShowWithoutActivating);self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        keep_tool_visible_when_inactive(self)
        root=QHBoxLayout(self);root.setContentsMargins(0,0,0,0)
        self.card=QFrame(self);root.addWidget(self.card)
        row=QHBoxLayout(self.card);row.setContentsMargins(12,8,12,8);row.setSpacing(7)
        self.symbol=QLabel(self.card);self.symbol.setFixedSize(15,15);row.addWidget(self.symbol)
        self.message=QLabel(self.card);row.addWidget(self.message)
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self.dismiss)
        from ui_transitions import WindowFade
        self._window_fade=WindowFade(self)

    def dismiss(self):
        if not self._window_fade.defer_done(0,lambda _:self.hide()):self.hide()

    def show_result(self,ok,context):
        from native_ui_common import colors
        from flickdock_icons import glyph_pixmap
        c=colors(self.manager.settings.theme)
        self.message.setText('复制已同步' if ok else '复制同步失败')
        self.symbol.setPixmap(glyph_pixmap('check' if ok else 'close',15,c['green'] if ok else c['red'],dpr=self.devicePixelRatioF()))
        self.card.setStyleSheet(f"QFrame{{background:{c['surface']};border:1px solid {c['border']};border-radius:8px;}} QLabel{{color:{c['text']};border:0;font-size:12px;background:transparent;}}")
        self.adjustSize();point=QPoint(*context.get('point',(0,0)))
        screen=QGuiApplication.screenAt(point) or QGuiApplication.primaryScreen();area=screen.availableGeometry()
        if context.get('fullscreen'):
            x=area.center().x()-self.width()//2;y=area.bottom()-self.height()-32
        else:x=point.x()+14;y=point.y()+22
        self.move(max(area.left()+8,min(x,area.right()-self.width()-8)),max(area.top()+8,min(y,area.bottom()-self.height()-8)))
        self._window_fade.reopen();self.show();self.raise_();self.timer.start(1400)
