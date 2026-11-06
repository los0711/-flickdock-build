"""Small embeddable LAN transfer panel, with explicit incoming consent."""
from __future__ import annotations

from pathlib import Path
import json
import math
import os
import sys
import time
from collections import deque

from PySide6.QtCore import QByteArray, QEvent, QObject, QPoint, QRect, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPainterPath, QPalette,QPen
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QFileDialog, QFrame, QGraphicsDropShadowEffect,
                               QHBoxLayout, QLayout, QLabel, QLineEdit, QProgressBar, QPushButton,
                               QListView, QMenu, QScrollArea, QSizePolicy, QStyle, QStyledItemDelegate,
                               QStyleOptionButton, QStyleOptionComboBox, QStylePainter, QToolTip, QVBoxLayout, QWidget, QStackedWidget, QButtonGroup)

from flickdock_icons import draw_glyph
from platform_native import remove_host_window_shadow
from native_ui_common import colors as ui_colors, icon as ui_icon, reference_font, reduced_motion
from async_images import ImageCache
from transfer_workbench import FileArtwork, reveal_file, TRANSFER_CORNER_RADIUS_PX

WORKBENCH_GAP_PX = 12
WORKBENCH_MARGIN_PX = 16
CARD_MARGIN_PX = 12
QUEUE_ROW_HEIGHT_PX = 44
QUEUE_MAX_HEIGHT_PX = 144
QUEUE_EMPTY_HEIGHT_PX = 164
DROP_ZONE_COMPACT_HEIGHT_PX = 34
SAME_ACCOUNT_BADGE_FONT_PX = 10
SAME_ACCOUNT_BADGE_PADDING_PX = 5
SAME_ACCOUNT_BADGE_GAP_PX = 6
DEVICE_ACTION_SHIFT_PX = 8


def _paint_account_badge(painter,x,center_y,dark):
    painter.save();font=painter.font();font.setPixelSize(SAME_ACCOUNT_BADGE_FONT_PX);painter.setFont(font)
    metrics=painter.fontMetrics();height=metrics.height()+4
    rect=QRectF(x,center_y-height/2,metrics.horizontalAdvance('同账号')+2*SAME_ACCOUNT_BADGE_PADDING_PX,height)
    painter.setPen(QPen(QColor(ui_colors('dark' if dark else 'light')['link']),1))
    painter.setBrush(Qt.NoBrush);painter.drawRoundedRect(rect.adjusted(.5,.5,-.5,-.5),4,4)
    painter.drawText(rect,Qt.AlignCenter,'同账号');painter.restore()
    return rect.width()+SAME_ACCOUNT_BADGE_GAP_PX
CODE_REFRESH_POLL_MS = 250
CODE_PROGRESS_MAX = 1000


def _label(value='', parent=None, *, muted=False):
    label = QLabel(value, parent); label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True); label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    if muted: label.setObjectName('lanMuted')
    return label


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
        from wan_transfer import RECEIVE_CODE
        if not (len(code)==6 and code.isascii() and code.isdigit() or RECEIVE_CODE.fullmatch(code)):return
        self.setText(current)
        QApplication.clipboard().setText(code)
        self.setToolTip('已复制');self._feedback.start(1600)
        QToolTip.showText(self.mapToGlobal(QPoint(0,self.height()+4)),'已复制到剪贴板',self,self.rect(),1600)
        self.setAccessibleDescription('已复制到剪贴板')


class _StatusLabel(QLabel):
    """Inline errors occupy space only while there is something to report."""
    def __init__(self,text='',parent=None):
        super().__init__(text,parent);self.setTextFormat(Qt.TextFormat.PlainText)

    def setText(self, value):
        super().setText(str(value))
        self.setVisible(bool(value))

    def clear(self):
        self.setText('')


class _CodeExpiryBar(QProgressBar):
    """The existing server refresh deadline, without a second countdown value."""
    def __init__(self,service,button,parent=None):
        super().__init__(parent);self.service=service;self.button=button;self._refresh_requested=False
        self.setRange(0,CODE_PROGRESS_MAX);self.setTextVisible(False);self.setFixedHeight(3)
        self.setAccessibleName('接收码刷新进度');self.setAccessibleDescription('进度条走完后自动刷新接收码')
        self._timer=QTimer(self);self._timer.setInterval(CODE_REFRESH_POLL_MS);self._timer.timeout.connect(self._tick)
    def _tick(self):
        identity=self.service.code_identity()
        self.button.setText(identity.get('code') or ('重试连接' if identity.get('error') else '刷新中…' if identity.get('online') else '连接中…'))
        self.button.setEnabled(bool(identity.get('code') or identity.get('error')))
        remaining=max(0.,identity.get('refresh_remaining',0));duration=max(1.,identity.get('refresh_duration',240))
        self.setValue(round(CODE_PROGRESS_MAX*min(1.,remaining/duration)))
        if remaining>0:self._refresh_requested=False
        elif identity.get('code') and identity.get('online') and not self._refresh_requested:
            self._refresh_requested=True;self.service.check_code()
    def showEvent(self,event):
        super().showEvent(event);self._tick();self._timer.start()
    def hideEvent(self,event):
        self._timer.stop();super().hideEvent(event)


def _size(value):
    value = float(value)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024 or unit == 'TB': return f'{value:.1f} {unit}'
        value /= 1024


def _turn_usage(value):
    amount=max(0,int(value or 0))/1000000
    return f'TURN:{amount/1000:.2f}GB' if amount>=1000 else f'TURN:{amount:.1f}MB'


def _transport_label(record):
    route=record.get('route');title={'lan':'局域网','direct':'P2P 直连','p2p':'P2P 直连','relay':'TURN 中继','cloud':'云端'}.get(route,'')
    return ' · '+title if title else ''


def _transport_hint(data):
    labels={'lan':'局域网','p2p':'P2P 直连','relay':'TURN 中继','cloud':'云端'}
    lines=['自动检测并选择更快的可用线路']
    measurements=data.get('route_measurements',[])
    for route,title in labels.items():
        samples=[row for row in measurements if row.get('route')==route and row.get('available')]
        rates=[row['bytes_per_second'] for row in samples if isinstance(row.get('bytes_per_second'),(int,float))]
        value=f'{_size(max(rates))}/s' if rates else '待测速' if samples else '未连接'
        lines.append(title+'：'+value)
    return '\n'.join(lines)


def _remaining(value):
    if value<60:return f'剩余约 {max(1,math.ceil(value))} 秒'
    if value<3600:return f'剩余约 {math.ceil(value/60)} 分钟'
    return f'剩余约 {math.ceil(value/3600)} 小时'


def _motion_allowed():
    """Honor platform accessibility settings and low-performance sessions."""
    disabled = {'1', 'true', 'yes', 'on'}
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
        view=self.view();popup=view.window()
        from native_ui_common import rounded_popup
        dark=bool(getattr(self.itemDelegate(),'dark',False))
        background='#303034' if dark else '#F1F1F4'
        for surface in (popup,view,view.viewport()):
            palette=surface.palette()
            palette.setColor(QPalette.ColorRole.Window,QColor(background));palette.setColor(QPalette.ColorRole.Base,QColor(background))
            surface.setPalette(palette);surface.setAutoFillBackground(True)
        popup.setStyleSheet('QFrame,QAbstractItemView{background:'+background+';border:0;border-radius:'+str(TRANSFER_CORNER_RADIUS_PX)+'px;}')
        rounded_popup(popup,TRANSFER_CORNER_RADIUS_PX)
        super().showPopup()
        self.view().viewport().installEventFilter(self)

    def eventFilter(self,watched,event):
        if watched is self.view().viewport() and getattr(self,'removable',False):
            if event.type() in (QEvent.MouseButtonPress,QEvent.MouseButtonRelease,QEvent.MouseButtonDblClick):
                index=self.view().indexAt(event.position().toPoint())
                if index.isValid() and self.view().visualRect(index).adjusted(self.view().visualRect(index).width()-34-DEVICE_ACTION_SHIFT_PX,0,-4-DEVICE_ACTION_SHIFT_PX,0).contains(event.position().toPoint()):
                    if event.type()==QEvent.MouseButtonPress:self._remove_pressed=index.data(Qt.UserRole)
                    elif event.type()==QEvent.MouseButtonRelease:
                        identifier=index.data(Qt.UserRole)
                        if identifier and identifier==getattr(self,'_remove_pressed',None):self.remove_requested.emit(identifier)
                        self._remove_pressed=None
                    return True
        return super().eventFilter(watched,event)

    def wheelEvent(self, event): event.ignore()

    def paintEvent(self, event):
        option=QStyleOptionComboBox();self.initStyleOption(option);option.currentText=''
        painter=QStylePainter(self);painter.setRenderHint(QPainter.Antialiasing)
        painter.drawComplexControl(QStyle.CC_ComboBox,option)
        dark=bool(getattr(self.itemDelegate(),'dark',False));colors=ui_colors('dark' if dark else 'light')
        text_rect=QRectF(self.style().subControlRect(QStyle.CC_ComboBox,option,QStyle.SC_ComboBoxEditField,self))
        text_rect.setRight(min(text_rect.right(),self.width()-52));painter.setFont(self.font())
        if self.currentData(Qt.UserRole+3):text_rect.adjust(_paint_account_badge(painter,text_rect.left(),text_rect.center().y(),dark),0,0,0)
        painter.setPen(QColor(colors['text'] if self.isEnabled() else colors['muted']))
        painter.drawText(text_rect,Qt.AlignLeft|Qt.AlignVCenter,painter.fontMetrics().elidedText(self.currentText(),Qt.ElideRight,max(1,round(text_rect.width()))))
        color=colors['secondary']
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

    def paint(self, painter, option, index):
        painter.save(); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        background = '#344357' if self.dark else '#C7DCF7'
        if selected:
            painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(QColor(background))
            painter.drawRoundedRect(QRectF(option.rect.adjusted(5, 3, -5, -3)), TRANSFER_CORNER_RADIUS_PX, TRANSFER_CORNER_RADIUS_PX)
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
        width = max(20, option.rect.width()-(112 if removable else 82)-DEVICE_ACTION_SHIFT_PX)
        online=index.data(Qt.ItemDataRole.UserRole+2)
        if online is not None:
            painter.setPen(Qt.PenStyle.NoPen);painter.setBrush(QColor('#30D158' if online else '#8E8E93'))
            painter.drawEllipse(QRectF(option.rect.right()-(52 if removable else 22)-DEVICE_ACTION_SHIFT_PX,option.rect.center().y()-4,8,8))
        if removable:
            ui_icon('close',secondary).paint(painter,QRect(option.rect.right()-27-DEVICE_ACTION_SHIFT_PX,option.rect.center().y()-9,18,18))
        painter.setFont(option.font)
        left=option.rect.left()+49
        if index.data(Qt.UserRole+3):
            offset=_paint_account_badge(painter,left,option.rect.top()+18,self.dark);left+=offset;width=max(1,width-offset)
        painter.setPen(QColor(primary))
        painter.drawText(QRectF(left,option.rect.top()+8,width,20),
                         Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(name,Qt.TextElideMode.ElideRight,width))
        detail_color=('#61D795' if self.dark else '#187A43') if host=='在线' and online else ('#A1A1AA' if self.dark else '#68686D') if host=='离线' else secondary
        painter.setPen(QColor(detail_color))
        secondary_font = painter.font()
        secondary_font.setPointSizeF(max(8, secondary_font.pointSizeF() - 1))
        painter.setFont(secondary_font)
        painter.drawText(QRectF(option.rect.left()+49,option.rect.top()+29,width,18),
                         Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(host,Qt.TextElideMode.ElideRight,width))
        painter.restore()


class _DropIllustration(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent); self.setFixedSize(28, 28); self.dark = False

    def paintEvent(self, event):
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ui_icon('file', '#64D2FF' if self.dark else '#0A84FF').paint(painter, self.rect().adjusted(2,2,-2,-2))


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
        from native_ui_common import DropBorderMotion
        self.border_motion=DropBorderMotion(self)
        layout = QVBoxLayout(self); layout.setContentsMargins(16, 16, 16, 16); layout.setSpacing(10)
        layout.addStretch(1)
        illustration = _DropIllustration(self)
        self.illustration = illustration
        layout.addWidget(illustration, 0, Qt.AlignmentFlag.AlignHCenter)
        self.title = _label('拖入文件/文件夹，或点击添加', self); self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setObjectName('lanDropTitle'); layout.addWidget(self.title)
        self.hint = _label('', self, muted=True)
        self.hint.setObjectName('lanDropHint')
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter); layout.addWidget(self.hint)
        self.hint.hide()
        layout.addStretch(1)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def border_path(self):
        rect=QRectF(self.rect()).adjusted(1,1,-1,-1)
        path=QPainterPath();path.addRoundedRect(rect,min(TRANSFER_CORNER_RADIUS_PX,rect.width()/2,rect.height()/2),min(TRANSFER_CORNER_RADIUS_PX,rect.width()/2,rect.height()/2))
        return path

    def set_drag_active(self,active):
        self._active=bool(active)
        self.border_motion.set_active(self._active or self._hovered)
        self.illustration.update()

    def paintEvent(self,event):
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        dark=self.illustration.dark
        path=self.border_path()
        self.border_motion.paint(painter,path,dark=dark,idle_fill=QColor(255,255,255,7) if dark else QColor(0,0,0,5))

    def enterEvent(self,event):
        self._hovered=True;self.border_motion.set_active(True);super().enterEvent(event)

    def leaveEvent(self,event):
        self._hovered=False;self.border_motion.set_active(self._active);super().leaveEvent(event)

    def hideEvent(self,event):
        self._hovered=False;self._active=False;self.border_motion.set_active(False);super().hideEvent(event)

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
            self.update()
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
        super().__init__('取消',parent); self.setFixedSize(32, 22); self.dark = False
        self.setToolTip('取消传输'); self.setAccessibleName('取消传输')
        self.setAutoDefault(False); self.setDefault(False)

    def paintEvent(self, event):
        super().paintEvent(event)


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


class _HistoryIcon(QWidget):
    def __init__(self, direction, parent=None):
        super().__init__(parent)
        self.direction = direction
        self.dark = False
        self.setFixedSize(16, 20)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor('#0A84FF' if self.direction == 'send' else '#30D158'))
        font=self.font();font.setPixelSize(15);font.setWeight(QFont.Weight.Bold);painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, '↗' if self.direction == 'send' else '↙')


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
        painter.drawRoundedRect(QRectF(0, 0, 38, 38), TRANSFER_CORNER_RADIUS_PX, TRANSFER_CORNER_RADIUS_PX)
        draw_glyph(painter, 'receive', QRectF(9, 9, 20, 20),
                   '#90bdff' if self.dark else '#2373dd', 1.8)


class _HistoryRemoveButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.dark = False
        self.setObjectName('lanHistoryRemove')
        self.setFixedSize(28, 28)
        self.setIcon(ui_icon('trash'));self.setIconSize(QSize(15,15))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAutoDefault(False)
        self.setDefault(False)
        self.setToolTip('删除这条传输记录')
        self.setAccessibleName('删除这条传输记录')

    def paintEvent(self, event):
        super().paintEvent(event)


class _HistoryRow(QFrame):
    remove_requested = Signal()
    reveal_requested = Signal(str)

    def __init__(self, record, artwork_cache, parent=None):
        super().__init__(parent)
        self.setObjectName('lanHistoryRow')
        self.setFixedHeight(56)
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
        layout.setContentsMargins(10, 6, 8, 6)
        layout.setSpacing(8)
        self.icon = FileArtwork(artwork_cache, self)
        paths=record.get('paths',[])
        self.icon.set_source(paths[0] if paths else '',record.get('name',''))
        layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignVCenter)
        lines = QVBoxLayout()
        lines.setSpacing(1);lines.setAlignment(Qt.AlignVCenter)
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
        self.when = QLabel(record['when'], self)
        self.when.setTextFormat(Qt.TextFormat.PlainText)
        self.when.setObjectName('lanHistoryWhen')
        self.when.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        trailing.addWidget(self.state, 0, Qt.AlignmentFlag.AlignRight)
        # Keep the timestamp accessible while the second line combines peer/time.
        self.when.hide()
        transport=_transport_label(record)
        self.title.setText((error if error else peer + ' · ' + record['when'])+transport)
        layout.addLayout(trailing)
        self.reveal_button=_button('',self);self.reveal_button.setFixedSize(28,28);self.reveal_button.setIconSize(QSize(16,16))
        self.reveal_button.setIcon(ui_icon('folder'));self.reveal_button.setObjectName('lanRevealFile')
        self.reveal_button.setAccessibleName('在文件夹打开 '+record.get('name','文件'))
        self.reveal_button.setVisible(state=='完成')
        self.reveal_button.setEnabled(bool(paths))
        self.reveal_button.setToolTip('在文件夹打开' if paths else '这条记录没有保存文件位置')
        self.reveal_button.clicked.connect(lambda:self.reveal_requested.emit(paths[0]))
        layout.addWidget(self.reveal_button)
        self.remove_button = _HistoryRemoveButton(self)
        self.remove_button.clicked.connect(self.remove_requested.emit)
        layout.addWidget(self.remove_button, 0, Qt.AlignmentFlag.AlignVCenter)


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
        self._rows = {}; self._offers = {}; self._queued_paths = []; self._row_sequence = 0
        from file_metadata import FileMetadataCache
        self.file_metadata=metadata_cache or FileMetadataCache(self)
        self.file_metadata.ready.connect(self._queue_info_ready);self._queue_row_map={};self._queue_labels={}
        self.file_metadata.deferred.connect(self._queue_info_deferred)
        self.artwork_cache=ImageCache(self)
        self.destroyed.connect(self.artwork_cache.close)
        QApplication.instance().aboutToQuit.connect(self.artwork_cache.close)
        self._history_context = {}
        self._dark = False; self._samples = {}; self._started = False
        self._history_path = settings.base / ('wan_history.json' if self._wan else 'lan_history.json') if settings else None
        self._history = self._load_history()
        self.setObjectName('lanPanel'); self.setFont(QFont(reference_font().family(), 10))
        self.setMinimumHeight(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(self); layout.setContentsMargins(WORKBENCH_MARGIN_PX, 10, WORKBENCH_MARGIN_PX, 10); layout.setSpacing(WORKBENCH_GAP_PX)
        columns = QHBoxLayout(); columns.setContentsMargins(0,0,0,0); columns.setSpacing(WORKBENCH_GAP_PX)
        self.left_column=QWidget(self)
        self.left_column.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Expanding)
        left_outer=QVBoxLayout(self.left_column);left_outer.setContentsMargins(0,0,0,0);left_outer.setSpacing(8)
        self.left_content=QWidget(self.left_column)
        self.left_content.setObjectName('lanLeftContent');self.left_content.setAttribute(Qt.WA_StyledBackground,True)
        left=QVBoxLayout(self.left_content);left.setContentsMargins(0,0,0,0);left.setSpacing(WORKBENCH_GAP_PX)
        left.setSizeConstraint(QLayout.SetMinimumSize)
        columns.addWidget(self.left_column,1)
        self.queue_area=QFrame(self);self.queue_area.setObjectName('lanPayloadArea')
        payload=QVBoxLayout(self.queue_area);payload.setContentsMargins(CARD_MARGIN_PX,CARD_MARGIN_PX,CARD_MARGIN_PX,CARD_MARGIN_PX);payload.setSpacing(10)
        self.drop_zone=_DropZone(self.queue_area);self.drop_zone.setFixedHeight(44)
        self.drop_zone.illustration.hide()
        self.drop_zone.layout().setContentsMargins(10,8,10,8);self.drop_zone.layout().setSpacing(0)
        self.drop_zone.dropped.connect(self.queue_paths);self.drop_zone.clicked.connect(self._pick_files)
        self.drop_zone.setContextMenuPolicy(Qt.CustomContextMenu);self.drop_zone.customContextMenuRequested.connect(self._pick_menu)
        queue_column=QWidget(self.queue_area);queue_box=QVBoxLayout(queue_column)
        self.queue_column=queue_column
        queue_box.setContentsMargins(0,0,0,0);queue_box.setSpacing(6)
        send_heading=QHBoxLayout();send_heading.setContentsMargins(0,0,0,0)
        self.send_heading=send_heading
        self.send_heading_label=_label('待发送项目',queue_column);self.send_heading_label.setObjectName('lanSection')
        send_heading.addWidget(self.send_heading_label,1)
        self.add_files=_button('添加文件',queue_column);self.add_files.setIcon(ui_icon('plus'));self.add_files.clicked.connect(self._pick_files)
        send_heading.addWidget(self.add_files)
        self.clear_queue=_button('清空',queue_column);self.clear_queue.setObjectName('lanClearButton')
        self.clear_queue.setToolTip('清除待发送列表');self.clear_queue.clicked.connect(lambda:self.queue_paths([],replace=True))
        send_heading.addWidget(self.clear_queue);queue_box.addLayout(send_heading)
        self.queue_body=QFrame(queue_column);self.queue_body.setObjectName('lanQueueBody')
        self.queue_layout=QVBoxLayout(self.queue_body);self.queue_layout.setContentsMargins(0,0,0,0);self.queue_layout.setSpacing(6)
        self.queue_layout.setSizeConstraint(QLayout.SetMinAndMaxSize);self.queue_rows=[]
        self.queue_scroll=QScrollArea(queue_column);self.queue_scroll.setObjectName('lanQueueScroll')
        self.queue_scroll.setFrameShape(QFrame.NoFrame);self.queue_scroll.setWidgetResizable(True)
        self.queue_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);self.queue_scroll.setWidget(self.queue_body)
        self.queue_scroll.verticalScrollBar().valueChanged.connect(self._queue_visible_info)
        self.queue_scroll.setMaximumHeight(QUEUE_MAX_HEIGHT_PX);queue_box.addWidget(self.queue_scroll)
        payload.addWidget(queue_column);payload.addWidget(self.drop_zone);left.addWidget(self.queue_area)
        self.send_column=QFrame(self);self.send_column.setObjectName('lanSendColumn')
        send=QVBoxLayout(self.send_column);send.setContentsMargins(CARD_MARGIN_PX,CARD_MARGIN_PX,CARD_MARGIN_PX,CARD_MARGIN_PX);send.setSpacing(8)
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
        self.devices = _DevicesCombo(self); self.devices.setMinimumHeight(32); self.devices.setMinimumWidth(0)
        self.devices.setObjectName('lanDevicePicker')
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
        self.nearby_hint=QFrame(self.send_column);self.nearby_hint.setObjectName('lanRouteHint');nearby_row=QHBoxLayout(self.nearby_hint)
        nearby_row.setContentsMargins(10,6,10,6);nearby_row.setSpacing(8)
        nearby_text=_label('已在局域网发现此设备，推荐使用局域网传输',self.nearby_hint,muted=True)
        nearby_text.setWordWrap(True);nearby_row.addWidget(nearby_text,1)
        self.use_lan_button=_button('使用局域网',self.nearby_hint);nearby_row.addWidget(self.use_lan_button)
        self.use_lan_button.clicked.connect(self._recommend_lan);self.nearby_hint.hide()
        if self._wan:send.addWidget(self.nearby_hint)
        self.peer_code_label=_label('对方接收码',self.send_column)
        self.peer_code_label.setObjectName('lanMuted');self.peer_code_label.setVisible(self._wan)
        self.peer_code_input=QLineEdit(self.send_column)
        self.peer_code_input.setPlaceholderText('4 位字母和数字')
        self.peer_code_input.setToolTip('输入对方当前的 4 位接收码，由字母和数字组成')
        self.peer_code_input.setAccessibleName('对方的接收码');self.peer_code_input.setMaxLength(4)
        self.peer_code_input.setMinimumHeight(30);self.peer_code_input.setVisible(self._wan)
        self.devices.currentIndexChanged.connect(self._target_changed)
        self.peer_code_input.textChanged.connect(self._target_changed)
        self.send_code = QFrame(self.send_column); self.send_code.setObjectName('lanSendCode')
        if self._wan:
            local_code=QVBoxLayout(self.send_code);local_code.setContentsMargins(0,0,0,0);local_code.setSpacing(4)
            local_code.addWidget(_label('本机接收码',self.send_code,muted=True))
            send_code_layout=QHBoxLayout();send_code_layout.setContentsMargins(0,0,0,0);send_code_layout.setSpacing(4);local_code.addLayout(send_code_layout)
        else:
            send_code_layout=QHBoxLayout(self.send_code);send_code_layout.setContentsMargins(12,6,12,6)
            send_code_layout.addWidget(_label('确认码',self.send_code,muted=True),1)
        self.send_code_value=_CopyCodeButton('—',self.send_code);self.send_code_value.setObjectName('lanSendCodeValue')
        self.send_code_value.setSizePolicy(QSizePolicy.Policy.Preferred,QSizePolicy.Policy.Preferred)
        self.send_code_value.setAccessibleName('点击复制本机接收码' if self._wan else '点击复制确认码')
        send_code_layout.addWidget(self.send_code_value)
        self.code_progress=None
        if self._wan and callable(getattr(service,'code_identity',None)):
            self.send_code_value.code_provider=lambda:service.code_identity().get('code','')
            if callable(getattr(service,'reconnect',None)):
                self.send_code_value.clicked.connect(lambda:service.reconnect() if not service.code_identity().get('code') else None)
            self.code_progress=_CodeExpiryBar(service,self.send_code_value,self.send_code)
            local_code.addWidget(self.code_progress)
        if self._wan and hasattr(service,'identity_changed'):
            service.identity_changed.connect(self._code_identity_changed)
            self._code_identity_changed(service.code_identity() if callable(getattr(service,'code_identity',None))
                                        else dict(code=getattr(service,'code','')))
        if self._wan:
            self.pairing_card=QFrame(self.send_column);self.pairing_card.setObjectName('lanPairingCard')
            pair=QHBoxLayout(self.pairing_card);pair.setContentsMargins(12,10,12,10);pair.setSpacing(12)
            pair.addWidget(self.send_code,1)
            remote=QWidget(self.pairing_card);remote_layout=QVBoxLayout(remote);remote_layout.setContentsMargins(0,0,0,0);remote_layout.setSpacing(4)
            remote_layout.addWidget(self.peer_code_label);self.peer_code_input.setFixedHeight(36);remote_layout.addWidget(self.peer_code_input);remote_layout.addStretch(1)
            pair.addWidget(remote,1);send.addWidget(self.pairing_card)
        else:send.addWidget(self.send_code)
        send.addStretch(1)
        self.send_button = _button('发送', self); self.send_button.setObjectName('lanPrimary')
        self.send_button.setProperty('pressScale',True)
        self.send_button.setMinimumHeight(34)
        self.send_button.clicked.connect(self._send)
        # The WAN form has additional receive-code fields. Keep its natural
        # minimum height and scroll on short screens instead of letting Qt
        # resolve impossible fixed heights by overlapping adjacent controls.
        self.send_scroll=QScrollArea(self);self.send_scroll.setObjectName('lanSendScroll')
        self.send_scroll.setFrameShape(QFrame.Shape.NoFrame);self.send_scroll.setWidgetResizable(True)
        self.send_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        send.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        left.addWidget(self.send_column,1)
        self.send_scroll.setWidget(self.left_content);left_outer.addWidget(self.send_scroll,1)
        self.left_content.setAutoFillBackground(False);self.send_scroll.viewport().setAutoFillBackground(False)
        left_outer.addWidget(self.send_button)

        self.activity_column = QFrame(self); self.activity_column.setObjectName('lanActivityColumn')
        self.activity_column.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Expanding)
        activity = QVBoxLayout(self.activity_column); activity.setContentsMargins(0,0,0,0); activity.setSpacing(WORKBENCH_GAP_PX)
        self.tasks_card=QFrame(self.activity_column);self.tasks_card.setObjectName('lanWorkbenchCard')
        tasks=QVBoxLayout(self.tasks_card);tasks.setContentsMargins(CARD_MARGIN_PX,CARD_MARGIN_PX,CARD_MARGIN_PX,CARD_MARGIN_PX);tasks.setSpacing(10)
        transfer_heading = QHBoxLayout(); transfer_heading.setSpacing(8)
        section = _label('正在传输', self.activity_column); section.setObjectName('lanSection')
        transfer_heading.addWidget(section, 1)
        self.transfer_count = _label('0 项', self.activity_column, muted=True)
        self.transfer_count.setSizePolicy(QSizePolicy.Policy.Preferred,QSizePolicy.Policy.Preferred)
        transfer_heading.addWidget(self.transfer_count)
        tasks.addLayout(transfer_heading)
        self.transfers_body = QWidget(self.activity_column)
        self.transfers_layout = QVBoxLayout(self.transfers_body)
        self.transfers_layout.setContentsMargins(3,3,3,3); self.transfers_layout.setSpacing(10)
        self.transfers_layout.addStretch(1)
        self.transfers_body.setMinimumHeight(76)
        tasks.addWidget(self.transfers_body, 1)
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
        tasks.addLayout(pages);activity.addWidget(self.tasks_card)
        self.previous_page.hide(); self.next_page.hide(); self.page_label.hide()
        self.history_card=QFrame(self.activity_column);self.history_card.setObjectName('lanWorkbenchCard')
        history=QVBoxLayout(self.history_card);history.setContentsMargins(CARD_MARGIN_PX,CARD_MARGIN_PX,CARD_MARGIN_PX,CARD_MARGIN_PX);history.setSpacing(10)
        section = _label('传输历史', self.history_card); section.setObjectName('lanSection')
        history_heading=QHBoxLayout();history_heading.addWidget(section,1)
        self.clear_history=_button('清空',self.history_card);self.clear_history.setObjectName('lanClearButton')
        self.clear_history.setAccessibleName('清空传输历史');self.clear_history.setToolTip('清空历史记录，保留已接收的文件')
        self.clear_history.clicked.connect(self._clear_history);history_heading.addWidget(self.clear_history);history.addLayout(history_heading)
        self.history_body = QFrame(self.activity_column)
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
        self.history_scroll = QScrollArea(self.activity_column)
        self.history_scroll.setObjectName('lanHistoryScroll')
        self.history_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.history_scroll.setWidgetResizable(True)
        self.history_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.history_scroll.setMinimumHeight(110)
        self.history_scroll.setWidget(self.history_body)
        history.addWidget(self.history_scroll,1);activity.addWidget(self.history_card,1)
        self._render_history()
        self.destination_bar=QFrame(self);self.destination_bar.setObjectName('lanDestination')
        destination = QHBoxLayout(self.destination_bar); destination.setContentsMargins(0,0,0,0);destination.setSpacing(8)
        destination_label=_label('接收位置：', self, muted=True)
        destination_label.setSizePolicy(QSizePolicy.Policy.Fixed,QSizePolicy.Policy.Preferred)
        destination.addWidget(destination_label)
        self.receive_path = QLineEdit(self); self.receive_path.setReadOnly(True)
        self.receive_path.setAccessibleName('局域网接收位置')
        self.receive_path.setText(str(settings.lan_receive_dir if settings else Path.home()/'Downloads'))
        self.receive_path.setToolTip(self.receive_path.text())
        destination.addWidget(self.receive_path, 1)
        self.open_destination=_button('打开目录',self);self.open_destination.setIcon(ui_icon('folder'))
        self.open_destination.clicked.connect(self._open_destination);destination.addWidget(self.open_destination)
        self.choose_destination = _button('更改位置', self); self.choose_destination.clicked.connect(self._choose_destination)
        destination.addWidget(self.choose_destination)
        # Preserve the natural minimum of both right-hand sections on short
        # logical screens. Otherwise Qt compresses the layout past its fixed
        # children and overlays history, headings and the destination field.
        self.activity_scroll=QScrollArea(self);self.activity_scroll.setObjectName('lanActivityScroll')
        self.activity_scroll.setFrameShape(QFrame.Shape.NoFrame);self.activity_scroll.setWidgetResizable(True)
        self.activity_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        activity.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.activity_scroll.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Expanding)
        self.activity_scroll.setWidget(self.activity_column);columns.addWidget(self.activity_scroll,1)
        layout.addLayout(columns, 1)
        layout.addWidget(self.destination_bar)
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
        self._activity_fit_timer=QTimer(self);self._activity_fit_timer.setSingleShot(True)
        self._activity_fit_timer.timeout.connect(self._fit_activity)
        self.tasks_card.installEventFilter(self)
        self._peers_changed(self.service.peers()); self.queue_paths([],replace=True); self.apply_theme(theme)

    def eventFilter(self,watched,event):
        if watched is self.tasks_card and event.type()==QEvent.LayoutRequest:self._activity_fit_timer.start(0)
        return super().eventFilter(watched,event)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'_activity_fit_timer'):self._fit_queue();self._fit_activity()

    def _fit_queue(self):
        queue=self.queue_column.layout();payload=self.queue_area.layout()
        heading=max(self.send_heading.sizeHint().height(),self.add_files.minimumHeight())
        column=heading+(queue.spacing()+self.queue_scroll.height() if self._queued_paths else 0)
        margins=payload.contentsMargins();fixed=margins.top()+margins.bottom()+payload.spacing()+column
        drop=DROP_ZONE_COMPACT_HEIGHT_PX if self._queued_paths else max(DROP_ZONE_COMPACT_HEIGHT_PX,QUEUE_EMPTY_HEIGHT_PX-fixed)
        self.queue_column.setFixedHeight(column);self.drop_zone.setFixedHeight(drop)
        height=fixed+drop
        if self.queue_area.minimumHeight()!=height or self.queue_area.maximumHeight()!=height:self.queue_area.setFixedHeight(height)

    def _fit_activity(self):
        width=max(1,self.activity_scroll.viewport().width())
        heights=[]
        for card in (self.tasks_card,self.history_card):
            inner=card.layout();inner.activate();wrapped=inner.totalHeightForWidth(width) if inner.hasHeightForWidth() else -1
            height=max(card.minimumSizeHint().height(),wrapped)
            if card.minimumHeight()!=height:card.setMinimumHeight(height)
            heights.append(height)
        required=sum(heights)+WORKBENCH_GAP_PX
        if self.activity_column.minimumHeight()!=required:self.activity_column.setMinimumHeight(required)
        self.activity_column.layout().activate()

    def set_paths_provider(self, callback): self._paths_provider = callback

    def _load_history(self):
        if not self._history_path: return []
        try:
            value = json.loads(self._history_path.read_text(encoding='utf-8'))
            if isinstance(value, list):
                result=[]
                for row in value[:12]:
                    if not isinstance(row,dict) or not all(isinstance(row.get(key),str) for key in ('name','peer','state','direction','when')):continue
                    paths=row.get('paths',[])
                    row['paths']=[path for path in paths[:128] if isinstance(path,str) and len(path)<=32768 and Path(path).is_absolute()] if isinstance(paths,list) else []
                    result.append(row)
                return result
        except (OSError, ValueError, UnicodeError): pass
        return []

    def _set_history_context(self, identifier, paths):
        context = {'history_name': Path(paths[0]).name if paths else '',
                   'history_count': len(paths),'paths':list(map(str,paths))}
        if identifier in self._rows:
            self._rows[identifier].update(context)
            if paths:self._rows[identifier]['icon'].set_source(paths[0])
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

    def _render_history(self):
        self.clear_history.setEnabled(bool(self._history))
        for widget in self.history_rows:
            self.history_layout.removeWidget(widget)
            widget.deleteLater()
        self.history_rows.clear()
        for separator in self.history_separators:
            self.history_layout.removeWidget(separator)
            separator.deleteLater()
        self.history_separators.clear()
        self.history_label.setVisible(not self._history)
        for index, record in enumerate(self._history[:12]):
            if index:
                separator = QFrame(self.history_body)
                separator.setObjectName('lanHistoryDivider')
                separator.setFixedHeight(1)
                self.history_layout.addWidget(separator)
                self.history_separators.append(separator)
            widget = _HistoryRow(record, self.artwork_cache, self.history_body)
            widget.icon.dark = self._dark
            widget.remove_button.dark = self._dark
            widget.remove_requested.connect(lambda i=index: self._delete_history_record(i))
            widget.reveal_requested.connect(self._reveal_file)
            self.history_layout.addWidget(widget)
            self.history_rows.append(widget)

    def _clear_history(self):
        previous=self._history;self._history=[]
        try:self._save_history()
        except OSError as error:
            self._history=previous;self.status_label.setText('无法清空传输历史：'+str(error));return
        self._render_history()

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
        self._render_history()

    def queue_paths(self, paths, *, replace=False):
        previous=list(self._queued_paths)
        if replace:self._queued_paths=[]
        present=set(self._queued_paths)
        for value in paths:
            path = Path(value)
            if str(path) not in present:self._queued_paths.append(str(path));present.add(str(path))
        if previous==self._queued_paths and getattr(self,'_queue_rendered',False):return
        self._queue_rendered=True
        count = len(self._queued_paths)
        self.drop_zone.title.setText('拖入文件/文件夹，或点击添加')
        self.drop_zone.hint.hide()
        self.clear_queue.setEnabled(bool(count))
        self.send_button.setText(f'发送 {count} 项' if count else '发送文件')
        self.send_heading_label.setText(f'待发送项目 · {count}' if count else '待发送项目')
        wanted=set(self._queued_paths);added=[]
        for value in list(self._queue_row_map):
            if value not in wanted:
                widget=self._queue_row_map.pop(value);self._queue_labels.pop(value,None)
                self.queue_layout.removeWidget(widget);widget.deleteLater()
        for value in self._queued_paths:
            if value in self._queue_row_map:continue
            row=QFrame(self.queue_body);row.setObjectName('lanQueueRow')
            row.setFixedHeight(QUEUE_ROW_HEIGHT_PX)
            line=QHBoxLayout(row);line.setContentsMargins(8,4,8,4);line.setSpacing(8)
            path=Path(value);row.artwork=FileArtwork(self.artwork_cache,row);row.artwork.set_source(value)
            line.addWidget(row.artwork);name=_ElidedLabel(path.name,row);line.addWidget(name,1)
            size=_label('读取信息中…',row,muted=True);size.setWordWrap(False);size.setSizePolicy(QSizePolicy.Policy.Fixed,QSizePolicy.Policy.Preferred)
            line.addWidget(size)
            remove=_button('',row);remove.setObjectName('lanQueueRemove');remove.setIcon(ui_icon('close'))
            remove.setFixedSize(26,26);remove.setAccessibleName('移除 '+path.name)
            remove.clicked.connect(lambda checked=False,ref=value:self.queue_paths([item for item in self._queued_paths if item!=ref],replace=True))
            line.addWidget(remove);self._queue_row_map[value]=row;self._queue_labels[value]=size
            self.queue_layout.addWidget(row);self._queue_info_ready(value);added.append(value)
        self.queue_rows=[self._queue_row_map[value] for value in self._queued_paths]
        if [value for value in previous if value in wanted]+added!=self._queued_paths:
            for row in self.queue_rows:self.queue_layout.removeWidget(row)
            for row in self.queue_rows:self.queue_layout.addWidget(row)
        self.queue_scroll.setFixedHeight(min(QUEUE_MAX_HEIGHT_PX, count*(QUEUE_ROW_HEIGHT_PX+6)))
        self.queue_scroll.setVisible(bool(count))
        self._fit_queue()

    def _queue_info_ready(self,ref):
        label=self._queue_labels.get(ref)
        if label is None:return
        info=self.file_metadata.request(ref)
        if info is None:return
        label.setText('文件夹' if info['is_dir'] else _size(info['size']) if info['exists'] else '无法读取文件信息')
        row=self._queue_row_map[ref]
        if row.artwork.folder!=info['is_dir']:row.artwork.set_source(ref,folder=info['is_dir'])

    def _queue_info_deferred(self,ref):
        label=self._queue_labels.get(ref)
        if label is not None:label.setText('待读取')

    def _queue_visible_info(self,*_):
        start=max(0,self.queue_scroll.verticalScrollBar().value()//(QUEUE_ROW_HEIGHT_PX+6))
        for ref in self._queued_paths[start:start+4]:self._queue_info_ready(ref)

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

    def _open_destination(self):
        folder=Path(self.receive_path.text())
        if not folder.is_dir():self.status_label.setText('接收位置不可用，请先选择一个有效文件夹。');return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):self.status_label.setText('无法打开接收文件夹。')

    def _reveal_file(self,path):
        try:reveal_file(path)
        except OSError as error:self.status_label.setText(str(error))

    def apply_theme(self, theme):
        if getattr(self,'_applied_theme',None)==theme:return
        self._applied_theme=theme
        self._dark = theme == 'dark'
        self.device_delegate.dark = self._dark
        c=ui_colors(theme)
        workbench=c['card'] if self._dark else '#EEF0F4'
        item_surface=c['control'] if self._dark else '#FFFFFF'
        self.setProperty('uiTheme',theme)
        font=reference_font()
        font.setPixelSize(13);self.setFont(font)
        color,muted,border,field,accent=c['text'],c['secondary'],c['border'],c['input'],c['action']
        faint='rgba(255,255,255,8)' if self._dark else 'rgba(0,0,0,6)'
        style=f"""
            QFrame#lanPanel {{ background:transparent; border:0; }}
            QFrame#lanPanel QLabel {{ color:{color}; background:transparent; border:0; font-size:12px; }}
            QFrame#lanPanel QLabel#lanSection {{ font-size:12px; font-weight:700; }}
            QFrame#lanPanel QLabel#lanMuted {{ color:{muted}; }}
            QFrame#lanPanel QLabel#lanStatus {{ color:{c['red']}; font-size:11px; padding:2px 20px; }}
            QFrame#lanSendColumn {{ background:{faint}; border:0; border-right:1px solid {c['divider']}; }}
            QFrame#lanPayloadArea {{background:{c['surface']};border:0;border-bottom:1px solid {c['divider']};}}
            QFrame#lanActivityColumn, QFrame#lanQueueBody, QFrame#lanHistoryGroup {{ background:transparent; border:0; }}
            QFrame#lanQueueRow, QFrame#lanHistoryRow {{ background:{faint}; border:1px solid {border}; border-radius:{TRANSFER_CORNER_RADIUS_PX}px; }}
            QFrame#lanHistoryDivider {{ background:transparent; border:0; max-height:0; }}
            QFrame#lanTransferRow {{ background:{c['card']}; border:1px solid {border}; border-radius:{TRANSFER_CORNER_RADIUS_PX}px; }}
            QFrame#lanTransferRow[offerFrame="true"] {{ background:transparent; border:0; }}
            QFrame#lanOfferCard {{ background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 rgba(10,132,255,51),stop:1 rgba(86,125,175,26)); border:1px solid rgba(10,132,255,102); border-radius:{TRANSFER_CORNER_RADIUS_PX}px; }}
            QFrame#lanPanel QLabel#lanOfferKicker {{ color:white; background:{accent}; border-radius:{TRANSFER_CORNER_RADIUS_PX}px; font-size:11px; font-weight:700; padding:2px 8px; }}
            QFrame#lanPanel QLabel#lanOfferPeer {{ color:{color}; font-size:12px; font-weight:700; }}
            QFrame#lanPanel QLabel#lanOfferFile {{ color:{muted}; font-size:12px; }}
            QFrame#lanPanel QLabel#lanOfferMeta {{ color:{muted}; font-size:11px; }}
            QFrame#lanOfferCode {{ background:transparent; border:0; }}
            QFrame#lanPanel QLabel#lanOfferCodeCaption {{ color:{muted}; font-size:11px; }}
            QFrame#lanPanel QPushButton#lanOfferCodeValue {{ color:{color}; font-size:18px; font-weight:700; font-family:monospace; }}
            QFrame#lanPanel QLabel#lanTransferTitle {{ color:{color}; font-size:12px; font-weight:700; }}
            QFrame#lanPanel QLabel#lanTransferDetail {{ color:{muted}; font-size:11px; }}
            QFrame#lanPanel QPushButton#lanOfferAccept {{ color:#0D0D0E; background:{c['green']}; border-color:transparent; font-weight:700; }}
            QFrame#lanPanel QLabel#lanHistoryTitle {{ color:{c['muted']}; font-size:11px; font-weight:400; }}
            QFrame#lanPanel QLabel#lanHistoryDetail {{ color:{color}; font-size:12px; font-weight:500; }}
            QFrame#lanPanel QLabel#lanHistoryWhen {{ color:{muted}; font-size:11px; }}
            QFrame#lanPanel QLabel#lanHistoryState {{ background:transparent; border:0; font-size:10px; }}
            QFrame#lanPanel QLabel#lanHistoryState[statusKind="complete"] {{ color:{c['green']}; }}
            QFrame#lanPanel QLabel#lanHistoryState[statusKind="cancelled"] {{ color:{c['orange']}; }}
            QFrame#lanPanel QLabel#lanHistoryState[statusKind="failed"] {{ color:{c['red']}; }}
            QFrame#lanPanel QLabel#lanHistoryState[statusKind="other"] {{ color:{muted}; }}
            QFrame#lanPanel QPushButton#lanHistoryRemove {{ background:transparent; border:0; border-radius:{TRANSFER_CORNER_RADIUS_PX}px; color:{c['muted']}; padding:0; font-size:12px; }}
            QFrame#lanPanel QPushButton#lanHistoryRemove:hover {{ background:{c['hover']}; color:{color}; }}
            QFrame#lanDropZone {{ background:transparent; border:0; }}
            QFrame#lanDropZone QLabel#lanDropTitle {{ font-size:12px; font-weight:500; color:{color}; }}
            QFrame#lanDropZone QLabel#lanDropHint {{ font-size:11px; color:{muted}; }}
            QFrame#lanSendCode {{ background:{c['card']}; border:1px solid {border}; border-radius:{TRANSFER_CORNER_RADIUS_PX}px; }}
            QFrame#lanPanel QPushButton#lanSendCodeValue {{ color:{c['cyan'] if self._dark else accent}; font-size:16px; font-weight:700; font-family:monospace; }}
            QFrame#lanPanel QPushButton {{ color:{color}; background:{c['control']}; border:1px solid transparent; border-radius:{TRANSFER_CORNER_RADIUS_PX}px; padding:4px 12px; font-size:12px; }}
            QFrame#lanPanel QPushButton#lanTextButton {{ color:{muted}; background:transparent; border:0; padding:2px 4px; min-height:18px; }}
            QFrame#lanPanel QPushButton#lanTextButton:hover {{ color:{accent}; background:{c['hover']}; }}
            QFrame#lanPanel QPushButton#lanPrimary {{ color:white; background:{accent}; border-color:transparent; border-radius:{TRANSFER_CORNER_RADIUS_PX}px; font-weight:700; }}
            QFrame#lanPanel QPushButton#lanPrimary:disabled {{ color:{c['muted']}; background:{c['control']}; border-color:transparent; }}
            QFrame#lanPanel QPushButton[accessibleName="取消传输"] {{ color:{c['red']}; background:transparent; border:0; padding:0; }}
            QFrame#lanPanel QPushButton:hover {{ background:{c['hover']}; }}
            QFrame#lanPanel QPushButton:pressed {{ background:{c['selected']}; }}
            QFrame#lanPanel QPushButton:disabled {{ color:{c['muted']}; }}
            QFrame#lanPanel QComboBox {{ color:{color}; background:{field}; border:1px solid {c['input_border']}; border-radius:{TRANSFER_CORNER_RADIUS_PX}px; padding:5px 28px 5px 10px; font-size:12px; }}
            QFrame#lanPanel QComboBox::drop-down {{ border:0; width:26px; }}
            QFrame#lanPanel QComboBox::down-arrow {{ image:none; width:0; }}
            QFrame#lanPanel QAbstractItemView {{ background:{field}; color:{color}; border:1px solid {border}; border-radius:{TRANSFER_CORNER_RADIUS_PX}px; padding:4px; outline:0; }}
            QFrame#lanPanel QLineEdit {{ color:{color}; background:{c['control']}; border:0; border-radius:{TRANSFER_CORNER_RADIUS_PX}px; padding:3px 6px; font-size:11px; font-family:monospace; }}
            QFrame#lanPanel QPushButton[accessibleName="上一页传输"], QFrame#lanPanel QPushButton[accessibleName="下一页传输"] {{ min-width:24px; max-width:24px; min-height:24px; max-height:24px; padding:0; }}
            QFrame#lanPanel QProgressBar {{ border:0; background:{c['control']}; height:6px; max-height:6px; border-radius:3px; }}
            QFrame#lanPanel QProgressBar::chunk {{ background:{c['cyan']}; border-radius:3px; }}
        """
        style+=f'''
            QFrame#lanPayloadArea,QFrame#lanSendColumn,QFrame#lanWorkbenchCard {{background:{workbench};border:0;border-radius:{TRANSFER_CORNER_RADIUS_PX}px;}}
            QWidget#lanLeftContent {{background:transparent;border:0;}}
            QFrame#lanQueueRow,QFrame#lanHistoryRow,QFrame#lanTransferRow {{background:{item_surface};border:0;border-radius:{TRANSFER_CORNER_RADIUS_PX}px;}}
            QFrame#lanSendCode {{background:{c['control']};border:0;border-radius:{TRANSFER_CORNER_RADIUS_PX}px;}}
            QFrame#lanPairingCard {{background:{c['control']};border:0;border-radius:{TRANSFER_CORNER_RADIUS_PX}px;}}
            QFrame#lanPairingCard QFrame#lanSendCode {{background:transparent;border:0;}}
            QFrame#lanDropZone {{background:{c['control']};border:0;border-radius:{TRANSFER_CORNER_RADIUS_PX}px;}}
            QFrame#lanDropZone:hover {{background:{c['hover']};}}
            QFrame#lanPanel QPushButton#lanQueueRemove {{background:transparent;border:0;border-radius:{TRANSFER_CORNER_RADIUS_PX}px;padding:0;}}
            QFrame#lanPanel QPushButton#lanQueueRemove:hover {{background:{c['hover']};}}
            QFrame#lanPanel QPushButton#lanRevealFile {{background:transparent;padding:0;}}
            QFrame#lanPanel QPushButton#lanRevealFile:hover {{background:{c['hover']};}}
            QFrame#lanPanel QComboBox {{border:0;}}
            QFrame#lanPanel QComboBox#lanDevicePicker {{background:{'#343438' if self._dark else '#E5E5EA'};border:0;border-radius:{TRANSFER_CORNER_RADIUS_PX}px;}}
            QFrame#lanPanel QComboBox#lanDevicePicker:hover {{background:{c['selected']};}}
            QFrame#lanPanel QComboBox#lanDevicePicker:disabled {{color:{c['muted']};background:{c['control']};}}
            QFrame#lanPanel QComboBox:focus,QFrame#lanPanel QLineEdit:focus {{border:1px solid {c['link']};}}
            QFrame#lanPanel QLabel#lanOfferKicker {{background:{c['action']};color:{c['on_action']};}}
            QFrame#lanPanel QPushButton#lanTextButton:hover {{color:{c['link']};}}
            QFrame#lanPanel QPushButton#lanClearButton {{background:transparent;border:0;color:{c['secondary']};}}
            QFrame#lanPanel QPushButton#lanClearButton:hover {{background:rgba(255,69,58,30);color:{c['red']};}}
            QFrame#lanPanel QPushButton#lanClearButton:pressed {{background:rgba(255,69,58,48);color:{c['red']};}}
            QFrame#lanPanel QPushButton#lanClearButton:disabled {{color:{c['muted']};background:transparent;}}
            QFrame#lanPanel QPushButton[keyboardFocus="true"] {{border:1px solid {c['link']};}}
            QFrame#lanPanel QPushButton#lanOfferAccept {{background:{c['action']};color:{c['on_action']};}}
            QFrame#lanRouteHint {{background:{'#173829' if self._dark else '#E3F5EA'};border:0;border-radius:{TRANSFER_CORNER_RADIUS_PX}px;}}
            QFrame#lanPanel QFrame#lanRouteHint QLabel {{color:{'#7DE0A4' if self._dark else '#187A43'};background:transparent;}}
            QFrame#lanPanel QFrame#lanRouteHint QPushButton {{color:{'#7DE0A4' if self._dark else '#187A43'};background:{'#244B37' if self._dark else '#D3EEDC'};border:0;}}
            QFrame#lanPanel QFrame#lanRouteHint QPushButton:hover {{background:{'#2E6045' if self._dark else '#C1E3CD'};}}
            QFrame#lanPanel QLabel {{font-size:12px;}}
            QFrame#lanPanel QLabel#lanSection {{font-size:12px;font-weight:600;}}
            QFrame#lanPanel QLabel#lanDropTitle {{font-size:11px;}}
            QFrame#lanPanel QLabel#lanHistoryTitle {{font-size:10px;color:{c['secondary']};}}
            QFrame#lanPanel QLabel#lanHistoryDetail {{font-size:12px;}}
            QFrame#lanPanel QLabel#lanHistoryState {{font-size:10px;}}
            QFrame#lanPanel QPushButton, QFrame#lanPanel QComboBox {{font-size:12px;}}
            QFrame#lanPanel QLineEdit {{font-size:11px;}}
            QScrollArea#lanSendScroll, QScrollArea#lanActivityScroll, QScrollArea#lanQueueScroll, QScrollArea#lanHistoryScroll {{background:transparent;border:0;}}
            QScrollArea#lanSendScroll > QWidget, QScrollArea#lanActivityScroll > QWidget {{background:transparent;}}
            QScrollArea#lanQueueScroll > QWidget > QWidget, QScrollArea#lanHistoryScroll > QWidget > QWidget {{background:transparent;}}
            QScrollBar:vertical {{background:transparent;width:6px;margin:2px 0;}}
            QScrollBar::handle:vertical {{background:{c['input_border']};border-radius:3px;min-height:24px;}}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{height:0;}}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{background:transparent;}}
        '''
        from native_ui_common import readable_qss
        style=readable_qss(style)
        self.setStyleSheet(style)
        for row in self._rows.values():
            row['cancel'].dark = self._dark; row['cancel'].update()
            if row.get('offer_icon'):
                row['offer_icon'].dark = self._dark; row['offer_icon'].update()
            if row.get('offer_shadow'):
                row['offer_shadow'].setColor(QColor(0,0,0,48 if self._dark else 18))
        for row in self.history_rows:
            row.icon.dark = self._dark; row.icon.update()
            row.remove_button.dark = self._dark; row.remove_button.update()
            row.remove_button.setIcon(ui_icon('trash',c['secondary']));row.reveal_button.setIcon(ui_icon('folder',c['secondary']))
        for button in (self.previous_page,self.next_page):
            button.dark=self._dark;button.update()
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
                self.devices.setItemData(index,'在线' if peer.get('online') else '离线',Qt.ItemDataRole.ToolTipRole)
                self.devices.setItemData(index,peer.get('platform','unknown'),Qt.ItemDataRole.UserRole+1)
                self.devices.setItemData(index,bool(peer.get('online')),Qt.ItemDataRole.UserRole+2)
                self.devices.setItemData(index,bool(peer.get('account_device')),Qt.ItemDataRole.UserRole+3)
                self.devices.setItemData(index,name+('，同账号' if peer.get('account_device') else '')+('，在线' if peer.get('online') else '，离线'),Qt.AccessibleTextRole)
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
        if self.code_progress:self.code_progress._tick()
        else:self.send_code_value.setText(identity.get('code') or '连接中…')

    def _target_changed(self,*_):
        if not self._wan:return
        nearby=getattr(self.service,'nearby_peer',lambda _:None)(self.devices.currentData()) if not self.peer_code_input.text().strip() else None
        self.nearby_hint.setVisible(bool(nearby))

    def _recommend_lan(self):
        peer=getattr(self.service,'nearby_peer',lambda _:None)(self.devices.currentData())
        if not peer:return
        paths=list(self._queued_paths)
        if not paths and self._paths_provider:paths=list(self._paths_provider() or [])
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
        if not paths: paths = list(self._paths_provider() or []) if self._paths_provider else []
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
        artwork=FileArtwork(self.artwork_cache,heading_widget);heading.addWidget(artwork)
        title = _ElidedLabel(('发送至 ' if direction == 'send' else '接收自 ') + peer, heading_widget)
        title.setObjectName('lanTransferTitle')
        heading.addWidget(title, 1); cancel = _CancelButton(frame); cancel.dark = self._dark
        cancel.clicked.connect(lambda: self.service.cancel(identifier)); heading.addWidget(cancel); outer.addWidget(heading_widget)
        detail = _label('', frame, muted=True); detail.setObjectName('lanTransferDetail')
        detail.setWordWrap(True)
        outer.addWidget(detail)
        bar = QProgressBar(frame); bar.setRange(0, 1000); bar.setTextVisible(False); outer.addWidget(bar)
        self.transfers_layout.insertWidget(0,frame)
        self._row_sequence += 1
        row = {'frame': frame, 'heading': heading_widget, 'icon':artwork,'title': title, 'detail': detail, 'bar': bar, 'cancel': cancel,
               'extra': None, 'sequence': self._row_sequence}
        row.update(self._history_context.pop(identifier, {}))
        artwork.set_source((row.get('paths') or [''])[0],row.get('history_name',''))
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

    def focus_transfer(self,identifier,snapshot=None):
        if identifier not in self._rows and snapshot:self._progress(snapshot)
        row=self._rows.get(identifier)
        if row is None:return
        if row.get('finished'):
            self.activity_scroll.ensureWidgetVisible(self.history_card,8,8);return
        rows=sorted((value for value in self._rows.values() if not value.get('finished')),key=lambda value:-value['sequence'])
        self._row_page=rows.index(row)//2;self._refresh_transfer_pages()
        self.activity_scroll.ensureWidgetVisible(row['frame'],8,8)

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
        self.transfer_count.setText(f"{sum(not row.get('finished') for row in rows)} 项进行中")
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
        state=getattr(self.service,'_pending',{}).get(offer['id'],{})
        if offer.get('account_device') is True and state.get('destination'):return
        identifier = offer['id']; self._offers[identifier] = offer
        if identifier in self._rows:return
        row = self._row(identifier, offer['peer'], 'receive')
        row['history_name'] = str((offer.get('names') or [''])[0])
        row['history_count'] = int(offer['count'])
        row['icon'].set_source('',row['history_name'])
        names = '、'.join(str(name) for name in offer.get('names', [])[:3])
        if len(offer.get('names', [])) > 3: names += ' 等'
        confirmation=str(offer.get('device_code') or offer.get('code') or '').strip()
        code_label='设备码' if self._wan else '确认码'
        row['detail'].setText(f"{names} · {_size(offer['total'])}"+(f" · {code_label} {confirmation}" if confirmation else ''))
        row['heading'].hide(); row['detail'].hide(); row['bar'].hide()

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
        self._fit_activity()
        self.status_label.clear()

    @staticmethod
    def _restore_transfer_surface(row):
        frame=row['frame'];frame.setProperty('offerFrame',False)
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
        if data.get('route'):row['route']=data['route']
        if 'relay_bytes' in data:row['relay_bytes']=max(row.get('relay_bytes',0),int(data['relay_bytes']))
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
                if not row['icon'].ref and row['icon'].name!=name:row['icon'].set_source('',name)
                row['title'].setToolTip(('发送至 ' if data['direction']=='send' else '接收自 ')+str(data.get('peer') or ''))
            eta=_remaining((total-received)/speed) if speed and total>received else '正在完成' if total and received>=total else '计算剩余时间…'
            transport=_transport_label(row)
            row['detail'].setText(f'{_size(received)} / {_size(total)} · ' + (f'{_size(speed)}/s' if speed else '计算速度…')+' · '+eta+transport)
            if data.get('route_measurements'):row['detail'].setToolTip(_transport_hint(data))
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
                    'count':row.get('history_count'),
                    'state':{'complete':'完成','cancelled':'已取消','error':'失败','interrupted':'已中断'}[state],
                    'direction':str(data.get('direction') or 'send')}
                record['paths']=row.get('paths',[])[:128]
                if row.get('route'):record.update(route=row['route'],relay_bytes=row.get('relay_bytes',0))
                if state in ('error','interrupted'):record['error']=str(data.get('message') or '')[:600]
                row['history_record']=record;self._history.insert(0,record)
                self._history = self._history[:12]; self._render_history()
                try:self._save_history()
                except OSError:pass
            self._refresh_transfer_pages()
        if state=='error':self.status_label.setText(str(data.get('message') or '传输失败，请重试。')[:600])
        elif state not in ('interrupted',):self.status_label.clear()
        self._fit_activity()

    def _completed(self, result):
        paths=list(result.get('paths') or [])
        row=self._rows.get(result['id'])
        if row and paths:
            row['paths']=paths
            row['icon'].set_source(paths[0])
            if row.get('history_record'):
                row['history_record']['paths']=paths[:128];self._render_history()
                try:self._save_history()
                except OSError as error:self.status_label.setText('无法保存传输记录：'+str(error))
        if result.get('direction') == 'receive': self.received_paths.emit(paths)


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
        if kind not in (QEvent.Type.DragEnter,QEvent.Type.DragMove,QEvent.Type.DragLeave,QEvent.Type.Drop):return False
        if not isinstance(watched,QWidget) or not (watched is self.dialog or self.dialog.isAncestorOf(watched)):return False
        zone=self.dialog.panel.drop_zone
        if kind==QEvent.Type.DragLeave:
            zone.dragLeaveEvent(event);return True
        paths=zone._paths(event)
        if not paths:
            zone.set_drag_active(False);event.ignore();return True
        if kind==QEvent.Type.Drop:
            zone.dragLeaveEvent(event);self.dialog.panel.queue_paths(paths)
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

    def show(self):
        self.chrome.prepare_open();self._window_fade.reopen();super().show()

    def done(self,result):
        if self._window_fade.defer_done(result,self.done):return
        super().done(result)

    def reject(self):
        if not self._closing_confirmed and self._active_transfers():self.close()
        else:super().reject()

    def __init__(self, service, settings, parent=None):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self._settings=settings;self._panels={};self._theme=settings.theme
        self._close_prompt=None;self._closing_confirmed=False;self._navigate_after_close=None
        self.finished.connect(self._finish_navigation)
        self._wan=getattr(service,'mode','lan')=='wan'
        self.setObjectName('lanDialog'); self.setWindowTitle('FlickDock · 文件互传')
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(False); self.resize(980, 704); self.setMinimumSize(824, 660)
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
        title = _label('文件互传', self.title_bar); title.setObjectName('lanDialogTitle')
        self.title_label=title
        title.setWordWrap(False);title.setSizePolicy(QSizePolicy.Fixed,QSizePolicy.Preferred)
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        title_row.addWidget(title)
        self.ready_label=_label('已就绪',self.title_bar);self.ready_label.setObjectName('transferReady')
        self.ready_label.setWordWrap(False);self.ready_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.back_button=_button('返回总览',self.title_bar);self.back_button.setObjectName('lanDialogBack')
        self.back_button.setIcon(ui_icon('left'));self.back_button.clicked.connect(lambda:self._navigate('overview'));title_row.addWidget(self.back_button)
        title_row.addWidget(self.ready_label)
        title_row.addStretch(1)
        content.addWidget(self.title_bar)
        self.mode_segment=QFrame(self.card);self.mode_segment.setObjectName('transferModeGroup')
        mode_row=QHBoxLayout(self.mode_segment);mode_row.setContentsMargins(4,4,4,4);mode_row.setSpacing(4)
        self.mode_group=QButtonGroup(self);self.mode_group.setExclusive(True);self.mode_buttons={}
        for mode,label in (('lan','局域网传输'),('wan','远程传输')):
            control=_button(label,self.mode_segment);control.setCheckable(True);control.setChecked((mode=='wan')==self._wan)
            control.setObjectName('transferModeSegment');self.mode_group.addButton(control);self.mode_buttons[mode]=control
            control.clicked.connect(lambda checked=False,mode=mode:self.mode_selected.emit(mode))
            mode_row.addWidget(control,1)
        close = _CloseButton('', self.title_bar); close.setObjectName('lanDialogClose')
        self.close_button=close
        close.setAutoDefault(False);close.setDefault(False);close.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        close.setFixedSize(32, 32); close.setAccessibleName('关闭局域网传输窗口')
        close.clicked.connect(self.close); title_row.addWidget(close)
        mode_band=QWidget(self.card);mode_band_layout=QVBoxLayout(mode_band);mode_band_layout.setContentsMargins(WORKBENCH_MARGIN_PX,8,WORKBENCH_MARGIN_PX,0);mode_band_layout.addWidget(self.mode_segment)
        content.addWidget(mode_band)
        self.panel_stack=QStackedWidget(self.card)
        self.panel_stack.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Ignored)
        from file_metadata import FileMetadataCache
        self.file_metadata=FileMetadataCache(self)
        self.panel = LanPanel(service, self.panel_stack, theme=settings.theme, settings=settings,metadata_cache=self.file_metadata)
        self.panel.lan_recommended.connect(self.lan_recommended)
        self._panels[service]=self.panel;self.panel_stack.addWidget(self.panel)
        if self._wan:self.close_button.setAccessibleName('关闭远程传输窗口')
        content.addWidget(self.panel_stack, 1)
        self.apply_theme(settings.theme)
        self.drop_router=_TransferDropRouter(self)
        from window_chrome import WindowChrome
        self.chrome=WindowChrome(self,self.title_bar,settings,'transfer',surface=self.card)
        if self._wan:service.identity_changed.connect(self._update_ready)
        self._update_ready()

    def _update_ready(self,*_):
        service=self.panel.service
        identity=service.code_identity() if getattr(service,'mode','lan')=='wan' else {}
        self.ready_label.setVisible(bool(identity.get('online') and identity.get('code')))

    def _navigate(self, target):
        self._navigate_after_close=target
        self.close()

    def _finish_navigation(self,*_):
        target=self._navigate_after_close;self._navigate_after_close=None
        if target:getattr(self,target+'_requested').emit()

    def _active_transfers(self):
        return [(service,identifier) for service in self._panels
                for identifier,transfer in tuple(getattr(service,'_transfers',{}).items())
                if getattr(transfer,'active',False)]

    def closeEvent(self,event):
        if self._closing_confirmed or not self._active_transfers():
            super().closeEvent(event);return
        event.ignore()
        if self._close_prompt is None:
            from app_settings import ReferenceModalDialog
            prompt=ReferenceModalDialog(self,surface_width=340,modal=True)
            prompt.setWindowTitle('关闭传输窗口');prompt.set_surface_theme(self._theme)
            text=_label('关闭将中断正在进行的传输，是否关闭？',prompt.body)
            text.setStyleSheet('color:'+ui_colors(self._theme)['text']+';font-size:13px;')
            prompt.body_layout.addWidget(text)
            actions=QHBoxLayout();actions.addStretch()
            cancel=_button('取消',prompt.body);confirm=_button('确定',prompt.body)
            actions.addWidget(cancel);actions.addWidget(confirm);prompt.body_layout.addLayout(actions)
            cancel.clicked.connect(prompt.reject)
            def cancel_close():self._navigate_after_close=None
            prompt.rejected.connect(cancel_close)
            def confirmed():
                for service,identifier in self._active_transfers():service.cancel(identifier)
                self._closing_confirmed=True;prompt.accept();self.close()
            confirm.clicked.connect(confirmed)
            self.close_cancel_button=cancel;self.close_confirm_button=confirm;self._close_prompt=prompt
        self._close_prompt.show();self._close_prompt.raise_();self._close_prompt.activateWindow()

    def set_service(self,service):
        """Swap only the content, keeping one native surface and shadow alive."""
        if self.panel.service is service:return self.panel
        queued=list(self.panel._queued_paths)
        try:
            if service not in self._panels:
                panel=LanPanel(service,self.panel_stack,theme=self._theme,settings=self._settings,metadata_cache=self.file_metadata)
                panel.lan_recommended.connect(self.lan_recommended)
                self._panels[service]=panel;self.panel_stack.addWidget(panel)
                if getattr(service,'mode','lan')=='wan':service.identity_changed.connect(self._update_ready)
                for widget in (panel,*panel.findChildren(QWidget)):widget.setAcceptDrops(True)
            self.panel=self._panels[service];self._wan=getattr(service,'mode','lan')=='wan'
            self.panel.queue_paths(queued,replace=True)
            self.mode_buttons['wan' if self._wan else 'lan'].setChecked(True)
            if not hasattr(self,'_page_fade'):
                from ui_transitions import SnapshotFade
                self._page_fade=SnapshotFade(self.panel_stack)
            self._page_fade.change(lambda:self.panel_stack.setCurrentWidget(self.panel))
            title='远程传输' if self._wan else '局域网互传'
            self.title_label.setText('文件互传');self.setWindowTitle('FlickDock · 文件互传')
            self.close_button.setAccessibleName('关闭'+title+'窗口')
            if self._wan and callable(getattr(service,'check_code',None)):
                service.check_code();self.panel._code_identity_changed(service.code_identity())
        finally:
            self._update_ready()
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
            radius=TRANSFER_CORNER_RADIUS_PX,blur=18,offset=QPointF(0,4),
            color=QColor(0,0,0,78) if self._theme=='dark' else QColor(15,23,42,32))
        p.end()

    def showEvent(self, event):
        self._closing_confirmed=False
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
        self.back_button.setIcon(ui_icon('left',c['secondary']))
        self.card.setStyleSheet(f'''
            QFrame#lanDialogCard {{ background:{surface}; border:1px solid {c['glass_border']}; border-radius:{TRANSFER_CORNER_RADIUS_PX}px; }}
            QWidget#lanTitleBar {{ background:transparent; border:0; border-bottom:1px solid {c['divider']}; }}
            QLabel#lanDialogTitle {{ color:{ink}; font:700 15px '{font_family}'; background:transparent; border:0; }}
            QLabel#transferReady {{ color:{c['green']}; font:500 11px '{font_family}'; background:transparent; border:0; }}
            QPushButton#lanDialogClose,QPushButton#lanDialogBack {{ background:transparent; color:{c['secondary']}; border:0; border-radius:{TRANSFER_CORNER_RADIUS_PX}px;
                                          font:12px '{font_family}'; padding:0; }}
            QPushButton#lanDialogBack {{ background:{c['control']};padding:4px 10px; }}
            QFrame#transferModeGroup {{background:{c['control']};border:0;border-radius:{TRANSFER_CORNER_RADIUS_PX}px;}}
            QPushButton#transferModeSegment {{background:transparent;color:{c['secondary']};border:0;border-radius:{TRANSFER_CORNER_RADIUS_PX}px;padding:7px 10px;font:12px '{font_family}';}}
            QPushButton#transferModeSegment:checked {{background:{c['hover']};color:{ink};}}
            QPushButton#transferModeSegment:hover {{background:{c['selected']};}}
            QPushButton#transferModeSegment:focus {{border:0;}}
            QPushButton#transferModeSegment[keyboardFocus="true"] {{background:{c['selected']};border:0;}}
            QPushButton#lanDialogClose:hover {{ background:{c['red']}; color:white; }}
            QPushButton#lanDialogBack:hover {{ background:{hover}; color:{ink}; }}
        ''')
        self.update()
        for panel in self._panels.values():panel.apply_theme(theme)
        from native_ui_common import readable_qss
        self.card.setStyleSheet(readable_qss(self.card.styleSheet()))
