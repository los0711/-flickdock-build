"""Small embeddable LAN transfer panel, with explicit incoming consent."""
from __future__ import annotations

from pathlib import Path
import clipboard_audit
import json
import math
import os
import sys
import time
from collections import deque

from PySide6.QtCore import QByteArray, QEvent, QObject, QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal, QUrl
from frame_animation import FrameAnimation as QVariantAnimation
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QKeySequence, QIcon, QTransform, QDesktopServices
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QFileDialog, QFrame, QGraphicsDropShadowEffect,
                               QHBoxLayout, QLayout, QLabel, QLineEdit, QProgressBar, QPushButton,
                               QListView, QMenu, QScrollArea, QSizePolicy, QStyle, QStyledItemDelegate,
                               QStyleOptionButton, QStylePainter, QToolTip, QVBoxLayout, QGridLayout, QWidget, QStackedWidget, QButtonGroup)


from flickdock_icons import draw_glyph
from platform_native import remove_host_window_shadow
from native_ui_common import colors as ui_colors, icon as ui_icon, reference_font, reduced_motion, SEGMENT_HEIGHT, SEGMENT_BUTTON_HEIGHT


def _label(value='', parent=None, *, muted=False):
    label = QLabel(value, parent); label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True); label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    if muted: label.setObjectName('lanMuted')
    return label


class _ActivityGroup(QFrame):
    """Equal flex track; live content minimum is measured by its owner."""
    def hasHeightForWidth(self):return False
    def heightForWidth(self,width):return self.minimumHeight()


class _PressButton(QPushButton):
    def paintEvent(self,event):
        painter=QStylePainter(self)
        option=QStyleOptionButton();self.initStyleOption(option)
        if self.isDown() and self.property('pressScale'):
            painter.translate(self.width()/2,self.height()/2);painter.scale(.96,.96)
            painter.translate(-self.width()/2,-self.height()/2)
        painter.drawControl(QStyle.ControlElement.CE_PushButton,option)


def _button(text, parent=None):
    button = _PressButton(text, parent); button.setMinimumHeight(28)
    button.setAutoDefault(False); button.setDefault(False)
    button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


class _CopyCodeButton(_PressButton):
    """Copy the visible pairing code without replacing or hiding its digits."""
    def __init__(self,text='',parent=None):
        super().__init__(text,parent)
        self.setAutoDefault(False);self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip('点击复制');self.setMinimumHeight(32)
        self._feedback=QTimer(self);self._feedback.setSingleShot(True)
        self.code_provider=None
        self._feedback.timeout.connect(lambda:self.setToolTip('点击复制'))
        self.clicked.connect(self._copy)

    def _copy(self):
        current=self.code_provider() if self.code_provider else self.text()
        if not current:return
        code=''.join(current.split())
        if not (len(code) in (4,6) and code.isascii() and code.isdigit() or len(code)==8 and code.isascii() and code.isalnum()):return
        self.setText(current)
        clipboard_audit.set_text(QApplication.clipboard(),code,module='lan_panel',
            function='_CopyCodeButton._copy',trigger='code_button',origin='user_copy')
        self.setToolTip('已复制');self._feedback.start(1600)
        QToolTip.showText(self.mapToGlobal(QPoint(0,self.height()+4)),'已复制到剪贴板',self,self.rect(),1600)
        self.setAccessibleDescription('已复制到剪贴板')


class _StatusLabel(QLabel):
    """Inline errors occupy space only while there is something to report."""
    def __init__(self,text='',parent=None):
        super().__init__('',parent);self._full_text='';self.setTextFormat(Qt.TextFormat.PlainText);self.setText(text)

    def setText(self, value):
        self._full_text=str(value);self.setVisible(bool(value))
        self.setToolTip(self._full_text);self.setAccessibleName(self._full_text)
        if hasattr(self,'_theme'):
            c=ui_colors(self._theme);message=str(value)
            token='red' if any(word in message for word in ('失败','错误','无法','未连接','中断','断开')) else 'green' if '就绪' in message or message.startswith('已连接') else 'secondary'
            self.setStyleSheet(f'QLabel{{color:{c[token]};background:transparent;border:0;padding:0;font-size:12px;}}')
        self._render_text()

    def text(self):return self._full_text

    def _render_text(self):
        QLabel.setText(self,self.fontMetrics().elidedText(self._full_text,Qt.TextElideMode.ElideRight,max(0,self.contentsRect().width())))

    def resizeEvent(self,event):
        super().resizeEvent(event);self._render_text()

    def set_theme(self,theme):self._theme=theme;self.setText(self.text())

    def clear(self):
        self.setText('')


class _CodeCountdown(QWidget):
    """Server-derived code refresh deadline, shown as a quiet circular timer."""
    def __init__(self,service,button,parent=None):
        super().__init__(parent);self.service=service;self.button=button;self.dark=False
        self.setFixedSize(26,26);self.setAccessibleName('接收码自动刷新倒计时')
        self._timer=QTimer(self);self._timer.setInterval(250);self._timer.timeout.connect(self._tick)
    def _tick(self):
        identity=self.service.code_identity()
        self.button.setText(identity.get('code') or ('重试连接' if identity.get('error') else '刷新中…' if identity.get('online') else '连接中…'))
        self.button.setEnabled(bool(identity.get('code') or identity.get('error')))
        self.update()
    def showEvent(self,event):
        super().showEvent(event);self._tick();self._timer.start()
    def hideEvent(self,event):
        self._timer.stop();super().hideEvent(event)
    def paintEvent(self,event):
        state=self.service.code_identity();remaining=max(0.,state.get('refresh_remaining',0))
        duration=max(1.,state.get('refresh_duration',240))
        value=str(math.ceil(remaining)) if state.get('code') and remaining else '…' if state.get('online') else '—'
        self.setToolTip(f'{math.ceil(remaining)} 秒后自动刷新接收码' if remaining else '正在刷新接收码' if state.get('online') else '远程服务连接中')
        self.setAccessibleDescription(self.toolTip())
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect=QRectF(self.rect()).adjusted(2,2,-2,-2);pen=QPen(QColor('#374450' if self.dark else '#DCE6EF'),1.5)
        painter.setPen(pen);painter.drawEllipse(rect)
        pen.setColor(QColor('#63B3FF' if self.dark else '#0878E8'));pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        if remaining:painter.drawArc(rect,90*16,-round(360*16*min(1.,remaining/duration)))
        painter.setPen(QColor('#C7D3DE' if self.dark else '#42566A'))
        font=QFont(reference_font());font.setPixelSize(10);font.setWeight(QFont.Weight.Medium);painter.setFont(font)
        painter.drawText(self.rect(),Qt.AlignmentFlag.AlignCenter,value)


def _size(value):
    value = float(value)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024 or unit == 'TB': return f'{value:.1f} {unit}'
        value /= 1024


def _turn_usage(value):
    amount=max(0,int(value or 0))/1000000
    return f'TURN:{amount/1000:.2f}GB' if amount>=1000 else f'TURN:{amount:.1f}MB'


def _remaining(value):
    if value<60:return f'剩余约 {max(1,math.ceil(value))} 秒'
    if value<3600:return f'剩余约 {math.ceil(value/60)} 分钟'
    return f'剩余约 {math.ceil(value/3600)} 小时'


def _motion_allowed():
    """Honor platform accessibility settings and low-performance sessions."""
    disabled = {'1', 'true', 'yes', 'on'}
    if reduced_motion():return False
    if (os.environ.get('FLICKDOCK_REDUCE_MOTION', '').lower() in disabled
            or os.environ.get('FLICKDOCK_LOW_PERFORMANCE', '').lower() in disabled):
        return False
    if sys.platform == 'win32':
        try:
            import ctypes
            class AnimationInfo(ctypes.Structure):
                _fields_ = [('cbSize', ctypes.c_uint), ('iMinAnimate', ctypes.c_int)]
            user32 = ctypes.windll.user32
            if user32.GetSystemMetrics(0x1000):  # Remote desktop session.
                return False
            state = AnimationInfo(ctypes.sizeof(AnimationInfo), 1)
            if user32.SystemParametersInfoW(0x0048, state.cbSize, ctypes.byref(state), 0):
                return bool(state.iMinAnimate)
        except (AttributeError, OSError, ValueError):
            pass
    elif sys.platform == 'darwin':
        try:
            from AppKit import NSWorkspace
            return not bool(NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())
        except (ImportError, AttributeError, OSError):
            try:
                import plistlib
                preference = Path.home() / 'Library/Preferences/com.apple.universalaccess.plist'
                with preference.open('rb') as stream:
                    return not bool(plistlib.load(stream).get('reduceMotion', False))
            except (OSError, ValueError, TypeError):
                pass
    return True


class _ScanIndicator(QWidget):
    """A fixed-size discovery cue; only its paint changes between frames."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self.setAccessibleName('正在搜索附近设备')
        self.dark = False
        self._scanning = False
        self._phase = 0
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._advance)

    def set_scanning(self, scanning):
        self._scanning = bool(scanning)
        self.setAccessibleName('正在搜索附近设备' if scanning else '已发现附近设备')
        self._refresh_timer()

    def _refresh_timer(self):
        if self._scanning and self.isVisible() and _motion_allowed():
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _advance(self):
        if not self._scanning or not self.isVisible() or not _motion_allowed():
            self._timer.stop();self._phase=0;self.update();return
        self._phase = (self._phase + 1) % 12
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_timer()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self._scanning:
            painter.setPen(Qt.PenStyle.NoPen);painter.setBrush(QColor('#30D158' if getattr(self,'online',True) else '#8E8E93'))
            painter.drawEllipse(QRectF(5,5,6,6))
            return
        painter.setPen(QPen(QColor('#516076' if self.dark else '#c3d1e1'), 1.4))
        painter.drawEllipse(QRectF(2, 2, 12, 12))
        accent_pen = QPen(QColor('#89baff' if self.dark else '#2577da'), 1.8)
        accent_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(accent_pen)
        start = (90 - self._phase * 30) * 16 if self._timer.isActive() else 45 * 16
        painter.drawArc(QRectF(2, 2, 12, 12), start, 100 * 16)


class _DevicesCombo(QComboBox):
    remove_requested=Signal(str)
    def __init__(self,parent=None):
        super().__init__(parent)
        from app_settings import _InlineComboStyle
        self._popup_style=_InlineComboStyle('Fusion');self._popup_style.setParent(self)
        self.setStyle(self._popup_style);self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def showPopup(self):
        popup=self.view().window()
        from native_ui_common import rounded_popup
        rounded_popup(popup,10)
        if not hasattr(popup,'_device_corner_sync'):
            class _CornerSync(QObject):
                def __init__(self,popup):
                    super().__init__(popup);self.popup=popup
                    self.timer=QTimer(self);self.timer.setSingleShot(True)
                    self.timer.timeout.connect(lambda:rounded_popup(self.popup,10))
                    popup.installEventFilter(self)
                def eventFilter(self,watched,event):
                    # Qt's private container can clear its mask after Resize.
                    if event.type() in (QEvent.Resize,QEvent.Show):self.timer.start(0)
                    elif event.type()==QEvent.Hide:self.timer.stop()
                    return False
            popup._device_corner_sync=_CornerSync(popup)
        dark=bool(getattr(self.itemDelegate(),'dark',False))
        popup.setStyleSheet('QFrame{background:'+('#242426' if dark else '#FFFFFF')+';border:1px solid '+('#424248' if dark else '#D4D4D8')+';border-radius:10px;}')
        super().showPopup()
        rounded_popup(popup,10)
        self.view().viewport().setMouseTracking(True)
        self.view().viewport().installEventFilter(self)

    def eventFilter(self,watched,event):
        if watched is self.view().viewport() and getattr(self,'removable',False):
            delegate=self.itemDelegate()
            if event.type()==QEvent.Leave:
                delegate.remove_hover_id=None;watched.update()
            if event.type()==QEvent.MouseMove:
                index=self.view().indexAt(event.position().toPoint())
                identifier=index.data(Qt.UserRole) if index.isValid() and _DeviceDelegate.remove_rect(self.view().visualRect(index)).contains(event.position().toPoint()) else None
                if identifier!=getattr(delegate,'remove_hover_id',None):delegate.remove_hover_id=identifier;watched.update()
                if identifier:return True
            if event.type() in (QEvent.MouseButtonPress,QEvent.MouseButtonRelease,QEvent.MouseButtonDblClick):
                index=self.view().indexAt(event.position().toPoint())
                hit=bool(index.isValid() and _DeviceDelegate.remove_rect(self.view().visualRect(index)).contains(event.position().toPoint()))
                pressed=getattr(self,'_remove_pressed',None)
                if event.type()==QEvent.MouseButtonRelease and pressed:
                    self._remove_pressed=None
                    if hit and index.data(Qt.UserRole)==pressed:self.remove_requested.emit(pressed)
                    watched.update();return True
                if hit:
                    if event.type()==QEvent.MouseButtonPress and event.button()==Qt.LeftButton:self._remove_pressed=index.data(Qt.UserRole)
                    watched.update()
                    return True
        return super().eventFilter(watched,event)

    def wheelEvent(self, event): event.ignore()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter=QPainter(self)
        color = ui_colors('dark' if self.window().property('uiTheme') == 'dark' else 'light')['secondary' if self.isEnabled() else 'muted']
        ui_icon('chevron', color).paint(painter, self.width()-25, (self.height()-14)//2, 14, 14)
        online=self.currentData(Qt.ItemDataRole.UserRole+2)
        if online is not None:
            painter.setPen(Qt.PenStyle.NoPen);painter.setBrush(QColor('#30D158' if online else '#8E8E93'))
            painter.drawEllipse(QRectF(self.width()-42,(self.height()-8)/2,8,8))


class _CloseButton(QPushButton):
    def hitButton(self,position):return self.rect().contains(position)
    def paintEvent(self,event):
        super().paintEvent(event)
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        theme='dark' if self.window().property('uiTheme')=='dark' else 'light'
        ui_icon('close','#FFFFFF' if self.underMouse() else ui_colors(theme)['secondary']).paint(
            painter,QRect((self.width()-24)//2,(self.height()-24)//2,24,24))


class _DeviceDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent); self.dark = False

    def sizeHint(self, option, index):
        return QSize(max(230, option.rect.width()), 56)

    @staticmethod
    def remove_rect(rect):
        return QRect(rect.right()-43,rect.center().y()-14,28,28)

    def paint(self, painter, option, index):
        painter.save(); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        background = '#344357' if self.dark else '#eaf2fe'
        if selected:
            painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(QColor(background))
            painter.drawRoundedRect(QRectF(option.rect.adjusted(5, 3, -5, -3)), 9, 9)
        icon_color = '#8fbbff' if self.dark else '#3178d9'
        platform=index.data(Qt.ItemDataRole.UserRole+1)
        if platform=='windows':
            art=ui_icon('windows','#0A84FF')
        else:art=ui_icon('apple' if platform=='macos' else 'desktop',icon_color)
        art.paint(painter,QRect(option.rect.left()+16,option.rect.top()+17,22,22))
        name = str(index.data(Qt.ItemDataRole.DisplayRole) or '')
        host = str(index.data(Qt.ItemDataRole.ToolTipRole) or '')
        primary = '#edf2fa' if self.dark else '#172537'
        secondary = '#a4b3c7' if self.dark else '#74849a'
        removable=getattr(self,'removable',False)
        width = max(20, option.rect.width()-(124 if removable else 82))
        online=index.data(Qt.ItemDataRole.UserRole+2)
        if online is not None:
            painter.setPen(Qt.PenStyle.NoPen);painter.setBrush(QColor('#30D158' if online else '#8E8E93'))
            painter.drawEllipse(QRectF(option.rect.right()-(64 if removable else 22),option.rect.center().y()-4,8,8))
        if removable:
            if index.data(Qt.UserRole)==getattr(self,'remove_hover_id',None):
                painter.setPen(Qt.PenStyle.NoPen);painter.setBrush(QColor('#663638' if self.dark else '#FFE6E5'))
                painter.drawRoundedRect(QRectF(self.remove_rect(option.rect)),6,6)
            ui_icon('close',secondary).paint(painter,self.remove_rect(option.rect).adjusted(6,6,-6,-6))
        painter.setFont(option.font)
        painter.setPen(QColor(primary))
        painter.drawText(QRectF(option.rect.left()+49,option.rect.top()+8,width,20),
                         Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(name,Qt.TextElideMode.ElideRight,width))
        painter.setPen(QColor(secondary))
        secondary_font = painter.font()
        secondary_font.setPointSizeF(max(8, secondary_font.pointSizeF() - 1))
        painter.setFont(secondary_font)
        same_account=bool(index.data(Qt.ItemDataRole.UserRole+3))
        if same_account:ui_icon('cloud','#0A84FF').paint(painter,QRect(option.rect.left()+49,option.rect.top()+31,14,14))
        inset=20 if same_account else 0
        painter.drawText(QRectF(option.rect.left()+49+inset,option.rect.top()+29,width-inset,18),
                         Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(host,Qt.TextElideMode.ElideRight,width-inset))
        painter.restore()


class _DropIllustration(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent); self.setFixedSize(28, 28); self.dark = False
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        from drop_target_motion import draw_drop_glyph
        draw_drop_glyph(painter,QRectF(self.rect()).adjusted(2,2,-2,-2),getattr(self.parentWidget(),'drop_motion',None),dark=self.dark)


class _PagerButton(QPushButton):
    def __init__(self, direction, parent=None):
        super().__init__(parent)
        self.direction = direction
        self.dark = False
        self.setFixedSize(28, 28)
        self.setAutoDefault(False)
        self.setDefault(False)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.direction == 'previous':
            painter.translate(self.width(), 0)
            painter.scale(-1, 1)
        color = ('#617083' if self.dark else '#8b99ac') if not self.isEnabled() else ('#c5d7f0' if self.dark else '#466480')
        draw_glyph(painter, 'chevron', QRectF(7, 7, 14, 14), color, 1.9)


class _DropZone(QFrame):
    dropped = Signal(list)
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('lanDropZone'); self.setAcceptDrops(True)
        self.setFixedHeight(120)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName('选择要发送的文件；也可拖入文件或文件夹')
        self._active = False
        self._hovered=False
        from drop_target_motion import DropTargetMotion
        self.drop_motion=DropTargetMotion(self,lambda:QRectF(self.rect()).adjusted(1,1,-1,-1),
                                          changed=self._drop_visual_changed)
        self._ants_timer=self.drop_motion.timer
        layout = QVBoxLayout(self); layout.setContentsMargins(16, 16, 16, 16); layout.setSpacing(10)
        layout.addStretch(1)
        illustration = _DropIllustration(self)
        self.illustration = illustration
        layout.addWidget(illustration, 0, Qt.AlignmentFlag.AlignHCenter)
        self.title = _label('拖入文件/文件夹，或点击添加', self); self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setObjectName('lanDropTitle');self.title.setAttribute(Qt.WA_TransparentForMouseEvents);layout.addWidget(self.title)
        self.hint = _label('', self, muted=True)
        self.hint.setObjectName('lanDropHint')
        self.hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter); layout.addWidget(self.hint)
        self.hint.hide()
        layout.addStretch(1)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def border_path(self):
        from drop_target_motion import target_path
        return target_path(QRectF(self.rect()).adjusted(1,1,-1,-1))

    def _drop_visual_changed(self):
        motion=getattr(self,'drop_motion',None)
        if motion is None:return
        self._hovered=motion.hovered
        if hasattr(self,'illustration'):self.illustration.update()

    def set_drag_active(self,active):
        self._active=bool(active);self.setProperty('dragActive',self._active)
        self.drop_motion.set_drag_active(self._active)

    def paintEvent(self,event):
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        from drop_target_motion import paint_target
        paint_target(painter,QRectF(self.rect()).adjusted(1,1,-1,-1),self.drop_motion,dark=self.illustration.dark)

    def enterEvent(self,event):
        super().enterEvent(event)

    def leaveEvent(self,event):
        super().leaveEvent(event)

    def hideEvent(self,event):
        self._active=False;self.setProperty('dragActive',False);self.drop_motion.stop();super().hideEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit(); event.accept(); return
        super().keyPressEvent(event)

    def mousePressEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton:
            self.clicked.emit();event.accept()
        else:super().mousePressEvent(event)


    @staticmethod
    def _paths(event):
        mime = event.mimeData()
        if not mime.hasUrls(): return []
        urls = mime.urls()
        if not urls or any(not url.isLocalFile() for url in urls): return []
        paths = [Path(url.toLocalFile()) for url in urls]
        return [str(path) for path in paths] if all(path.exists() for path in paths) else []

    def dragEnterEvent(self, event):
        if self._paths(event):
            self.set_drag_active(True)
            event.setDropAction(Qt.DropAction.CopyAction); event.accept()
        else: event.ignore()

    def dragMoveEvent(self, event):
        if self._paths(event): event.setDropAction(Qt.DropAction.CopyAction); event.accept()
        else: event.ignore()

    def dragLeaveEvent(self, event):
        self.set_drag_active(False)
        event.accept()

    def dropEvent(self, event):
        paths = self._paths(event)
        self.dragLeaveEvent(event)
        if paths:
            self.dropped.emit(paths)
            event.setDropAction(Qt.DropAction.CopyAction); event.accept()
        else: event.ignore()


class _CancelButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__('',parent); self.setFixedSize(28, 28); self.dark = False
        self.setToolTip('取消传输'); self.setAccessibleName('取消传输')
        self.setAutoDefault(False); self.setDefault(False)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter=QPainter(self);ui_icon('close',ui_colors('dark' if self.dark else 'light')['secondary']).paint(painter,self.rect().adjusted(6,6,-6,-6))


class _ElidedLabel(QLabel):
    """Keep a full accessible record while fitting names into one compact row."""

    def __init__(self, value='', parent=None):
        super().__init__(parent)
        self._full_text = ''
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(value)

    def setText(self, value):
        self._full_text = str(value)
        self.setAccessibleName(self._full_text)
        self.setToolTip(self._full_text)
        self._update_elision()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_elision()

    def _update_elision(self):
        width = max(1, self.contentsRect().width())
        super().setText(self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, width))


class _PayloadArea(QFrame):
    """Animate only the local drop band; the native window never resizes."""
    def __init__(self,panel):
        super().__init__(panel);self.panel=panel;self.filled=False;self.value=0.
        self.setFixedHeight(140);self.drop_zone=None;self.queue_column=None
        from ui_transitions import exit_curve
        self.animation=QVariantAnimation(self);self.animation.setDuration(200);self.animation.setEasingCurve(exit_curve())
        self.animation.valueChanged.connect(self._frame)

    def configure(self,drop,column):
        self.drop_zone=drop;self.queue_column=column
        self.panel.send_column.installEventFilter(self);self._layout()

    def set_filled(self,filled):
        changed=self.filled!=bool(filled);self.filled=bool(filled);target=float(self.filled)
        if not changed:self._layout();return
        self.animation.stop()
        if not self.isVisible() or reduced_motion():self._frame(target);return
        # Reversing before the old clock advances can have start == end.
        # Qt then emits no valueChanged; apply current visibility intent now.
        self._layout()
        self.animation.setStartValue(self.value);self.animation.setEndValue(target);self.animation.start()

    def _frame(self,value):
        if self.animation.state()==self.animation.State.Running and reduced_motion():
            self.animation.stop();value=float(self.filled)
        self.value=float(value);self._layout()

    def _layout(self):
        if self.drop_zone is None:return
        full=max(1,self.width()-40)
        compact=min(full,max(160,self.panel.send_column.width()-40))
        width=round(full+(compact-full)*self.value)
        # _DropZone's standalone preset is 120px. Match this band's 116px
        # content height explicitly: a fixed minimum otherwise silently makes
        # the lower gutter 8px while the upper gutter remains 12px.
        self.drop_zone.setFixedHeight(self.height()-24)
        self.drop_zone.setGeometry(20,12,width,self.height()-24)
        self.queue_column.setGeometry(20+width+16,12,max(0,full-width-16),116)
        self.queue_column.setVisible(self.filled and full-width>16)

    def eventFilter(self,watched,event):
        if event.type()==QEvent.Resize:self._layout()
        return False

    def resizeEvent(self,event):super().resizeEvent(event);self._layout()
    def showEvent(self,event):self._frame(float(self.filled));super().showEvent(event)
    def hideEvent(self,event):self.animation.stop();self._frame(float(self.filled));super().hideEvent(event)


class _HistoryIcon(QWidget):
    def __init__(self, direction, parent=None):
        super().__init__(parent)
        self.direction = direction
        self.dark = False
        self.setFixedSize(16, 20)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color=ui_colors('dark' if self.dark else 'light')['secondary']
        if getattr(self,'state','')=='完成':ui_icon('check',color).paint(painter,self.rect().adjusted(0,2,0,-2))
        else:
            from native_ui_common import cached_svg_renderer
            svg=f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3 10 18H2L12 3Z"/><path d="M12 9v4m0 4h.01"/></svg>'
            cached_svg_renderer(svg).render(painter,QRectF(self.rect().adjusted(0,2,0,-2)))


class _OfferIcon(QWidget):
    """Vector illustration for an incoming request, independent of display DPI."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.dark = False
        self.setFixedSize(38, 38)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#304761' if self.dark else '#e5f0ff'))
        painter.drawRoundedRect(QRectF(0, 0, 38, 38), 11, 11)
        draw_glyph(painter, 'receive', QRectF(9, 9, 20, 20),
                   '#90bdff' if self.dark else '#2373dd', 1.8)


class _HistoryRemoveButton(QPushButton):
    def __init__(self, parent=None, *, glyph='close', size=28):
        super().__init__(parent)
        self.glyph=glyph
        self.dark = False
        self.setObjectName('lanHistoryRemove')
        self.setFixedSize(size,size)
        self.setText('')
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAutoDefault(False)
        self.setDefault(False)
        self.setToolTip('删除这条传输记录')
        self.setAccessibleName('删除这条传输记录')

    def paintEvent(self, event):
        super().paintEvent(event)
        painter=QPainter(self)
        if not self.isEnabled():painter.setOpacity(.4)
        ui_icon(self.glyph,ui_colors('dark' if self.dark else 'light')['secondary']).paint(painter,QRect((self.width()-14)//2,(self.height()-14)//2,14,14))


def _file_type_preview(parent,name,*,directory=False):
    """Same bounded, format-based artwork as shelf/drop feedback, no Shell IO."""
    label=QLabel(parent);label.setFixedSize(20,24)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    _update_file_type_preview(label,name,directory=directory)
    return label


def _update_file_type_preview(label,name,*,directory=False):
    from flickdock_icons import document_pixmap
    suffix='folder' if directory else Path(str(name)).suffix.lstrip('.') or 'file'
    if label.property('fileType')==suffix:return
    label.setProperty('fileType',suffix)
    label.setPixmap(document_pixmap(suffix,20,dpr=label.devicePixelRatioF()))
    label.setAccessibleName('文件类型 '+suffix.upper())


class _HistoryRow(QFrame):
    remove_requested = Signal()
    reveal_requested = Signal()

    def __init__(self, record, parent=None):
        super().__init__(parent)
        self.setObjectName('lanHistoryRow')
        self.setFixedHeight(52)
        direction = record['direction']
        state = record['state']
        peer = record['peer'].strip() or '未知设备'
        title = ('发送至 ' if direction == 'send' else '接收自 ') + peer
        name = record['name'].strip()
        if (not name or name == title or (not record['peer'].strip() and name.startswith('发送至 '))
                or ('想发送 ' in name and '个文件' in name)):
            name = '文件传输'
        count = record.get('count')
        if isinstance(count, int) and count > 0:
            name = f'{name} · {count} 项'
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 5, 11, 5)
        layout.setSpacing(6)
        self.icon = _HistoryIcon(direction, self)
        self.icon.state=state
        self.icon.hide()
        paths=record.get('paths') or []
        self.file_preview=_file_type_preview(self,paths[0] if paths else record.get('name',''))
        layout.addWidget(self.file_preview)
        lines = QVBoxLayout()
        lines.setSpacing(1)
        self.title = _ElidedLabel(title, self)
        self.title.setObjectName('lanHistoryTitle')
        self.detail = _ElidedLabel(name, self)
        self.detail.setObjectName('lanHistoryDetail')
        error=str(record.get('error') or '')[:600]
        if error:
            self.title.setText(error)
            self.title.setToolTip(error);self.setAccessibleDescription(error)
            self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lines.addWidget(self.detail)
        lines.addWidget(self.title)
        layout.addLayout(lines, 1)
        trailing = QVBoxLayout()
        trailing.setSpacing(1)
        self.state = QLabel('已完成' if state == '完成' else state, self)
        self.state.setTextFormat(Qt.TextFormat.PlainText)
        self.state.setObjectName('lanHistoryState')
        self.state.setProperty('statusKind', {'完成': 'complete', '已取消': 'cancelled',
                                              '失败': 'failed','已中断':'cancelled'}.get(state, 'other'))
        self.state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state.setFixedWidth(52)
        self.state.setFixedHeight(22)
        self.when = QLabel(record['when'], self)
        self.when.setTextFormat(Qt.TextFormat.PlainText)
        self.when.setObjectName('lanHistoryWhen')
        self.when.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        trailing.addWidget(self.state, 0, Qt.AlignmentFlag.AlignRight)
        # Keep the timestamp accessible while the second line combines peer/time.
        self.when.hide()
        transport=(' · '+_turn_usage(record.get('relay_bytes')) if record.get('route')=='relay' or record.get('relay_bytes',0)>0 else ' · P2P' if record.get('route')=='direct' else '')
        self.title.setText((error if error else peer + ' · ' + record['when'])+transport)
        if transport:self.title.setToolTip(self.title.accessibleName()+'\nTURN 为本次中继连接数据量；不含网络协议开销，计费用量以 Cloudflare 为准。')
        layout.addLayout(trailing)
        self.reveal_button=_HistoryRemoveButton(self,glyph='folder')
        self.reveal_button.setAccessibleName('在文件夹中显示 '+name)
        self.reveal_button.setToolTip('在文件夹中显示' if record.get('paths') else '此记录没有保存文件位置')
        self.reveal_button.setEnabled(bool(record.get('paths')))
        self.reveal_button.clicked.connect(self.reveal_requested.emit);layout.addWidget(self.reveal_button,0,Qt.AlignVCenter)
        self.remove_button = _HistoryRemoveButton(self)
        self.remove_button.clicked.connect(self.remove_requested.emit)
        layout.addWidget(self.remove_button, 0, Qt.AlignmentFlag.AlignVCenter)

    def update_record(self,record):
        peer=record['peer'].strip() or '未知设备';state=record['state']
        name=record['name'].strip() or '文件传输';count=record.get('count')
        if isinstance(count,int) and count>0:name=f'{name} · {count} 项'
        error=str(record.get('error') or '')[:600]
        transport=(' · '+_turn_usage(record.get('relay_bytes')) if record.get('route')=='relay' or record.get('relay_bytes',0)>0 else ' · P2P' if record.get('route')=='direct' else '')
        self.detail.setText(name);self.title.setText((error or peer+' · '+record['when'])+transport)
        self.title.setToolTip(error);self.setAccessibleDescription(error)
        self.state.setText('已完成' if state=='完成' else state);self.when.setText(record['when'])
        self.reveal_button.setEnabled(bool(record.get('paths')))
        self.reveal_button.setToolTip('在文件夹中显示' if record.get('paths') else '此记录没有保存文件位置')
        self.reveal_button.setAccessibleName('在文件夹中显示 '+name)


class LanPanel(QFrame):
    """Overview's transfer surface. start() remains the owner's explicit decision.

    ``set_paths_provider(callback)`` optionally supplies selected shelf paths.
    If none are selected, the send button uses Windows/macOS native file picker.
    Received files stay in the chosen destination and never enter the shelf.
    """
    received_paths = Signal(list)
    transfer_started = Signal()
    lan_recommended = Signal(list,str)

    def __init__(self, service, parent=None, *, theme='light', settings=None,metadata_cache=None):
        super().__init__(parent)
        self.service = service; self.settings = settings; self._paths_provider = None
        self._wan=getattr(service,'mode','lan')=='wan'
        self._rows = {}; self._offers = {}; self._queued_paths = []; self._explicit_queue=False; self._row_sequence = 0
        from file_metadata import FileMetadataCache
        self.file_metadata=metadata_cache or FileMetadataCache(self)
        self.file_metadata.ready.connect(self._queue_info_ready);self._queue_row_map={};self._queue_labels={}
        self._history_reveal_pending=None;self.file_metadata.ready.connect(self._history_file_ready)
        self.file_metadata.deferred.connect(self._history_file_deferred)
        self.file_metadata.deferred.connect(self._queue_info_deferred)
        self._history_context = {}
        self._dark = False; self._samples = {}; self._started = False
        self._history_path = settings.base / ('wan_history.json' if self._wan else 'lan_history.json') if settings else None
        self._history = self._load_history()
        self.setObjectName('lanPanel'); self.setFont(QFont(reference_font().family(), 10))
        self.setMinimumHeight(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0)
        columns = QHBoxLayout(); columns.setContentsMargins(0,0,0,0); columns.setSpacing(0)
        # The shared payload band stays separate from mode-specific connection
        # fields and task/history scroll regions. Switching keeps one surface.
        self.queue_area=_PayloadArea(self);self.queue_area.setObjectName('lanPayloadArea')
        self.drop_zone=_DropZone(self.queue_area)
        self.drop_zone.layout().setContentsMargins(12,10,12,10);self.drop_zone.layout().setSpacing(6)
        self.drop_zone.dropped.connect(self.queue_paths);self.drop_zone.clicked.connect(self._pick_files)
        self.drop_zone.setContextMenuPolicy(Qt.CustomContextMenu);self.drop_zone.customContextMenuRequested.connect(self._pick_menu)
        queue_column=QWidget(self.queue_area);queue_box=QVBoxLayout(queue_column)
        self.queue_column=queue_column
        queue_box.setContentsMargins(0,0,0,0);queue_box.setSpacing(8)
        send_heading=QHBoxLayout();send_heading.setContentsMargins(0,0,0,0)
        self.send_heading_label=_label('待发送项目',queue_column);self.send_heading_label.setObjectName('lanSection')
        send_heading.addWidget(self.send_heading_label,1)
        self.clear_queue=_button('清除',queue_column);self.clear_queue.setObjectName('lanTextButton')
        self.clear_queue.setToolTip('清除待发送列表');self.clear_queue.clicked.connect(lambda:self.queue_paths([],replace=True))
        send_heading.addWidget(self.clear_queue);queue_box.addLayout(send_heading)
        self.queue_body=QFrame(queue_column);self.queue_body.setObjectName('lanQueueBody')
        self.queue_layout=QGridLayout(self.queue_body);self.queue_layout.setContentsMargins(0,0,0,0);self.queue_layout.setHorizontalSpacing(8);self.queue_layout.setVerticalSpacing(6)
        self.queue_layout.setColumnStretch(0,1);self.queue_layout.setColumnStretch(1,1)
        self.queue_layout.setSizeConstraint(QLayout.SetMinAndMaxSize);self.queue_rows=[]
        self.queue_scroll=QScrollArea(queue_column);self.queue_scroll.setObjectName('lanQueueScroll')
        self.queue_scroll.setFrameShape(QFrame.NoFrame);self.queue_scroll.setWidgetResizable(True)
        self.queue_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);self.queue_scroll.setWidget(self.queue_body)
        self.queue_scroll.verticalScrollBar().valueChanged.connect(self._queue_visible_info)
        self.queue_scroll.setMaximumHeight(84);queue_box.addWidget(self.queue_scroll);queue_box.addStretch(1)
        layout.addWidget(self.queue_area)
        self.send_column=QFrame(self);self.send_column.setObjectName('lanSendColumn')
        send=QVBoxLayout(self.send_column);send.setContentsMargins(20,16,20,16);send.setSpacing(10)
        target = QHBoxLayout()
        target_label = _label('发送到附近设备', self.send_column); target_label.setObjectName('lanSection')
        self.target_label=target_label
        target_label.setWordWrap(False)
        target_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        target.addWidget(target_label)
        self.peer_count = _label('正在搜索', self.send_column, muted=True)
        self.peer_count.setWordWrap(False)
        self.peer_count.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self.peer_count.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.scan_indicator = _ScanIndicator(self.send_column)
        target.addStretch(1)
        target.addWidget(self.scan_indicator); target.addWidget(self.peer_count)
        send.addLayout(target)
        self.devices = _DevicesCombo(self); self.devices.setMinimumHeight(36); self.devices.setMinimumWidth(0)
        self.devices.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.devices.setMinimumContentsLength(10); self.devices.setMaxVisibleItems(8)
        self.devices_view = QListView(self.devices)
        self.devices_view.setSpacing(2)
        self.device_delegate = _DeviceDelegate(self.devices_view)
        self.devices_view.setItemDelegate(self.device_delegate)
        self.devices.setView(self.devices_view)
        self.devices.removable=self._wan;self.device_delegate.removable=self._wan
        self.devices.remove_requested.connect(self._remove_recent)
        send.addWidget(self.devices)
        self.nearby_hint=QFrame(self.send_column);self.nearby_hint.setObjectName('lanNearbyRecommendation');nearby_row=QHBoxLayout(self.nearby_hint)
        nearby_row.setContentsMargins(10,8,10,8);nearby_row.setSpacing(8)
        nearby_text=_label('同一局域网，推荐直传',self.nearby_hint,muted=True)
        nearby_text.setObjectName('lanNearbyRecommendationText')
        nearby_text.setWordWrap(False);nearby_row.addWidget(nearby_text,1)
        self.use_lan_button=_button('使用局域网',self.nearby_hint);self.use_lan_button.setObjectName('lanNearbyRecommendationAction');nearby_row.addWidget(self.use_lan_button)
        self.use_lan_button.clicked.connect(self._recommend_lan);self.nearby_hint.hide()
        if self._wan:send.addWidget(self.nearby_hint)
        self.peer_code_label=_label('对方接收码',self.send_column)
        self.peer_code_label.setObjectName('lanSection');self.peer_code_label.setVisible(self._wan)
        if self._wan:send.addWidget(self.peer_code_label)
        self.peer_code_input=QLineEdit(self.send_column)
        self.peer_code_input.setPlaceholderText('输入 8 位本机码或 4 位一次性码')
        self.peer_code_input.setAccessibleName('对方的接收码');self.peer_code_input.setMaxLength(8)
        self.peer_code_input.setMinimumHeight(36);self.peer_code_input.setVisible(self._wan)
        if self._wan:send.addWidget(self.peer_code_input)
        self.devices.currentIndexChanged.connect(self._target_changed)
        self.peer_code_input.textChanged.connect(self._target_changed)
        send.addStretch(1)
        self.send_code = QFrame(self.send_column); self.send_code.setObjectName('lanSendCode')
        self.send_code.setFixedHeight(44)
        send_code_layout=QHBoxLayout(self.send_code);send_code_layout.setContentsMargins(12,4,12,4)
        send_code_label=_label('本机接收码' if self._wan else '确认码',self.send_code,muted=True);send_code_layout.addWidget(send_code_label,1)
        self.send_code_value=_CopyCodeButton('—',self.send_code);self.send_code_value.setObjectName('lanSendCodeValue')
        self.send_code_value.setSizePolicy(QSizePolicy.Policy.Preferred,QSizePolicy.Policy.Preferred)
        self.send_code_value.setFixedHeight(32)
        self.send_code_value.setAccessibleName('点击复制本机接收码' if self._wan else '点击复制确认码')
        send_code_layout.addWidget(self.send_code_value)
        self.code_countdown=None
        if self._wan and callable(getattr(service,'code_identity',None)):
            self.send_code_value.code_provider=lambda:service.code_identity().get('code','')
            if callable(getattr(service,'reconnect',None)):
                self.send_code_value.clicked.connect(lambda:service.reconnect() if not service.code_identity().get('code') else None)
            self.code_countdown=_CodeCountdown(service,self.send_code_value,self.send_code)
            send_code_layout.addWidget(self.code_countdown)
        if self._wan and hasattr(service,'identity_changed'):
            service.identity_changed.connect(self._code_identity_changed)
            self._code_identity_changed(service.code_identity() if callable(getattr(service,'code_identity',None))
                                        else dict(code=getattr(service,'code','')))
        send.addWidget(self.send_code)
        if self._wan and hasattr(service,'custom_code_changed'):
            custom_row=QHBoxLayout();custom_row.setSpacing(6)
            self.custom_input=QLineEdit(self.send_column);self.custom_input.setMaxLength(4)
            from PySide6.QtCore import QRegularExpression
            from PySide6.QtGui import QRegularExpressionValidator
            self.custom_input.setValidator(QRegularExpressionValidator(QRegularExpression("[0-9]{0,4}"),self.custom_input))
            self.custom_input.setInputMethodHints(Qt.ImhDigitsOnly)
            self.custom_input.setPlaceholderText('自定义一次性接收码 · 4 位数字')
            self.custom_input.setAccessibleName('自定义一次性接收码')
            self.custom_set=_button('设置',self.send_column);self.custom_cancel=_button('取消',self.send_column)
            for control in (self.custom_input,self.custom_set,self.custom_cancel):control.setFixedHeight(32)
            self.custom_set.clicked.connect(self._set_custom);self.custom_cancel.clicked.connect(self._cancel_custom)
            custom_row.addWidget(self.custom_input,1);custom_row.addWidget(self.custom_set);custom_row.addWidget(self.custom_cancel)
            send.addLayout(custom_row)
            status_row=QHBoxLayout();status_row.setSpacing(8)
            self.custom_value=_CopyCodeButton('—',self.send_column)
            self.custom_value.code_provider=lambda:service.custom_identity().get('code','') if service.custom_identity().get('status')=='available' and service.custom_identity().get('online') else ''
            self.custom_status=_label('',self.send_column,muted=True)
            status_row.addWidget(self.custom_value);status_row.addWidget(self.custom_status,1);send.addLayout(status_row)
            service.custom_code_changed.connect(self._custom_changed);self._custom_changed(service.custom_identity())
        self.send_button = _button('发送', self); self.send_button.setObjectName('lanPrimary')
        self.send_button.setProperty('pressScale',True)
        self.send_button.setMinimumHeight(36)
        self.send_button.clicked.connect(self._send)
        send.addWidget(self.send_button)
        # The WAN form has additional receive-code fields. Keep its natural
        # minimum height and scroll on short screens instead of letting Qt
        # resolve impossible fixed heights by overlapping adjacent controls.
        self.send_scroll=QScrollArea(self);self.send_scroll.setObjectName('lanSendScroll')
        self.send_scroll.setFrameShape(QFrame.Shape.NoFrame);self.send_scroll.setWidgetResizable(True)
        self.send_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        send.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.send_scroll.setWidget(self.send_column);columns.addWidget(self.send_scroll,5)

        self.activity_column = QFrame(self); self.activity_column.setObjectName('lanActivityColumn')
        activity = QVBoxLayout(self.activity_column); activity.setContentsMargins(20,20,20,16); activity.setSpacing(8)
        self.active_group=_ActivityGroup(self.activity_column);self.active_group.setObjectName('lanActiveGroup')
        self.active_group.setMinimumHeight(176)
        self.active_group.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Ignored)
        active=QVBoxLayout(self.active_group);active.setContentsMargins(12,12,12,12);active.setSpacing(8)
        activity.addWidget(self.active_group,1)
        transfer_heading = QHBoxLayout(); transfer_heading.setSpacing(8)
        section = _label('传输', self.active_group); section.setObjectName('lanSection')
        transfer_heading.addWidget(section, 1)
        self.transfer_count = _label('0', self.active_group)
        self.transfer_count.setObjectName('lanTransferCount');self.transfer_count.setAlignment(Qt.AlignCenter)
        self.transfer_count.setFixedHeight(22);self.transfer_count.setMinimumWidth(26)
        self.transfer_count.setSizePolicy(QSizePolicy.Policy.Minimum,QSizePolicy.Policy.Fixed)
        self.transfer_count.setAccessibleName('0 项进行中')
        transfer_heading.addWidget(self.transfer_count)
        active.addLayout(transfer_heading)
        self.transfers_body = QWidget(self.active_group)
        self.transfers_layout = QVBoxLayout(self.transfers_body)
        self.transfers_layout.setContentsMargins(3,3,3,3); self.transfers_layout.setSpacing(10)
        self.transfers_layout.addStretch(1)
        self.transfers_body.setMinimumHeight(76)
        active.addWidget(self.transfers_body, 1)
        self.transfers_empty = _label('目前没有正在进行的传输', self.transfers_body, muted=True)
        self.transfers_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.transfers_empty.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Expanding)
        self.transfers_layout.insertWidget(0,self.transfers_empty)
        self._row_page = 0
        pages = QHBoxLayout(); pages.setSpacing(8)
        self.previous_page = _PagerButton('previous', self.activity_column)
        self.previous_page.setAccessibleName('上一页传输')
        self.previous_page.clicked.connect(lambda:self._change_page(-1))
        self.next_page = _PagerButton('next', self.activity_column)
        self.next_page.setAccessibleName('下一页传输')
        self.next_page.clicked.connect(lambda:self._change_page(1))
        self.page_label = _label('', self.activity_column, muted=True)
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_label.setFixedWidth(48)
        self.page_label.setSizePolicy(QSizePolicy.Policy.Fixed,QSizePolicy.Policy.Preferred)
        pages.addStretch(1); pages.addWidget(self.previous_page)
        pages.addWidget(self.page_label); pages.addWidget(self.next_page)
        active.addLayout(pages)
        self.previous_page.hide(); self.next_page.hide(); self.page_label.hide()
        self.recent_group=_ActivityGroup(self.activity_column);self.recent_group.setObjectName('lanRecentGroup')
        self.recent_group.setMinimumHeight(176)
        # The history viewport's preferred height grows with saved rows. Both
        # tracks use the explicit shared minimum and equal stretch instead;
        # live offer content can still raise that minimum in the balancer.
        self.recent_group.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Ignored)
        recent=QVBoxLayout(self.recent_group);recent.setContentsMargins(12,12,12,12);recent.setSpacing(8)
        activity.addWidget(self.recent_group,1)
        section = _label('最近传输', self.recent_group); section.setObjectName('lanSection')
        history_heading=QHBoxLayout();history_heading.setContentsMargins(0,0,0,0);history_heading.addWidget(section,1)
        self.clear_history=_button('清除',self.recent_group);self.clear_history.setObjectName('lanTextButton')
        self.clear_history.setAccessibleName('清除最近传输记录');self.clear_history.setToolTip('只清除记录，保留文件')
        policy=self.clear_history.sizePolicy();policy.setRetainSizeWhenHidden(True);self.clear_history.setSizePolicy(policy)
        self.clear_history.clicked.connect(self._clear_history_records);history_heading.addWidget(self.clear_history)
        recent.addLayout(history_heading)
        self.history_body = QFrame(self.recent_group)
        self.history_body.setObjectName('lanHistoryGroup')
        self.history_layout = QVBoxLayout(self.history_body)
        self.history_layout.setContentsMargins(0, 0, 0, 0)
        self.history_layout.setSpacing(4)
        self.history_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.history_label = _label('还没有传输记录', self.history_body, muted=True)
        self.history_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.history_label.setMinimumHeight(48)
        self.history_layout.addWidget(self.history_label)
        self.history_rows = []
        self.history_separators = []
        self.history_scroll = QScrollArea(self.recent_group)
        self.history_scroll.setObjectName('lanHistoryScroll')
        self.history_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.history_scroll.setWidgetResizable(True)
        self.history_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.history_scroll.setMinimumHeight(110)
        self.history_scroll.setMaximumHeight(16777215)
        self.history_scroll.setWidget(self.history_body)
        recent.addWidget(self.history_scroll,1)
        from transfer_item_motion import TransferItemMotion
        self.queue_motion=TransferItemMotion(self,self.queue_scroll.viewport())
        self.history_motion=TransferItemMotion(self,self.history_scroll.viewport())
        self._render_history()
        destination = QHBoxLayout(); destination.setSpacing(8)
        destination_label=_label('接收位置：', self, muted=True)
        destination_label.setSizePolicy(QSizePolicy.Policy.Fixed,QSizePolicy.Policy.Preferred)
        destination.addWidget(destination_label)
        self.receive_path = QLineEdit(self); self.receive_path.setReadOnly(True)
        self.receive_path.setAccessibleName('局域网接收位置')
        self.receive_path.setText(str(settings.lan_receive_dir if settings else Path.home()/'Downloads'))
        self.receive_path.setToolTip(self.receive_path.text())
        destination.addWidget(self.receive_path, 1)
        self.choose_destination = _button('更改...', self); self.choose_destination.clicked.connect(self._choose_destination)
        self.receive_path.setFixedHeight(32);self.choose_destination.setFixedHeight(32)
        destination.addWidget(self.choose_destination); activity.addLayout(destination)
        # Preserve the natural minimum of both right-hand sections on short
        # logical screens. Otherwise Qt compresses the layout past its fixed
        # children and overlays history, headings and the destination field.
        self.activity_scroll=QScrollArea(self);self.activity_scroll.setObjectName('lanActivityScroll')
        self.activity_scroll.setFrameShape(QFrame.Shape.NoFrame);self.activity_scroll.setWidgetResizable(True)
        self.activity_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        activity.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.activity_scroll.setWidget(self.activity_column);columns.addWidget(self.activity_scroll,7)
        self._activity_balance_timer=QTimer(self);self._activity_balance_timer.setSingleShot(True)
        self._activity_balance_timer.timeout.connect(self._balance_activity_groups)
        self.active_group.installEventFilter(self);self.recent_group.installEventFilter(self)
        self._activity_balance_timer.start(0)
        layout.addLayout(columns, 1)
        self.status_label = _StatusLabel('', self)
        self.status_label.hide()
        self.status_label.setObjectName('lanStatus')
        self.status_label.setMaximumHeight(20)
        layout.addWidget(self.status_label)
        self.service.peers_changed.connect(self._peers_changed)
        self.service.offer_received.connect(self._offer)
        self.service.progress.connect(self._progress)
        self.service.completed.connect(self._completed)
        self.service.status_changed.connect(self.status_label.setText)
        self.queue_area.configure(self.drop_zone,self.queue_column)
        self._peers_changed(self.service.peers()); self.queue_paths([],replace=True); self.apply_theme(theme)
        self._explicit_queue=False  # Initial empty rendering is not a user-cleared queue.

    def set_paths_provider(self, callback): self._paths_provider = callback

    def _balance_activity_groups(self):
        # Two equal flex tracks, including when an incoming offer raises the
        # natural minimum. The outer activity viewport scrolls on short screens.
        active_layout=self.active_group.layout()
        natural=max(active_layout.sizeHint().height(),active_layout.totalHeightForWidth(self.active_group.contentsRect().width()))
        height=max(176,natural+2,self.recent_group.layout().minimumSize().height()+2)
        for group in (self.active_group,self.recent_group):
            if group.minimumHeight()!=height:group.setMinimumHeight(height)

    def eventFilter(self,watched,event):
        if watched in (getattr(self,'active_group',None),getattr(self,'recent_group',None)):
            if (event.type()==QEvent.LayoutRequest or event.type()==QEvent.Resize
                    and event.size().width()!=event.oldSize().width()):
                self._activity_balance_timer.start(0)
        return super().eventFilter(watched,event)

    def _load_history(self):
        if not self._history_path: return []
        try:
            value = json.loads(self._history_path.read_text(encoding='utf-8'))
            if isinstance(value, list):
                return [row for row in value[:12] if isinstance(row, dict)
                        and all(isinstance(row.get(key), str) for key in ('name', 'peer', 'state', 'direction', 'when'))]
        except (OSError, ValueError, UnicodeError): pass
        return []

    def _set_history_context(self, identifier, paths):
        context = {'history_name': Path(paths[0]).name if paths else '',
                   'history_count': len(paths),'history_paths':[str(paths[0])] if paths else []}
        if identifier in self._rows:
            self._rows[identifier].update(context)
        else:
            self._history_context[identifier] = context

    def _save_history(self):
        if not self._history_path: return
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._history_path.with_name('.lan-history-' + str(time.time_ns()) + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as stream:
                json.dump(self._history[:12], stream, ensure_ascii=False)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, self._history_path)
        finally:
            if temporary.exists(): temporary.unlink()

    def _render_history(self, *, preserve_motion=False):
        self.clear_history.setVisible(bool(self._history))
        if not preserve_motion:self.history_motion.cancel()
        rows={widget.record_identity:widget for widget in self.history_rows}
        separators=getattr(self,'_history_separator_map',{})
        records=self._history[:12];wanted={id(record) for record in records}
        for key,widget in rows.items():
            if key not in wanted:
                self.history_layout.removeWidget(widget);widget.hide();widget.deleteLater()
        separator_keys={id(record) for record in records[1:]}
        for key,separator in tuple(separators.items()):
            if key not in separator_keys:
                self.history_layout.removeWidget(separator);separator.hide();separator.deleteLater();del separators[key]
        current=[];ordered=[]
        self.history_label.setVisible(not self._history)
        for index,record in enumerate(records):
            key=id(record)
            if index:
                separator=separators.get(key)
                if separator is None:
                    separator=QFrame(self.history_body);separator.setObjectName('lanHistoryDivider');separator.setFixedHeight(1)
                    separators[key]=separator
                ordered.append(separator)
            widget=rows.get(key)
            if widget is None:
                widget=_HistoryRow(record,self.history_body);widget.record_identity=key
                widget.icon.dark=self._dark;widget.remove_button.dark=self._dark;widget.reveal_button.dark=self._dark
                widget.reveal_requested.connect(lambda record=record:self._reveal_history(record))
                widget.remove_requested.connect(lambda record=record:self._delete_history_identity(record))
                widget.ensurePolished();widget.layout().activate()
            elif getattr(widget,'_history_record_snapshot',None)!=record:widget.update_record(record)
            widget._history_record_snapshot=dict(record)
            ordered.append(widget);current.append(widget)
        for index,widget in enumerate(ordered,1):
            if self.history_layout.indexOf(widget)!=index:self.history_layout.insertWidget(index,widget)
            if widget.isHidden():widget.show()
        self.history_rows=current;self._history_separator_map=separators
        self.history_separators=[separators[id(record)] for record in records[1:]]
        self.history_layout.activate();self.history_body.updateGeometry()

    def _delete_history_identity(self,record):
        index=next((index for index,current in enumerate(self._history) if current is record),-1)
        if index>=0:self._delete_history_record(index)

    def _reveal_history(self,record):
        paths=record.get('paths')
        if not isinstance(paths,list) or not paths or not isinstance(paths[0],str):return
        self._history_reveal_pending=(record,paths[0])
        self.status_label.setText('正在定位文件…');self._history_file_ready(paths[0])

    def _history_file_ready(self,path):
        pending=self._history_reveal_pending
        if pending is None or path!=pending[1]:return
        if not self.isVisible() or not any(row is pending[0] for row in self._history):
            self._history_reveal_pending=None;return
        info=self.file_metadata.request(path)
        if info is None:return
        self._history_reveal_pending=None
        if not info.get('exists'):self.status_label.setText('文件已移动或删除，无法定位。');return
        try:
            import subprocess
            if sys.platform=='win32':
                from windows_reveal import request_reveal
                record=pending[0];self._history_native_reveal_record=record
                def done(error):
                    if any(row is record for row in self._history):
                        self.status_label.setText('无法打开文件所在位置：'+str(error) if error else '')
                request_reveal(self,path,done);return
            elif sys.platform=='darwin':subprocess.Popen(['/usr/bin/open','-R',path])
            else:QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))
            self.status_label.clear()
        except OSError:self.status_label.setText('无法打开文件所在位置。')

    def _history_file_deferred(self,path):
        if self._history_reveal_pending and self._history_reveal_pending[1]==path:
            self._history_reveal_pending=None
            self.status_label.setText('文件位置暂不可用，请稍后重试。')

    def hideEvent(self,event):
        self._history_reveal_pending=None;super().hideEvent(event)

    def _delete_history_record(self, index):
        """Forget one completed transfer without touching its files or live transfer."""
        if not 0 <= index < len(self._history):
            return
        record = self._history.pop(index)
        try:
            self._save_history()
        except OSError:
            self._history.insert(index, record)
            self.status_label.setText('无法删除传输记录，请检查保存位置。')
            return
        if self._history_reveal_pending and self._history_reveal_pending[0] is record:self._history_reveal_pending=None
        if getattr(self,'_history_native_reveal_record',None) is record:
            from windows_reveal import cancel_reveal
            cancel_reveal(self);self._history_native_reveal_record=None
        positions=self.history_motion.capture({widget.record_identity:widget for widget in self.history_rows})
        self.history_motion.remove({id(record):self.history_rows[index]},positions,style='slide')
        self._render_history(preserve_motion=True);self.history_layout.activate()
        self.history_motion.reflow({widget.record_identity:widget for widget in self.history_rows},positions)

    def _clear_history_records(self):
        if not self._history:return
        records=self._history;self._history=[]
        try:self._save_history()
        except OSError:
            self._history=records;self.status_label.setText('无法清除传输记录，请检查保存位置。');return
        self._history_reveal_pending=None
        if getattr(self,'_history_native_reveal_record',None) is not None:
            from windows_reveal import cancel_reveal
            cancel_reveal(self);self._history_native_reveal_record=None
        widgets={widget.record_identity:widget for widget in self.history_rows}
        positions=self.history_motion.capture(widgets)
        self.history_motion.remove(widgets,positions,style='slide',stagger=True)
        self._render_history(preserve_motion=True);self.history_layout.activate()
        self.history_motion.reflow({},positions)

    def queue_paths(self, paths, *, replace=False):
        self._explicit_queue=True
        previous=list(self._queued_paths)
        if replace:self._queued_paths=[]
        present=set(self._queued_paths)
        for value in paths:
            path = Path(value)
            if str(path) not in present:self._queued_paths.append(str(path));present.add(str(path))
        if previous==self._queued_paths and getattr(self,'_queue_rendered',False):return
        removed={value:widget for value,widget in self._queue_row_map.items() if value not in self._queued_paths}
        animate_removal=bool(removed or self.queue_motion.moves or self.queue_motion.ghosts or self.queue_motion.pending)
        positions=self.queue_motion.capture(self._queue_row_map) if animate_removal else {}
        if removed:self.queue_motion.remove(removed,positions,style='shrink')
        self._queue_rendered=True
        count = len(self._queued_paths)
        self.drop_zone.title.setText('拖入文件/文件夹，或点击添加')
        self.drop_zone.hint.hide()
        self.clear_queue.setVisible(bool(count))
        self.send_button.setText(f'发送 {count} 项' if count else '发送文件')
        self.send_heading_label.setText(f'待发送项目 · {count}' if count else '待发送项目')
        wanted=set(self._queued_paths);added=[]
        for value in list(self._queue_row_map):
            if value not in wanted:
                widget=self._queue_row_map.pop(value);self._queue_labels.pop(value,None)
                self.queue_layout.removeWidget(widget);widget.hide();widget.deleteLater()
        for value in self._queued_paths:
            if value in self._queue_row_map:continue
            row=QFrame(self.queue_body);row.setObjectName('lanQueueRow')
            from transfer_drop_feedback import ScaleFade
            row.entrance=ScaleFade(row,viewport=self.queue_scroll.viewport())
            row.setFixedHeight(36)
            line=QHBoxLayout(row);line.setContentsMargins(12,4,5,4);line.setSpacing(8)
            path=Path(value);row.file_preview=_file_type_preview(row,value);line.addWidget(row.file_preview)
            name=_ElidedLabel(path.name,row);line.addWidget(name,1)
            size=_label('读取信息中…',row,muted=True);size.setWordWrap(False);size.setSizePolicy(QSizePolicy.Policy.Fixed,QSizePolicy.Policy.Preferred)
            line.addWidget(size)
            remove=_HistoryRemoveButton(row,size=24);remove.setAccessibleName('移除待发送文件 '+path.name);remove.setToolTip('移出待发送队列')
            remove.clicked.connect(lambda _,ref=value:self.queue_paths([path for path in self._queued_paths if path!=ref],replace=True));line.addWidget(remove)
            self._queue_row_map[value]=row;self._queue_labels[value]=size
            self._queue_info_ready(value);added.append(value)
        self.queue_rows=[self._queue_row_map[value] for value in self._queued_paths]
        for row in self.queue_rows:self.queue_layout.removeWidget(row)
        for index,row in enumerate(self.queue_rows):self.queue_layout.addWidget(row,index//2,index%2)
        rows=(count+1)//2;self.queue_body.setFixedHeight(rows*36+max(0,rows-1)*6);self.queue_layout.activate()
        self.queue_scroll.setFixedHeight(min(84, rows*42+4))
        self.queue_scroll.setVisible(bool(count))
        self.queue_area.set_filled(bool(count))
        for value in added:self._queue_row_map[value].entrance.arm()
        if animate_removal:self.queue_motion.reflow(self._queue_row_map,positions)

    def _queue_info_ready(self,ref):
        label=self._queue_labels.get(ref)
        if label is None:return
        info=self.file_metadata.request(ref)
        if info is None:return
        row=self._queue_row_map.get(ref)
        if row is not None:_update_file_type_preview(row.file_preview,ref,directory=info['is_dir'])
        label.setText('文件夹' if info['is_dir'] else _size(info['size']) if info['exists'] else '无法读取文件信息')

    def _queue_info_deferred(self,ref):
        label=self._queue_labels.get(ref)
        if label is not None:label.setText('待读取')

    def _queue_visible_info(self,*_):
        start=max(0,self.queue_scroll.verticalScrollBar().value()//42)
        for ref in self._queued_paths[start*2:start*2+8]:self._queue_info_ready(ref)

    def _pick_menu(self, position):
        menu = QMenu(self.drop_zone)
        menu.addAction('选择文件…', self._pick_files)
        menu.addAction('选择文件夹…', self._pick_folder)
        menu.exec(self.drop_zone.mapToGlobal(position))

    def _pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, '选择要发送的文件')
        self.queue_paths(paths)

    def _pick_folder(self):
        path = QFileDialog.getExistingDirectory(self, '选择要发送的文件夹')
        if path: self.queue_paths([path])

    def _choose_destination(self):
        path = QFileDialog.getExistingDirectory(self, '选择接收位置', self.receive_path.text())
        if not path: return
        try:
            if self.settings: self.settings.set_lan_receive_dir(path)
            self.receive_path.setText(path)
            self.receive_path.setToolTip(path)
            for row in self._rows.values():
                if row.get('offer_destination'):
                    row['offer_destination'].setText('保存到 ' + path)
        except (ValueError, OSError) as error: self.status_label.setText(str(error))

    def apply_theme(self, theme):
        if getattr(self,'_applied_theme',None)==theme:return
        self._applied_theme=theme
        self._dark = theme == 'dark'
        self.device_delegate.dark = self._dark
        c=ui_colors(theme)
        self.setProperty('uiTheme',theme)
        font=reference_font()
        font.setPixelSize(13);self.setFont(font)
        color,muted,border,field,accent=c['text'],c['secondary'],c['border'],c['input'],c['action']
        faint='rgba(255,255,255,8)' if self._dark else 'rgba(0,0,0,6)'
        style=f"""
            QFrame#lanPanel {{ background:transparent; border:0; }}
            QFrame#lanPanel QLabel {{ color:{color}; background:transparent; border:0; font-size:12px; }}
            QFrame#lanPanel QLabel#lanSection {{ font-size:12px; font-weight:700; }}
            QFrame#lanPanel QLabel#lanTransferCount {{ background:{c['control']};border:1px solid {border};border-radius:7px;padding:0 7px;color:{color};font-size:11px;font-weight:600; }}
            QFrame#lanPanel QLabel#lanMuted {{ color:{muted}; }}
            QFrame#lanPanel QLabel#lanStatus {{ color:{c['red']}; font-size:11px; padding:2px 20px; }}
            QFrame#lanSendColumn {{ background:{faint}; border:0; border-right:1px solid {c['divider']}; }}
            QFrame#lanPayloadArea {{background:{c['surface']};border:0;border-bottom:1px solid {c['divider']};}}
            QFrame#lanActivityColumn, QFrame#lanQueueBody, QFrame#lanHistoryGroup {{ background:transparent; border:0; }}
            QFrame#lanActiveGroup, QFrame#lanRecentGroup {{ background:{faint}; border:1px solid {border}; border-radius:8px; }}
            QFrame#lanQueueRow, QFrame#lanHistoryRow {{ background:{faint}; border:1px solid {border}; border-radius:8px; }}
            QFrame#lanHistoryDivider {{ background:transparent; border:0; max-height:0; }}
            QFrame#lanTransferRow {{ background:{c['card']}; border:1px solid {border}; border-radius:8px; }}
            QFrame#lanTransferRow[offerFrame="true"] {{ background:transparent; border:0; }}
            QFrame#lanOfferCard {{ background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 rgba(10,132,255,51),stop:1 rgba(86,125,175,26)); border:1px solid rgba(10,132,255,102); border-radius:16px; }}
            QFrame#lanPanel QLabel#lanOfferKicker {{ color:white; background:{accent}; border-radius:8px; font-size:11px; font-weight:700; padding:2px 8px; }}
            QFrame#lanPanel QLabel#lanOfferPeer {{ color:{color}; font-size:12px; font-weight:700; }}
            QFrame#lanPanel QLabel#lanOfferFile {{ color:{muted}; font-size:12px; }}
            QFrame#lanPanel QLabel#lanOfferMeta {{ color:{muted}; font-size:11px; }}
            QFrame#lanOfferCode {{ background:transparent; border:0; }}
            QFrame#lanPanel QLabel#lanOfferCodeCaption {{ color:{muted}; font-size:11px; }}
            QFrame#lanPanel QPushButton#lanOfferCodeValue {{ color:{color}; font-size:18px; font-weight:700; font-family:monospace; }}
            QFrame#lanPanel QLabel#lanTransferTitle {{ color:{color}; font-size:12px; font-weight:700; }}
            QFrame#lanPanel QLabel#lanTransferDetail {{ color:{muted}; font-size:11px; }}
            QFrame#lanPanel QLabel#lanTransferMetrics {{color:{muted};font-size:11px;}}
            QFrame#lanPanel QLabel#lanTransferDirection {{color:{c['link']};background:{'rgba(10,132,255,22)' if self._dark else '#E9F3FF'};border:1px solid {accent};border-radius:5px;font-size:11px;padding:0;}}
            QFrame#lanPanel QLabel#lanTransferDirection[transferDirection="receive"] {{color:{c['green']};border-color:{c['green']};background:{'rgba(48,209,88,20)' if self._dark else '#EAF8EF'};}}
            QFrame#lanPanel QLabel#lanTransferRoute {{color:{muted};background:{c['control']};border:1px solid {border};border-radius:5px;padding:0 7px;font-size:11px;}}
            QFrame#lanPanel QLabel#lanTransferRoute[relayRoute="true"] {{color:{c['orange']};border-color:{c['orange']};}}
            QFrame#lanNearbyRecommendation {{background:{'rgba(10,132,255,22)' if self._dark else '#EAF3FF'};border:1px solid {accent};border-radius:6px;}}
            QFrame#lanPanel QLabel#lanNearbyRecommendationText {{color:{c['link']};font-size:11px;}}
            QFrame#lanPanel QPushButton#lanNearbyRecommendationAction {{background:{accent};color:white;border:0;border-radius:6px;font-size:11px;padding:4px 9px;}}
            QFrame#lanPanel QPushButton#lanOfferAccept {{ color:#0D0D0E; background:{c['green']}; border-color:transparent; font-weight:700; }}
            QFrame#lanPanel QLabel#lanHistoryTitle {{ color:{c['muted']}; font-size:11px; font-weight:400; }}
            QFrame#lanPanel QLabel#lanHistoryDetail {{ color:{color}; font-size:12px; font-weight:500; }}
            QFrame#lanPanel QLabel#lanHistoryWhen {{ color:{muted}; font-size:11px; }}
            QFrame#lanPanel QLabel#lanHistoryState {{ background:transparent; border:1px solid transparent; border-radius:5px;font-size:10px; }}
            QFrame#lanPanel QLabel#lanHistoryState[statusKind="complete"] {{ color:{c['green']};border-color:{c['green']}; }}
            QFrame#lanPanel QLabel#lanHistoryState[statusKind="cancelled"] {{ color:{c['orange']};border-color:{c['orange']}; }}
            QFrame#lanPanel QLabel#lanHistoryState[statusKind="failed"] {{ color:{c['red']};border-color:{c['red']}; }}
            QFrame#lanPanel QLabel#lanHistoryState[statusKind="other"] {{ color:{muted}; }}
            QFrame#lanPanel QPushButton#lanHistoryRemove {{ background:{c['control']}; border:1px solid {border}; border-radius:6px; color:{c['muted']}; padding:0; font-size:12px; }}
            QFrame#lanPanel QPushButton#lanHistoryRemove:hover {{ background:{c['hover']}; color:{color}; }}
            QFrame#lanDropZone {{ background:transparent; border:0; }}
            QFrame#lanDropZone QLabel#lanDropTitle {{ font-size:12px; font-weight:500; color:{color}; }}
            QFrame#lanDropZone QLabel#lanDropHint {{ font-size:11px; color:{muted}; }}
            QFrame#lanSendCode {{ background:{c['card']}; border:1px solid {border}; border-radius:8px; }}
            QFrame#lanPanel QPushButton#lanSendCodeValue {{ color:{c['cyan'] if self._dark else accent}; font-size:16px; font-weight:700; font-family:monospace; }}
            QFrame#lanPanel QPushButton {{ color:{color}; background:{c['control']}; border:1px solid transparent; border-radius:8px; padding:4px 12px; font-size:12px; }}
            QFrame#lanPanel QPushButton#lanTextButton {{ color:{muted}; background:transparent; border:0; padding:2px 4px; min-height:18px; }}
            QFrame#lanPanel QPushButton#lanTextButton:hover {{ color:{accent}; background:{c['hover']}; }}
            QFrame#lanPanel QPushButton#lanPrimary {{ color:white; background:{accent}; border-color:transparent; border-radius:8px; font-weight:700; }}
            QFrame#lanPanel QPushButton#lanPrimary:disabled {{ color:{c['muted']}; background:{c['control']}; border-color:transparent; }}
            QFrame#lanPanel QPushButton[accessibleName="取消传输"] {{ color:{c['red']}; background:transparent; border:0; padding:0; }}
            QFrame#lanPanel QPushButton:hover {{ background:{c['hover']}; }}
            QFrame#lanPanel QPushButton:pressed {{ background:{c['selected']}; }}
            QFrame#lanPanel QPushButton:disabled {{ color:{c['muted']}; }}
            QFrame#lanPanel QComboBox {{ color:{color}; background:{field}; border:1px solid {c['input_border']}; border-radius:8px; padding:5px 28px 5px 10px; font-size:12px; }}
            QFrame#lanPanel QComboBox::drop-down {{ border:0; width:26px; }}
            QFrame#lanPanel QComboBox::down-arrow {{ image:none; width:0; }}
            QFrame#lanPanel QAbstractItemView {{ background:{field}; color:{color}; border:1px solid {border}; border-radius:8px; padding:4px; outline:0; }}
            QFrame#lanPanel QLineEdit {{ color:{color}; background:{c['control']}; border:0; border-radius:8px; padding:3px 6px; font-size:11px; font-family:monospace; }}
            QFrame#lanPanel QPushButton[accessibleName="上一页传输"], QFrame#lanPanel QPushButton[accessibleName="下一页传输"] {{ min-width:24px; max-width:24px; min-height:24px; max-height:24px; padding:0; }}
            QFrame#lanPanel QProgressBar {{ border:0; background:{c['control']}; height:6px; max-height:6px; border-radius:3px; }}
            QFrame#lanPanel QProgressBar::chunk {{ background:{c['cyan']}; border-radius:3px; }}
        """
        style+=f'''
            QFrame#lanPanel QLabel#lanOfferKicker {{background:{c['action']};color:{c['on_action']};}}
            QFrame#lanPanel QPushButton#lanTextButton:hover {{color:{c['link']};}}
            QFrame#lanPanel QPushButton[keyboardFocus="true"] {{border:1px solid {c['link']};}}
            QFrame#lanPanel QPushButton#lanOfferAccept {{background:{c['action']};color:{c['on_action']};}}
            QFrame#lanPanel QLabel {{font-size:13px;}}
            QFrame#lanPanel QLabel#lanSection {{font-size:13px;font-weight:600;}}
            QFrame#lanPanel QLabel#lanDropTitle {{font-size:13px;}}
            QFrame#lanPanel QLabel#lanHistoryTitle {{font-size:10px;color:{c['secondary']};}}
            QFrame#lanPanel QLabel#lanHistoryDetail {{font-size:12px;font-weight:500;}}
            QFrame#lanPanel QLabel#lanHistoryState {{font-size:10px;}}
            QFrame#lanPanel QPushButton, QFrame#lanPanel QComboBox {{font-size:13px;}}
            QFrame#lanPanel QLineEdit {{font-size:12px;}}
            QScrollArea#lanSendScroll, QScrollArea#lanQueueScroll, QScrollArea#lanHistoryScroll, QScrollArea#lanActivityScroll {{background:transparent;border:0;}}
            QScrollArea#lanQueueScroll > QWidget > QWidget, QScrollArea#lanHistoryScroll > QWidget > QWidget, QScrollArea#lanActivityScroll > QWidget > QWidget {{background:transparent;}}
            QScrollBar:vertical {{background:transparent;width:6px;margin:2px 0;}}
            QScrollBar::handle:vertical {{background:{c['input_border']};border-radius:3px;min-height:24px;}}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{height:0;}}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{background:transparent;}}
        '''
        from native_ui_common import readable_qss
        style=readable_qss(style)
        from input_focus import focus_qss,install_focus_tree
        style+=focus_qss('QFrame#lanPanel',c['input_border'])
        from control_states import disabled_fields_qss
        style+=disabled_fields_qss('QFrame#lanPanel',c)
        self.setStyleSheet(style)
        install_focus_tree(self,theme=lambda:self._applied_theme)
        for row in self._rows.values():
            row['cancel'].dark = self._dark; row['cancel'].update()
            if row.get('offer_icon'):
                row['offer_icon'].dark = self._dark; row['offer_icon'].update()
            if row.get('offer_shadow'):
                row['offer_shadow'].setColor(QColor(0,0,0,48 if self._dark else 18))
        for row in self.history_rows:
            row.icon.dark = self._dark; row.icon.update()
            row.remove_button.dark = self._dark; row.remove_button.update()
        for button in (self.previous_page,self.next_page):
            button.dark=self._dark;button.update()
        if self.code_countdown:self.code_countdown.dark=self._dark;self.code_countdown.update()
        self.drop_zone.illustration.dark=self._dark;self.drop_zone.illustration.update()
        self.scan_indicator.dark=self._dark;self.scan_indicator.update()

    def _peers_changed(self, peers):
        if self._wan:
            selected=self.devices.currentData();scroll=self.devices.view().verticalScrollBar().value()
            blocked=self.devices.blockSignals(True);wanted={peer['id'] for peer in peers}
            for index in range(self.devices.count()-1,-1,-1):
                if self.devices.itemData(index) not in wanted:self.devices.removeItem(index)
            for peer in peers:
                name=str(peer.get('name') or '未命名设备')
                index=self.devices.findData(peer['id'])
                if index<0:self.devices.addItem('',peer['id']);index=self.devices.count()-1
                self.devices.setItemText(index,name)
                same_account=bool(peer.get('account_device'))
                self.devices.setItemIcon(index,ui_icon('cloud','#0A84FF') if same_account else QIcon())
                self.devices.setItemData(index,('同账号设备，' if same_account else '')+('在线' if peer.get('online') else '离线'),Qt.ItemDataRole.AccessibleDescriptionRole)
                self.devices.setItemData(index,'在线' if peer.get('online') else '离线',Qt.ItemDataRole.ToolTipRole)
                self.devices.setItemData(index,peer.get('platform','unknown'),Qt.ItemDataRole.UserRole+1)
                self.devices.setItemData(index,bool(peer.get('online')),Qt.ItemDataRole.UserRole+2)
                self.devices.setItemData(index,bool(peer.get('account_device')),Qt.ItemDataRole.UserRole+3)
            index=self.devices.findData(selected)
            self.devices.setCurrentIndex(index if index>=0 else 0 if peers else -1)
            self.devices.blockSignals(blocked);self.devices.view().verticalScrollBar().setValue(scroll)
            self.target_label.setText(('同账号与最近设备' if any(p.get('account_device') for p in peers) else '最近传输设备')+(f' ({len(peers)})' if peers else ''))
            self.peer_count.setText(f'{sum(bool(p.get("online")) for p in peers)} 台在线')
            self.scan_indicator.online=any(p.get('online') for p in peers)
            self.scan_indicator.set_scanning(False);self.send_button.setEnabled(True)
            self._target_changed()
            return
        selected = self.devices.currentData(); self.devices.clear()
        for peer in peers:
            name=str(peer.get('name') or '未命名设备')
            host=str(peer.get('host') or '')
            self.devices.addItem(name + (' ('+host+')' if host else ''), peer['id'])
            self.devices.setItemData(self.devices.count()-1,str(peer.get('host') or '局域网设备'),Qt.ItemDataRole.ToolTipRole)
            self.devices.setItemData(self.devices.count()-1,peer.get('platform','unknown'),Qt.ItemDataRole.UserRole+1)
            self.devices.setItemData(self.devices.count()-1,True,Qt.ItemDataRole.UserRole+2)
        if selected is not None:
            index = self.devices.findData(selected)
            if index >= 0: self.devices.setCurrentIndex(index)
        self.target_label.setText('发送到附近设备'+(f' ({len(peers)})' if peers else ''))
        self.peer_count.setText('在线' if peers else '正在搜索')
        self.scan_indicator.set_scanning(not peers)
        if not peers: self.devices.addItem('附近暂无设备', '')
        self.devices.setEnabled(bool(peers))
        self.send_button.setEnabled(bool(peers))

    def _code_identity_changed(self,identity):
        if self.code_countdown:self.code_countdown._tick()
        else:self.send_code_value.setText(identity.get('code') or '连接中…')
    def _set_custom(self):
        try:self.service.set_custom_code(self.custom_input.text())
        except (ValueError,OSError) as error:self.custom_status.setText(str(error))

    def _cancel_custom(self):
        try:self.service.cancel_custom_code()
        except (ValueError,OSError) as error:self.custom_status.setText(str(error))

    def _custom_changed(self,value):
        status=value.get('status','empty');online=bool(value.get('online'))
        text={'available':'可用 · 10 分钟内有效','connecting':'正在建立会话','expired':'已过期','used':'已使用','cancelled':'已取消','empty':'可自定义一次性接收码'}.get(status,'')
        self.custom_value.setText(value.get('code') or '—')
        self.custom_value.setEnabled(online and status=='available')
        self.custom_cancel.setEnabled(online and status in ('available','connecting'))
        self.custom_set.setEnabled(online)
        if status=='available':text=f'可用 · {max(0,int(value.get("remaining",0)))} 秒'
        self.custom_status.setText(str(value.get('error') or (text if online else '远程服务未连接')))

    def _target_changed(self,*_):
        if not self._wan:return
        nearby=getattr(self.service,'nearby_peer',lambda _:None)(self.devices.currentData()) if not self.peer_code_input.text().strip() else None
        self.nearby_hint.setVisible(bool(nearby))

    def _recommend_lan(self):
        peer=getattr(self.service,'nearby_peer',lambda _:None)(self.devices.currentData())
        if not peer:return
        paths=list(self._queued_paths)
        if not paths and not self._explicit_queue and self._paths_provider:paths=list(self._paths_provider() or [])
        self.lan_recommended.emit(paths,peer['id'])

    def _remove_recent(self,identifier):
        if not identifier:return
        try:self.service.remove_peer(identifier)
        except (ValueError,OSError) as error:self.status_label.setText(str(error))

    def _send(self):
        peer = self.peer_code_input.text().strip().replace(' ','').upper() if self._wan else ''
        peer = peer or self.devices.currentData()
        if not peer: return
        paths = list(self._queued_paths)
        if not paths and not self._explicit_queue: paths = list(self._paths_provider() or []) if self._paths_provider else []
        if not paths:
            paths, _ = QFileDialog.getOpenFileNames(self, '选择要发送的文件')
        if not paths: return
        try:
            identifier = self.service.send(peer, paths)
            if self._wan:self.peer_code_input.clear()
            self._set_history_context(identifier, paths)
            self.queue_paths([],replace=True)
        except (ValueError, OSError) as error: self.status_label.setText(str(error))

    def send_paths(self, peer_id, paths):
        """Overview/context-menu entry can send selected files and folders."""
        try:
            identifier = self.service.send(peer_id, paths)
            self._set_history_context(identifier, paths)
            return identifier
        except (ValueError, OSError) as error: self.status_label.setText(str(error)); return ''

    def _row(self, identifier, peer='', direction='send'):
        if identifier in self._rows: return self._rows[identifier]
        self.transfer_started.emit()
        frame = QFrame(self.transfers_body); frame.setObjectName('lanTransferRow')
        outer = QVBoxLayout(frame); outer.setContentsMargins(14, 8, 14, 9); outer.setSpacing(5)
        heading_widget = QWidget(frame); heading = QHBoxLayout(heading_widget)
        heading.setContentsMargins(0,0,0,0)
        direction_badge=QLabel('发送' if direction=='send' else '接收',heading_widget)
        direction_badge.setObjectName('lanTransferDirection');direction_badge.setProperty('transferDirection',direction)
        direction_badge.setAlignment(Qt.AlignCenter);direction_badge.setFixedSize(44,24);heading.addWidget(direction_badge)
        title = _ElidedLabel(('发送至 ' if direction == 'send' else '接收自 ') + peer, heading_widget)
        title.setObjectName('lanTransferTitle')
        heading.addWidget(title, 1)
        route_badge=QLabel('连接中' if self._wan else '局域网直连',heading_widget);route_badge.setObjectName('lanTransferRoute')
        route_badge.setAlignment(Qt.AlignCenter);route_badge.setFixedHeight(24);heading.addWidget(route_badge)
        cancel = _CancelButton(frame); cancel.dark = self._dark
        cancel.clicked.connect(lambda: self.service.cancel(identifier)); heading.addWidget(cancel); outer.addWidget(heading_widget)
        bar = QProgressBar(frame); bar.setRange(0, 1000); bar.setTextVisible(False); outer.addWidget(bar)
        summary=QWidget(frame);summary_layout=QHBoxLayout(summary);summary_layout.setContentsMargins(0,0,0,0);summary_layout.setSpacing(12)
        detail = _ElidedLabel('', summary); detail.setObjectName('lanTransferDetail');summary_layout.addWidget(detail,1)
        metrics=_ElidedLabel('',summary);metrics.setObjectName('lanTransferMetrics');metrics.setAlignment(Qt.AlignRight|Qt.AlignVCenter)
        metrics.setMinimumWidth(128);summary_layout.addWidget(metrics,1);outer.addWidget(summary)
        self.transfers_layout.insertWidget(0,frame)
        self._row_sequence += 1
        row = {'frame': frame, 'heading': heading_widget, 'title': title, 'detail': detail, 'bar': bar, 'cancel': cancel,
               'summary':summary,'metrics':metrics,'route_badge':route_badge,'direction_badge':direction_badge,
               'extra': None, 'sequence': self._row_sequence}
        row.update(self._history_context.pop(identifier, {}))
        self._rows[identifier] = row
        # Keep completed history small without removing active/cancellable rows.
        if len(self._rows) > 12:
            for old_id, old in list(self._rows.items()):
                if old_id != identifier and old.get('finished'):
                    old['frame'].deleteLater(); del self._rows[old_id]; break
        self._row_page = 0
        self._refresh_transfer_pages()
        return row

    def _change_page(self, delta):
        self._row_page += delta
        self._refresh_transfer_pages()

    def _refresh_transfer_pages(self):
        # Terminal transfers belong in history; they never reserve active slots.
        for row in self._rows.values():
            if row.get('finished'):row['bar'].hide();row['frame'].hide()
        rows = sorted((row for row in self._rows.values() if not row.get('finished')), key=lambda row:-row['sequence'])
        for row in reversed(rows):
            self.transfers_layout.insertWidget(0,row['frame'])
        pages = max(1, (len(rows) + 1) // 2)
        self._row_page = min(max(0, self._row_page), pages - 1)
        first = self._row_page * 2
        visible = rows[first:first+2]
        for row in rows: row['frame'].setVisible(row in visible)
        self.transfers_empty.setVisible(not rows)
        count=len(rows);self.transfer_count.setText(str(count));self.transfer_count.setAccessibleName(f'{count} 项进行中')
        self.previous_page.setVisible(pages > 1)
        self.next_page.setVisible(pages > 1)
        self.page_label.setVisible(pages > 1)
        self.page_label.setText(f'{self._row_page+1} / {pages}')
        self.previous_page.setEnabled(self._row_page > 0)
        self.next_page.setEnabled(self._row_page < pages - 1)

    def _offer(self, offer):
        self.show_offer(offer)

    def show_offer(self, offer):
        """Replay an offer emitted before this optional panel was mounted."""
        identifier = offer['id']; self._offers[identifier] = offer
        if identifier in self._rows:return
        row = self._row(identifier, offer['peer'], 'receive')
        row['history_name'] = str((offer.get('names') or [''])[0])
        row['history_count'] = int(offer['count'])
        if offer.get('automatic'):
            row['detail'].setText('同账号已授权，正在建立连接…')
            return
        names = '、'.join(str(name) for name in offer.get('names', [])[:3])
        if len(offer.get('names', [])) > 3: names += ' 等'
        confirmation=str(offer.get('device_code') or offer.get('code') or '').strip()
        code_label='设备码' if self._wan else '确认码'
        row['detail'].setText(f"{names} · {_size(offer['total'])}"+(f" · {code_label} {confirmation}" if confirmation else ''))
        row['heading'].hide(); row['detail'].hide(); row['bar'].hide();row['summary'].hide()

        card = QFrame(row['frame']); card.setObjectName('lanOfferCard')
        row['frame'].setProperty('offerFrame',True)
        row['frame'].layout().setContentsMargins(0,0,0,0)
        contents = QVBoxLayout(card); contents.setContentsMargins(16, 12, 16, 12); contents.setSpacing(6)
        header = QHBoxLayout(); header.setSpacing(12)
        identity = QVBoxLayout(); identity.setSpacing(4)
        kicker = _label('收到传输请求', card); kicker.setObjectName('lanOfferKicker')
        kicker.setMinimumHeight(18)
        kicker.setSizePolicy(QSizePolicy.Policy.Fixed,QSizePolicy.Policy.Preferred)
        peer_label = _ElidedLabel(str(offer['peer']), card); peer_label.setObjectName('lanOfferPeer')
        identity.addWidget(kicker); identity.addWidget(peer_label)
        header.addLayout(identity, 1)
        code = QFrame(card); code.setObjectName('lanOfferCode')
        code_layout = QVBoxLayout(code); code_layout.setContentsMargins(0,0,0,0); code_layout.setSpacing(2)
        caption = _label(code_label, code); caption.setObjectName('lanOfferCodeCaption')
        caption.setMinimumHeight(14)
        caption.setAlignment(Qt.AlignmentFlag.AlignRight)
        code_value = _CopyCodeButton(confirmation or '暂不可用', code); code_value.setObjectName('lanOfferCodeValue')
        caption.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        code_value.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        code_value.setAccessibleName(('点击复制发送方设备码 ' if self._wan else '与发送方核对六位数字 ') + confirmation)
        code_layout.addWidget(caption); code_layout.addWidget(code_value)
        code.setVisible(self._wan or bool(confirmation))
        header.addWidget(code)
        contents.addLayout(header)
        files=QHBoxLayout();files.setSpacing(8)
        file_label = _ElidedLabel(names or '文件', card); file_label.setObjectName('lanOfferFile')
        files.addWidget(file_label,1)
        meta = _label(f"{offer['count']} 项 · {_size(offer['total'])}", card)
        meta.setObjectName('lanOfferMeta'); meta.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        meta.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        files.addWidget(meta);contents.addLayout(files)
        destination = _ElidedLabel('保存到 ' + self.receive_path.text(), card)
        destination.setObjectName('lanOfferMeta')

        footer = QHBoxLayout(); footer.setSpacing(7)
        footer.addWidget(destination,1)
        refuse = _button('拒绝', card); refuse.setObjectName('lanOfferReject')
        accept = _button('接收文件', card); accept.setObjectName('lanOfferAccept')
        refuse.setProperty('pressScale',True);accept.setProperty('pressScale',True)
        footer.addWidget(refuse); footer.addWidget(accept)
        contents.addLayout(footer)
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(16); shadow.setOffset(0, 3)
        shadow.setColor(QColor(0, 0, 0, 72 if self._dark else 28))
        card.setGraphicsEffect(shadow)
        refuse.clicked.connect(lambda: self._reject(identifier)); accept.clicked.connect(lambda: self._accept(identifier))
        row['frame'].layout().addWidget(card)
        row.update(extra=card, offer_shadow=shadow, offer_destination=destination)
        self.status_label.clear()

    @staticmethod
    def _restore_transfer_surface(row):
        frame=row['frame'];frame.setProperty('offerFrame',False)
        row['summary'].show()
        frame.layout().setContentsMargins(14,8,14,9)
        frame.style().unpolish(frame);frame.style().polish(frame)

    def _accept(self, identifier):
        folder = self.receive_path.text()
        if not Path(folder).is_dir():
            self.status_label.setText('接收位置不可用，请先选择一个有效文件夹。'); return
        try:
            if self.service.accept(identifier, folder):
                row = self._rows[identifier]; row['extra'].hide(); row['heading'].show(); row['detail'].show(); row['bar'].show()
                self._restore_transfer_surface(row)
                row['detail'].setText('已确认接收，正在建立直连…')
        except (ValueError, OSError) as error: self.status_label.setText(str(error))

    def _reject(self, identifier):
        self.service.reject(identifier)
        row = self._rows[identifier]
        if row.get('extra'): row['extra'].hide()
        self._restore_transfer_surface(row)
        row['heading'].show(); row['detail'].show(); row['cancel'].hide()
        row['detail'].setText('已拒绝接收'); row['finished'] = True
        self._refresh_transfer_pages()

    def _progress(self, data):
        identifier = data['id']; row = self._row(identifier, data.get('peer', ''), data['direction'])
        state = data['state']; now = time.monotonic(); received = data.get('received', 0); total = data.get('total', 0)
        row['metrics'].setVisible(state=='transferring')
        if state!='transferring':row['metrics'].clear()
        if data.get('route'):row['route']=data['route']
        if 'relay_bytes' in data:row['relay_bytes']=max(row.get('relay_bytes',0),int(data['relay_bytes']))
        usage=row.get('relay_bytes',0);route=row.get('route','')
        route_text=('P2P 直连 · '+_turn_usage(usage) if route=='direct' and usage else _turn_usage(usage)) if route=='relay' or usage else 'P2P 直连' if route=='direct' else '局域网直连' if not self._wan else '连接中'
        row['route_badge'].setText(route_text)
        relay=bool(route=='relay' or usage)
        if row['route_badge'].property('relayRoute')!=relay:
            row['route_badge'].setProperty('relayRoute',relay)
            row['route_badge'].style().unpolish(row['route_badge']);row['route_badge'].style().polish(row['route_badge'])
        if row.get('finished') and state in ('preparing','waiting','transferring','reconnecting'):
            if row.get('terminal_state')!='interrupted':return
            previous=row.pop('history_record',None)
            if previous in self._history:self._history.remove(previous);self._render_history()
            row.update(finished=False,recorded=False);row['bar'].show();row['cancel'].show();self._refresh_transfer_pages()
        if state == 'transferring':
            sample=self._samples.get(identifier)
            if sample is None or received<sample['points'][-1][1]:
                sample=dict(points=deque(),speed=0,shown_at=now)
                self._samples[identifier]=sample
            points=sample['points'];points.append((now,received))
            while len(points)>2 and points[1][0]<now-2:points.popleft()
            elapsed=now-points[0][0]
            if elapsed>=.35 and now-sample['shown_at']>=.5:
                sample['speed']=max(0,received-points[0][1])/elapsed;sample['shown_at']=now
            speed=sample['speed']
            fraction = min(1000, int(received * 1000 / total)) if total else 0
            row['bar'].setRange(0, 1000); row['bar'].setValue(fraction)
            name = str(data.get('name') or '')
            if name and not row.get('history_name'):
                row['history_name'] = Path(name).name
            if name:
                row['title'].setText(Path(name).name)
                row['title'].setToolTip(('发送至 ' if data['direction']=='send' else '接收自 ')+str(data.get('peer') or ''))
            eta=_remaining((total-received)/speed) if speed and total>received else '正在完成' if total and received>=total else '计算剩余时间…'
            transport=' · '+_turn_usage(row.get('relay_bytes')) if row.get('route')=='relay' or row.get('relay_bytes',0)>0 else ' · P2P' if row.get('route')=='direct' else ''
            row['detail'].setText(f'{_size(received)} / {_size(total)} · 对端：'+str(data.get('peer') or '未知设备'))
            row['metrics'].setText((f'{_size(speed)}/s' if speed else '计算速度…')+' · '+eta)
            if transport:row['detail'].setToolTip('TURN 为本次中继连接数据量；不含网络协议开销，计费用量以 Cloudflare 为准。')
        elif state == 'waiting':
            if not self._wan:self.send_code_value.setText(' '.join(str(data.get('code',''))) or '—')
            row['bar'].setRange(0, 0)
            row['detail'].setText('等待接收方确认' if self._wan else f"等待接收方确认 · 确认码 {data.get('code', '')}")
        elif state=='connecting':
            row['bar'].setRange(0,0);row['detail'].setText('正在尝试中继连接…' if data.get('route')=='relay' else '正在建立 P2P 直连…')
        elif state in ('preparing', 'reconnecting'):
            row['bar'].setRange(0, 0); row['detail'].setText('正在准备传输…' if state == 'preparing' else '连接中断，正在续传…')
        elif state in ('cancelled', 'error', 'complete','interrupted'):
            self._samples.pop(identifier,None)
            row['finished'] = True;row['terminal_state']=state; row['cancel'].hide()
            if row.get('extra'):
                row['extra'].hide(); row['heading'].show(); row['detail'].show()
                self._restore_transfer_surface(row)
            row['bar'].setRange(0, 1000); row['bar'].setValue(1000 if state == 'complete' else 0);row['bar'].hide()
            row['detail'].setText('传输完成 · 原文件已校验' if state == 'complete' else '连接中断，可由发送方续传' if state=='interrupted' else ('已取消传输' if state == 'cancelled' else data.get('message', '传输失败')))
            if not row.get('recorded'):
                row['recorded'] = True
                record={'when':time.strftime('%m-%d %H:%M'),
                    'name':str(row.get('history_name') or data.get('name') or row['title'].text())[:100],
                    'peer':str(data.get('peer') or '')[:80],
                    'count':row.get('history_count'),'paths':row.get('history_paths',[]),
                    'state':{'complete':'完成','cancelled':'已取消','error':'失败','interrupted':'已中断'}[state],
                    'direction':str(data.get('direction') or 'send')}
                if row.get('route'):record.update(route=row['route'],relay_bytes=row.get('relay_bytes',0))
                if state in ('error','interrupted'):record['error']=str(data.get('message') or '')[:600]
                row['history_record']=record;self._history.insert(0,record)
                self._history = self._history[:12]; self._render_history()
                try:self._save_history()
                except OSError:pass
            self._refresh_transfer_pages()
        if state=='error':self.status_label.setText(str(data.get('message') or '传输失败，请重试。')[:600])
        elif state not in ('interrupted',):self.status_label.clear()

    def _completed(self, result):
        row=self._rows.get(result.get('id'));paths=result.get('paths')
        if row and isinstance(paths,list) and paths and isinstance(paths[0],str):
            row['history_paths']=[paths[0]]
            if row.get('history_record') is not None:
                row['history_record']['paths']=[paths[0]];self._render_history()
                try:self._save_history()
                except OSError:pass
        if result.get('direction') == 'receive': self.received_paths.emit(list(result.get('paths') or []))


class _DialogTitleBar(QWidget):
    def __init__(self, dialog):
        super().__init__(dialog); self.dialog = dialog; self._grab = None
        self.setObjectName('lanTitleBar')
        self.setFixedHeight(48); self.setCursor(Qt.CursorShape.OpenHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._grab = event.globalPosition().toPoint() - self.dialog.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor); event.accept(); return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._grab is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.dialog.move(event.globalPosition().toPoint() - self._grab)
            event.accept(); return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._grab = None; self.setCursor(Qt.CursorShape.OpenHandCursor)
        super().mouseReleaseEvent(event)


class _TransferDropRouter(QObject):
    """Queue a local file drop anywhere in the transfer window, exactly once."""
    def __init__(self, dialog):
        super().__init__(dialog);self.dialog=dialog
        for widget in [dialog,*dialog.findChildren(QWidget)]:widget.setAcceptDrops(True)
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, watched, event):
        kind=event.type()
        if kind not in (QEvent.Type.ShortcutOverride,QEvent.Type.KeyPress,QEvent.Type.DragEnter,
                        QEvent.Type.DragMove,QEvent.Type.DragLeave,QEvent.Type.Drop):return False
        from shiboken6 import isValid
        if not isValid(self.dialog):return False
        if not isinstance(watched,QWidget) or not (watched is self.dialog or self.dialog.isAncestorOf(watched)):return False
        if kind in (QEvent.Type.ShortcutOverride,QEvent.Type.KeyPress):
            if (not self.dialog.isVisible() or QApplication.activeModalWidget() is not None
                    or not event.matches(QKeySequence.StandardKey.Paste)):return False
            mime=QApplication.clipboard().mimeData()
            if mime is None or not mime.hasUrls():return False
            urls=mime.urls()
            if not urls or any(not url.isLocalFile() for url in urls):return False
            paths=[url.toLocalFile() for url in urls if url.toLocalFile()]
            if not paths:return False
            # Path validation/metadata stays in the existing asynchronous queue;
            # reading a network-mounted path here would stall keyboard input.
            if kind==QEvent.Type.KeyPress and not event.isAutoRepeat():
                self.dialog.panel.queue_paths(paths)
            event.accept();return True
        if kind not in (QEvent.Type.DragEnter,QEvent.Type.DragMove,QEvent.Type.DragLeave,QEvent.Type.Drop):return False
        zone=self.dialog.panel.drop_zone
        if kind==QEvent.Type.DragLeave:
            zone.dragLeaveEvent(event);return True
        paths=zone._paths(event)
        if not paths:
            zone.set_drag_active(False);event.ignore();return True
        if kind==QEvent.Type.Drop:
            zone.dragLeaveEvent(event);self.dialog.panel.queue_paths(paths)
            point=watched.mapTo(self.dialog,event.position().toPoint())
            self.dialog.drop_feedback.play(paths,point,point)
        else:
            zone.set_drag_active(True)
        event.setDropAction(Qt.DropAction.CopyAction);event.accept();return True


class LanDialog(QDialog):
    """A dedicated layered transfer window opened from the overview."""
    overview_requested = Signal()
    mode_selected = Signal(str)
    lan_recommended = Signal(list,str)
    mini_requested = Signal()
    dock_requested = Signal()
    closing_intent = Signal()

    def present_dropped_files(self):
        # The native drag loop may reorder its source after Drop returns.
        # Raise once after that return, without activating another application.
        from platform_native import raise_shelf_for_reveal
        if not hasattr(self,'_drop_order_timer'):
            self._drop_order_timer=QTimer(self);self._drop_order_timer.setSingleShot(True)
            self._drop_order_timer.timeout.connect(self._release_drop_order)
        def reveal():
            if not self.isVisible():return
            self.raise_()
            self._drop_order_raised=raise_shelf_for_reveal(self) or getattr(self,'_drop_order_raised',False)
            self._drop_order_timer.start(400)
        reveal();QTimer.singleShot(0,self,reveal)

    def _release_drop_order(self):
        if getattr(self,'_drop_order_raised',False):
            from platform_native import lower_shelf_after_reveal
            if lower_shelf_after_reveal(self):self._drop_order_raised=False

    def hideEvent(self,event):
        self.drop_feedback.cancel()
        if hasattr(self,'_drop_order_timer'):self._drop_order_timer.stop()
        self._release_drop_order()
        super().hideEvent(event)

    def show(self):
        self._navigation_generation+=1;self._navigate_after_close=None
        self.chrome.prepare_open();self._window_fade.reopen();super().show()

    def done(self,result):
        self.closing_intent.emit()
        if self._window_fade.defer_done(result,self.done):return
        super().done(result)

    def reject(self):
        super().reject()

    def __init__(self, service, settings, parent=None):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self._settings=settings;self._panels={};self._theme=settings.theme
        self._navigate_after_close=None;self._navigation_generation=0
        self.finished.connect(self._discard_closed_drafts)
        self.finished.connect(self._finish_navigation)
        self._wan=getattr(service,'mode','lan')=='wan'
        self.setObjectName('lanDialog'); self.setWindowTitle('FlickDock · 文件互传')
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(False); self.resize(980, 792); self.setMinimumSize(824, 660)
        root = QVBoxLayout(self); root.setContentsMargins(22, 22, 22, 22)
        self.card = QFrame(self); self.card.setObjectName('lanDialogCard')
        from collections import OrderedDict
        self._shadow_cache=OrderedDict()
        root.addWidget(self.card)
        content = QVBoxLayout(self.card); content.setContentsMargins(0, 0, 0, 0); content.setSpacing(0)
        self.title_bar = _DialogTitleBar(self)
        title_row = QHBoxLayout(self.title_bar); title_row.setContentsMargins(20,0,20,0);title_row.setSpacing(12)
        self.traffic_buttons=[]
        if sys.platform=='darwin':
            from native_ui_common import toggle_window_zoom
            controls=QHBoxLayout();controls.setSpacing(8)
            from window_chrome import minimize_utility
            for value,callback in (('#FF5F56',self.close),('#FFBD2E',lambda:minimize_utility(self)),('#27C93F',lambda:toggle_window_zoom(self))):
                button=_button('',self.title_bar);button.setFixedSize(12,12)
                button.setStyleSheet(f'QPushButton{{background:{value};border:0;border-radius:6px;padding:0;min-height:0;}}')
                if callback:button.clicked.connect(callback)
                else:button.setEnabled(False)
                controls.addWidget(button);self.traffic_buttons.append(button)
            title_row.addLayout(controls)
        self.title_icon=QLabel(self.title_bar);self.title_icon.setFixedSize(16,16)
        self.title_icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        title_row.addWidget(self.title_icon)
        title = _label('文件互传', self.title_bar); title.setObjectName('lanDialogTitle')
        self.title_label=title
        title.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Preferred);title.setMinimumWidth(72)
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        title_row.addWidget(title)
        self.mode_bar=QFrame(self.card);self.mode_bar.setObjectName('transferModeBar')
        self.mode_bar.setFixedHeight(50)
        mode_row=QVBoxLayout(self.mode_bar);mode_row.setContentsMargins(20,8,20,8);mode_row.setSpacing(4)
        self.mode_segment=QFrame(self.mode_bar);self.mode_segment.setObjectName('transferModeSegmentGroup')
        self.mode_segment.setProperty('brandSegment',True)
        self.mode_segment.setFixedHeight(34)
        self.mode_segment.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed)
        self.mode_segment.setAccessibleName('传输方式')
        mode_layout=QHBoxLayout(self.mode_segment);mode_layout.setContentsMargins(2,2,2,2);mode_layout.setSpacing(0)
        self.mode_group=QButtonGroup(self);self.mode_group.setExclusive(True);self.mode_buttons={}
        for mode,label in (('lan','局域网传输'),('wan','远程传输')):
            control=_button(label,self.mode_segment);control.setCheckable(True);control.setChecked((mode=='wan')==self._wan)
            control.setFixedHeight(28)
            control.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed)
            control.setObjectName('transferModeSegment');self.mode_group.addButton(control);self.mode_buttons[mode]=control
            control.clicked.connect(lambda checked=False,mode=mode:self.mode_selected.emit(mode))
            mode_layout.addWidget(control,1)
        from ui_transitions import SegmentIndicator
        self.mode_indicator=SegmentIndicator(self.mode_segment,self.mode_buttons.values())
        self.mode_indicator.set_theme(self._theme)
        mode_row.addWidget(self.mode_segment);self.mode_row=mode_row
        self.back_button=_button('返回总览',self.title_bar);self.back_button.setObjectName('lanDialogBack')
        self.back_button.setFixedHeight(28)
        self.back_button.clicked.connect(lambda:self._navigate('overview'));title_row.addWidget(self.back_button)
        self.status_title_layout=title_row
        close = _CloseButton('', self.title_bar); close.setObjectName('lanDialogClose')
        self.close_button=close
        close.setAutoDefault(False);close.setDefault(False);close.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        close.setFixedSize(32, 32); close.setAccessibleName('关闭局域网传输窗口')
        close.clicked.connect(self.close); title_row.addWidget(close)
        content.addWidget(self.title_bar)
        content.addWidget(self.mode_bar)
        self.panel_stack=QStackedWidget(self.card)
        from file_metadata import FileMetadataCache
        self.file_metadata=FileMetadataCache(self)
        self.panel = LanPanel(service, self.panel_stack, theme=settings.theme, settings=settings,metadata_cache=self.file_metadata)
        self.panel.lan_recommended.connect(self.lan_recommended)
        self._panels[service]=self.panel;self.panel_stack.addWidget(self.panel)
        self._mount_mode_status()
        if self._wan:self.close_button.setAccessibleName('关闭远程传输窗口')
        content.addWidget(self.panel_stack, 1)
        self.apply_theme(settings.theme)
        self.drop_router=_TransferDropRouter(self)
        from window_chrome import WindowChrome
        self.chrome=WindowChrome(self,self.title_bar,settings,'transfer',surface=self.card)
        from transfer_drop_feedback import DropFeedback
        self.drop_feedback=DropFeedback(self)

    def _mount_mode_status(self):
        for panel in self._panels.values():
            label=panel.status_label;self.status_title_layout.removeWidget(label)
            if panel is not self.panel:
                label.setParent(panel);label.hide()
        label=self.panel.status_label;self.panel.layout().removeWidget(label)
        label.setParent(self.title_bar)
        policy=QSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed);policy.setRetainSizeWhenHidden(True);label.setSizePolicy(policy)
        label.setMinimumWidth(0);label.setFixedHeight(20);label.setAlignment(Qt.AlignLeft|Qt.AlignVCenter)
        self.status_title_layout.insertWidget(self.status_title_layout.indexOf(self.back_button)+1,label,1)
        label.set_theme(self._theme);label.show()

    def _navigate(self, target):
        self._navigate_after_close=target
        self.close()

    def _finish_navigation(self,*_):
        target=self._navigate_after_close;self._navigate_after_close=None
        if target:
            generation=self._navigation_generation
            def navigate():
                from shiboken6 import isValid
                if isValid(self) and not self.isVisible() and generation==self._navigation_generation:
                    getattr(self,target+'_requested').emit()
            # WindowChrome's finished handler may still change macOS Dock
            # policy. Complete all close handlers before activating the shelf.
            QTimer.singleShot(0,navigate)

    def _active_transfers(self):
        return [(service,identifier) for service in self._panels
                for identifier,transfer in tuple(getattr(service,'_transfers',{}).items())
                if getattr(transfer,'active',False)]

    def closeEvent(self,event):
        # Closing a view does not own the lifetime of active transport jobs.
        # The explicit per-file cancel controls still cancel those jobs.
        self.closing_intent.emit()
        super().closeEvent(event)

    def _discard_closed_drafts(self,*_):
        for panel in self._panels.values():panel.queue_paths([],replace=True)

    def set_service(self,service):
        """Swap only the content, keeping one native surface and shadow alive."""
        if self.panel.service is service:return self.panel
        queued=list(self.panel._queued_paths);explicit_queue=self.panel._explicit_queue
        try:
            if service not in self._panels:
                panel=LanPanel(service,self.panel_stack,theme=self._theme,settings=self._settings,metadata_cache=self.file_metadata)
                panel.lan_recommended.connect(self.lan_recommended)
                self._panels[service]=panel;self.panel_stack.addWidget(panel)
                for widget in (panel,*panel.findChildren(QWidget)):widget.setAcceptDrops(True)
            self.panel=self._panels[service];self._wan=getattr(service,'mode','lan')=='wan'
            self._mount_mode_status()
            self.panel.queue_paths(queued,replace=True)
            self.panel._explicit_queue=explicit_queue
            self.mode_buttons['wan' if self._wan else 'lan'].setChecked(True)
            if not hasattr(self,'_page_fade'):
                from ui_transitions import SnapshotFade
                self._page_fade=SnapshotFade(self.panel_stack)
            self._page_fade.change(lambda:self.panel_stack.setCurrentWidget(self.panel))
            title='远程传输' if self._wan else '局域网互传'
            self.title_label.setText('文件互传');self.setWindowTitle('FlickDock · 文件互传')
            self.close_button.setAccessibleName('关闭'+title+'窗口')
            self._refresh_title_icon()
            self.mode_indicator.set_theme(self._theme)
            if self._wan and callable(getattr(service,'check_code',None)):
                service.check_code();self.panel._code_identity_changed(service.code_identity())
        finally:
            self.card.update();self.update()
        return self.panel

    def paintEvent(self,event):
        from surface_morph import paint_surface_shadow
        from PySide6.QtCore import QPointF,QRectF
        p=QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(event.rect(),Qt.transparent)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        paint_surface_shadow(p,QRectF(self.card.geometry()),self._shadow_cache,
            blur=18,offset=QPointF(0,4),
            color=QColor(0,0,0,78) if self._theme=='dark' else QColor(15,23,42,32))
        p.end()

    def _refresh_title_icon(self):
        c=ui_colors(self._theme)
        self.title_icon.setPixmap(ui_icon('web' if self._wan else 'nearby',c['cyan'] if self._theme=='dark' else c['blue']).pixmap(16,16))

    def showEvent(self, event):
        super().showEvent(event)
        remove_host_window_shadow(self)
        if self._wan and callable(getattr(self.panel.service,'check_code',None)):
            self.panel.service.check_code();self.panel._code_identity_changed(self.panel.service.code_identity())

    def apply_theme(self, theme):
        if getattr(self,'_applied_dialog_theme',None)==theme:return
        self._applied_dialog_theme=theme;self._theme=theme
        dark = theme == 'dark'
        self.setProperty('uiTheme', theme)
        c=ui_colors(theme)
        font_family=reference_font().family()
        surface,ink,hover=c['surface'],c['text'],c['hover']
        self._refresh_title_icon()
        self.mode_indicator.set_theme(theme)
        self.back_button.setIcon(QIcon(ui_icon('right',c['secondary']).pixmap(16,16).transformed(QTransform().rotate(180))))
        self.back_button.setIconSize(QSize(16,16))
        self.card.setStyleSheet(f'''
            QFrame#lanDialogCard {{ background:{surface}; border:1px solid {c['glass_border']}; border-radius:{c['radius']}px; }}
            QWidget#lanTitleBar {{ background:transparent; border:0; border-bottom:1px solid {c['divider']}; }}
            QFrame#transferModeBar {{background:transparent;border:0;border-bottom:1px solid {c['divider']};}}
            QLabel#lanDialogTitle {{ color:{ink}; font:700 15px '{font_family}'; background:transparent; border:0; }}
            QPushButton#lanDialogClose,QPushButton#lanDialogBack {{ background:transparent; color:{c['secondary']}; border:0; border-radius:8px;
                                          font:12px '{font_family}'; padding:0; }}
            QPushButton#lanDialogBack {{background:{c['control']};border:1px solid {c['border']};padding:0 10px;min-height:0;}}
            QFrame#transferModeSegmentGroup {{background:{'rgba(255,255,255,10)' if dark else 'rgba(0,0,0,10)'};border:1px solid {'rgba(255,255,255,18)' if dark else 'rgba(0,0,0,18)'};border-radius:9px;}}
            QPushButton#transferModeSegment {{background:transparent;color:{c['secondary']};border:0;border-radius:5px;padding:0 10px;font:11px '{font_family}';}}
            QPushButton#transferModeSegment:!checked:hover {{background:{'rgba(255,255,255,8)' if dark else 'rgba(0,0,0,5)'};color:{ink};}}
            QPushButton#transferModeSegment:checked {{background:transparent;color:{c['link']};font-weight:600;}}
            QPushButton#lanDialogClose:hover {{ background:{c['red']}; color:white; }}
            QPushButton#lanDialogBack:hover {{ background:{hover}; color:{ink}; }}
        ''')
        self.update()
        for panel in self._panels.values():panel.apply_theme(theme)
        from native_ui_common import readable_qss
        self.card.setStyleSheet(readable_qss(self.card.styleSheet()))
