"""Small, nonmodal application window for an available FlickDock update."""
from __future__ import annotations
import sys

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, Signal,QEvent,QRectF
from PySide6.QtGui import QColor, QCursor, QGuiApplication,QPainter
from PySide6.QtWidgets import (QFrame, QGraphicsDropShadowEffect, QHBoxLayout,
                               QLabel, QPushButton, QVBoxLayout, QWidget,QScrollArea,QApplication,QProgressBar)
from shiboken6 import isValid
from native_ui_common import colors as ui_colors, reference_font


class UpdateNotice(QWidget):
    """Present one update offer without a system notification or focus steal."""

    update_requested = Signal()
    dismissed = Signal()

    def __init__(self, parent=None, *, theme="light"):
        super().__init__(parent, Qt.WindowType.Widget)
        self.setObjectName("flickdockUpdateNotice")
        self.setWindowTitle("FlickDock · 新版本")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._owner=parent
        self._previous_focus=None
        if parent:parent.installEventFilter(self);parent.card.installEventFilter(self)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16,16,16,16)
        self.surface = QFrame(self)
        self.surface.setObjectName("updateNoticeSurface")
        outer.addWidget(self.surface,0,Qt.AlignmentFlag.AlignCenter)
        self.surface.setFixedSize(420,146)
        shadow = QGraphicsDropShadowEffect(self.surface)
        shadow.setBlurRadius(23)
        shadow.setOffset(0, 5)
        shadow.setColor(QColor(20, 34, 58, 52))
        self.surface.setGraphicsEffect(shadow)

        content = QVBoxLayout(self.surface)
        content.setContentsMargins(20,20,20,20)
        content.setSpacing(16)
        header=QHBoxLayout();header.setSpacing(10)
        self.symbol=QLabel('↑',self.surface);self.symbol.setObjectName('updateNoticeSymbol')
        self.symbol.setFixedSize(32,32);self.symbol.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.symbol)
        captions=QVBoxLayout();captions.setSpacing(3)
        self.title = QLabel("FlickDock 有新版本", self.surface)
        self.title.setObjectName("updateNoticeTitle")
        captions.addWidget(self.title)
        self.version_label = QLabel(self.surface)
        self.version_label.setObjectName("updateNoticeVersion")
        self.version_label.setTextFormat(Qt.TextFormat.PlainText)
        captions.addWidget(self.version_label);header.addLayout(captions,1)
        self.close_button=QPushButton('×',self.surface);self.close_button.setObjectName('updateNoticeClose')
        self.close_button.setFixedSize(24,24);self.close_button.setAccessibleName('稍后更新')
        self.close_button.setAutoDefault(False);self.close_button.clicked.connect(self.dismiss)
        header.addWidget(self.close_button)
        content.addLayout(header)
        self.notes_area=QScrollArea(self.surface);self.notes_area.setWidgetResizable(True)
        self.notes_area.setFrameShape(QFrame.Shape.NoFrame);self.notes_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.notes_label=QLabel(self.notes_area);self.notes_label.setObjectName('updateNoticeNotes')
        self.notes_label.setWordWrap(True);self.notes_label.setTextFormat(Qt.TextFormat.PlainText)
        self.notes_label.setAlignment(Qt.AlignmentFlag.AlignTop|Qt.AlignmentFlag.AlignLeft)
        self.notes_area.setWidget(self.notes_label);self.notes_area.hide();content.addWidget(self.notes_area,1)
        self.status_label=QLabel(self.surface);self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText);self.status_label.hide();content.addWidget(self.status_label)
        self.progress=QProgressBar(self.surface);self.progress.setFixedHeight(5)
        self.progress.setTextVisible(False);self.progress.hide();content.addWidget(self.progress)
        self._state='available'

        actions = QHBoxLayout()
        actions.setSpacing(9)
        actions.addStretch(1)
        self.later_button = QPushButton("稍后", self.surface)
        self.later_button.setObjectName("updateNoticeLater")
        self.later_button.setFixedSize(56,28)
        self.later_button.setAutoDefault(False)
        self.later_button.setAccessibleName("稍后更新")
        self.later_button.clicked.connect(self.dismiss)
        actions.addWidget(self.later_button)
        self.update_button = QPushButton("立即更新", self.surface)
        self.update_button.setObjectName("updateNoticePrimary")
        self.update_button.setFixedSize(92,28)
        self.update_button.setAutoDefault(False)
        self.update_button.clicked.connect(self.update_requested.emit)
        actions.addWidget(self.update_button)
        content.addLayout(actions)

        self._appearance = QPropertyAnimation(self, b"windowOpacity", self)
        self._appearance.setDuration(170)
        self._appearance.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.apply_theme(theme)

    def apply_theme(self, theme):
        c=ui_colors(theme)
        family=reference_font().family()
        self.setStyleSheet(f"""
            QFrame#updateNoticeSurface {{ background:{c['surface']}; border:1px solid {c['glass_border']}; border-radius:{c['radius']}px; }}
            QLabel#updateNoticeTitle {{ color:{c['text']}; border:0; background:transparent;
                font-family:'{family}'; font-size:14px; font-weight:700; }}
            QLabel#updateNoticeVersion {{ color:{c['secondary']}; border:0; background:transparent;
                font-family:'{family}'; font-size:11px; }}
            QScrollArea {{background:transparent;border:0;}}
            QLabel#updateNoticeNotes {{color:{c['secondary']};background:transparent;border:0;font-family:'{family}';font-size:12px;}}
            QLabel#updateNoticeSymbol {{ color:{c['blue']}; background:rgba(10,132,255,51); border:0; border-radius:12px; font-size:20px; font-weight:700; }}
            QPushButton {{ border:1px solid transparent; border-radius:12px; background:{c['control']};
                color:{c['text']}; font-family:'{family}'; font-size:12px; padding:0; }}
            QPushButton:hover {{ background:{c['hover']}; }}
            QPushButton:pressed {{ background:{c['selected']}; }}
            QPushButton#updateNoticePrimary {{ background:{c['action']}; color:white; font-weight:700; }}
            QPushButton#updateNoticePrimary:hover {{ background:{c['action_hover']}; }}
            QPushButton#updateNoticeClose {{ border:0; border-radius:8px; background:transparent; color:{c['secondary']}; font-size:18px; }}
            QPushButton#updateNoticeClose:hover {{ background:{c['hover']}; }}
        """)

    def dismiss(self):
        self.dismissed.emit();self.hide()

    def set_state(self,state,version='',received=0,total=0,error=''):
        """Render the shared controller without owning its task lifetime."""
        self._state=state
        labels={'idle':('','检查更新'),'checking':('正在检查更新…','检查中…'),
                'available':('可以开始下载','立即更新'),'latest':('当前已是最新版本','检查更新'),
                'downloading':(f'正在下载 {min(100,round(received*100/total))}%' if total else '正在下载…','下载中…'),
                'verifying':('正在校验安装包…','校验中…'),'ready':('下载完成，可以安装','重启并安装'),
                'installing':('正在安装；如系统提示，请完成授权。','安装中…'),
                'handoff':('安装程序已启动，请按系统提示完成。','安装已启动'),
                'error':(str(error)[:800] or '更新未完成，请重试。','重试更新' if version else '重试检查'),
                'cancelled':('更新已取消','继续更新'),'cancelling':('正在取消…','请稍候…')}
        text,action=labels.get(state,('','检查更新'))
        self.status_label.setText(text);self.status_label.setVisible(bool(text))
        self.status_label.setToolTip(str(error))
        busy=state in ('checking','downloading','verifying','installing','cancelling','handoff')
        self.update_button.setText(action);self.update_button.setEnabled(not busy)
        self.progress.setVisible(state in ('checking','downloading','verifying','installing'))
        if state=='downloading' and total>0:
            self.progress.setRange(0,1000);self.progress.setValue(min(1000,round(received*1000/total)))
        else:self.progress.setRange(0,0)
        self._fit_surface()

    def _fit_surface(self):
        width=min(420,max(240,self.width()-32))
        wanted=(272 if not self.notes_area.isHidden() else 146)+(64 if not self.status_label.isHidden() else 0)
        self.surface.setFixedSize(width,min(wanted,max(130,self.height()-32)))

    def eventFilter(self,watched,event):
        if self.isVisible() and self._owner and isValid(self._owner):
            if watched in (self._owner,self._owner.card) and event.type() in (QEvent.Type.Hide,QEvent.Type.Close):self.hide()
            if isinstance(watched,QWidget) and (watched is self._owner or self._owner.isAncestorOf(watched)) and event.type() in (QEvent.Type.KeyPress,QEvent.Type.ShortcutOverride):
                if event.key()==Qt.Key.Key_Escape:
                    event.accept()
                    if event.type()==QEvent.Type.KeyPress:self.dismiss()
                    return True
                if not (watched is self or self.isAncestorOf(watched)):
                    event.accept();return True
                if event.key() in (Qt.Key.Key_Tab,Qt.Key.Key_Backtab):
                    event.accept()
                    if event.type()==QEvent.Type.KeyPress:
                        buttons=(self.later_button,self.update_button,self.close_button);current=QApplication.focusWidget()
                        index=buttons.index(current) if current in buttons else 0
                        buttons[(index+(-1 if event.key()==Qt.Key.Key_Backtab else 1))%len(buttons)].setFocus(Qt.FocusReason.TabFocusReason)
                    return True
        if event.type() in (QEvent.Type.Resize,QEvent.Type.Move) and self._owner:
            self.setGeometry(self._owner.card.geometry())
            self._fit_surface()
        return False

    def paintEvent(self,event):
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen);painter.setBrush(QColor(0,0,0,150))
        painter.drawRoundedRect(QRectF(self.rect()),16 if sys.platform=='darwin' else 10,16 if sys.platform=='darwin' else 10)
        painter.end()

    def hideEvent(self,event):
        QApplication.instance().removeEventFilter(self)
        if self._owner and isValid(self._owner):
            self._owner.setProperty('flickdockConfirmationActive',False)
            if self._owner.isActiveWindow() and self._previous_focus and isValid(self._previous_focus):
                self._previous_focus.setFocus(Qt.FocusReason.OtherFocusReason)
        super().hideEvent(event)

    def mousePressEvent(self,event):event.accept()
    def mouseReleaseEvent(self,event):event.accept()
    def mouseMoveEvent(self,event):event.accept()
    def wheelEvent(self,event):event.accept()

    def keyPressEvent(self,event):
        if event.key()==Qt.Key.Key_Escape:self.dismiss();event.accept();return
        super().keyPressEvent(event)

    def show_version(self, version,notes=''):
        version = str(version).strip()[:32]
        if not version:
            return
        self.version_label.setText(f"v{version} 已发布")
        self.title.setText(f"发现新版本 FlickDock v{version}")
        self.update_button.setAccessibleName(f"立即更新到 {version}")
        notes=str(notes).strip()[:4000]
        self.notes_label.setText('本次更新\n'+notes if notes else '')
        self.notes_area.setVisible(bool(notes))
        if self._owner:
            self.setGeometry(self._owner.card.geometry())
            self._owner.setProperty('flickdockConfirmationActive',True)
            self._previous_focus=QApplication.focusWidget()
        QApplication.instance().installEventFilter(self)
        self.show()
        self._fit_surface()
        self.raise_()
        if self._owner and self._owner.isActiveWindow():self.later_button.setFocus(Qt.FocusReason.OtherFocusReason)
