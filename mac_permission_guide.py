"""Nonactivating, draggable help at the bottom of macOS Accessibility settings."""
from __future__ import annotations

import logging
import time

from PySide6.QtCore import QMimeData, QRect, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QDrag, QGuiApplication
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from native_ui_common import colors
from platform_native import configure_passive_cocoa_panel, keep_tool_visible_when_inactive


LOG = logging.getLogger(__name__)
GUIDE_INSET = 12
SETTINGS_FOOTER_CLEARANCE = 44


def guide_geometry(window, available, size):
    """Keep the guide within the visible settings pane and above its footer."""
    visible = window.intersected(available)
    pane = visible.adjusted(GUIDE_INSET, GUIDE_INSET, -GUIDE_INSET, -SETTINGS_FOOTER_CLEARANCE)
    if pane.width() < size.width() or pane.height() < size.height():
        return QRect()
    # Center over the content pane, leaving the navigation sidebar accessible.
    content_left = window.left() + min(220, window.width() // 3)
    content_center = (max(content_left, pane.left()) + pane.right()) // 2
    left = max(pane.left(), min(content_center - size.width() // 2, pane.right() - size.width() + 1))
    return QRect(left, pane.bottom() - size.height() + 1, size.width(), size.height())


def settings_window():
    """Use only window bounds and the foreground process, without screen capture."""
    import AppKit
    import Quartz

    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is None or front.bundleIdentifier() != 'com.apple.systempreferences':
        return QRect()
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    for window in windows or ():
        if window.get(Quartz.kCGWindowOwnerPID) != front.processIdentifier() or window.get(Quartz.kCGWindowLayer) != 0:
            continue
        bounds = window.get(Quartz.kCGWindowBounds, {})
        width, height = int(bounds.get('Width', 0)), int(bounds.get('Height', 0))
        if width >= 360 and height >= 250:
            return QRect(int(bounds['X']), int(bounds['Y']), width, height)
    return QRect()


class _AppDragButton(QPushButton):
    def __init__(self, bundle, parent):
        super().__init__(parent)
        self.bundle = bundle
        self.press = None
        self.drag_active = False
        self.setAccessibleName('将当前 FlickDock 拖入辅助功能列表')
        self.setToolTip('拖入系统设置的辅助功能列表')
        self.setIcon(QApplication.windowIcon())
        self.setIconSize(QSize(36, 36))
        self.setFixedSize(48, 48)
        self.setCursor(Qt.OpenHandCursor)
        self.setAutoDefault(False)

    def mime_data(self):
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(self.bundle))])
        return mime

    def mousePressEvent(self, event):
        self.press = event.position().toPoint() if event.button() == Qt.LeftButton else None
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self.press = None
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        if (self.press is None or not event.buttons() & Qt.LeftButton or
                (event.position().toPoint() - self.press).manhattanLength() < QApplication.startDragDistance()):
            super().mouseMoveEvent(event)
            return
        self.press = None
        self.setDown(False)
        drag = QDrag(self)
        drag.setMimeData(self.mime_data())
        drag.setPixmap(self.icon().pixmap(self.iconSize()))
        self.drag_active = True
        try:
            drag.exec(Qt.CopyAction | Qt.LinkAction, Qt.CopyAction)
        finally:
            self.drag_active = False
            drag.deleteLater()


class MacAccessibilityGuide(QWidget):
    def __init__(self, bundle, backend, theme, font, reveal):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowDoesNotAcceptFocus)
        self.backend = backend
        self.setFont(font)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        keep_tool_visible_when_inactive(self)
        self.setWindowTitle('FlickDock · 辅助功能授权引导')
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._follow_settings)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        surface = QFrame(self)
        surface.setObjectName('accessibilityGuide')
        layout.addWidget(surface)
        body = QHBoxLayout(surface)
        body.setContentsMargins(12, 10, 10, 10)
        body.setSpacing(10)
        self.app_icon = _AppDragButton(bundle, surface)
        self.app_icon.clicked.connect(reveal)
        body.addWidget(self.app_icon, 0, Qt.AlignVCenter)
        steps = QVBoxLayout()
        steps.setSpacing(3)
        heading = QLabel('允许当前 FlickDock', surface)
        heading.setObjectName('guideTitle')
        steps.addWidget(heading)
        steps.addWidget(QLabel('将图标拖入辅助功能列表，然后开启开关。', surface))
        hint = QLabel('旧条目可先移除。点击图标可在 Finder 定位。', surface)
        hint.setObjectName('guideHint')
        steps.addWidget(hint)
        body.addLayout(steps, 1)
        close = QPushButton('×', surface)
        close.setAccessibleName('关闭授权引导')
        close.setToolTip('关闭授权引导')
        close.setFixedSize(24, 24)
        close.clicked.connect(self.stop)
        body.addWidget(close, 0, Qt.AlignTop)

        c = colors(theme)
        self.setStyleSheet(
            f'QFrame#accessibilityGuide{{background:{c["surface"]};border:1px solid {c["border"]};'
            f'border-radius:{c["radius_card"]}px;}} QLabel{{color:{c["text"]};font-size:12px;}}'
            f'QLabel#guideTitle{{font-size:14px;font-weight:600;}} QLabel#guideHint{{color:{c["muted"]};}}'
            f'QPushButton{{color:{c["text"]};background:{c["control"]};border:0;'
            f'border-radius:{c["radius_control"]}px;}} QPushButton:hover{{background:{c["hover"]};}}'
        )
        self.setFixedSize(self.sizeHint().expandedTo(QSize(330, 70)))
        self._native_panel_ready = False

    def start(self):
        self._deadline = time.monotonic() + 180
        self.timer.start()
        self._follow_settings()

    def stop(self):
        self.timer.stop()
        self.hide()

    def _follow_settings(self):
        if self.app_icon.drag_active:
            return
        try:
            if time.monotonic() >= self._deadline or self.backend.accessibility() == 'allowed':
                self.stop()
                return
            window = settings_window()
        except (ImportError, KeyError, OSError, RuntimeError, TypeError, ValueError):
            LOG.exception('Unable to position Mac permission guide')
            self.stop()
            return
        screen = QGuiApplication.screenAt(window.center()) if not window.isEmpty() else None
        position = guide_geometry(window, screen.availableGeometry(), self.size()) if screen else QRect()
        if position.isEmpty():
            self.hide()
            return
        if self.geometry() != position:
            self.setGeometry(position)
        if not self.isVisible():
            if not self._native_panel_ready:
                self._native_panel_ready = configure_passive_cocoa_panel(self)
            self.show()
            self.raise_()

