"""Small, nonmodal application window for an available FlickDock update."""
from __future__ import annotations
import sys

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication
from PySide6.QtWidgets import (QFrame, QGraphicsDropShadowEffect, QHBoxLayout,
                               QLabel, QPushButton, QVBoxLayout, QWidget)
from native_ui_common import colors as ui_colors, reference_font


class UpdateNotice(QWidget):
    """Present one update offer without a system notification or focus steal."""

    update_requested = Signal()

    def __init__(self, parent=None, *, theme="light"):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("flickdockUpdateNotice")
        self.setWindowTitle("FlickDock · 新版本")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(452, 178)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16,16,16,16)
        self.surface = QFrame(self)
        self.surface.setObjectName("updateNoticeSurface")
        outer.addWidget(self.surface)
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
        self.close_button.setAutoDefault(False);self.close_button.clicked.connect(self.hide)
        header.addWidget(self.close_button)
        content.addLayout(header)

        actions = QHBoxLayout()
        actions.setSpacing(9)
        actions.addStretch(1)
        self.later_button = QPushButton("稍后", self.surface)
        self.later_button.setObjectName("updateNoticeLater")
        self.later_button.setFixedSize(56,28)
        self.later_button.setAutoDefault(False)
        self.later_button.setAccessibleName("稍后更新")
        self.later_button.clicked.connect(self.hide)
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

    def show_version(self, version):
        version = str(version).strip()[:32]
        if not version:
            return
        self.version_label.setText(f"v{version} 已发布")
        self.title.setText(f"发现新版本 FlickDock v{version}")
        self.update_button.setAccessibleName(f"立即更新到 {version}")
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(area.right() - self.width() - 18, area.bottom() - self.height() - 18)
        self._appearance.stop()
        if not self.isVisible() and QGuiApplication.platformName() not in ("offscreen", "minimal"):
            self.setWindowOpacity(0.0)
            self.show()
            self._appearance.setStartValue(0.0)
            self._appearance.setEndValue(1.0)
            self._appearance.start()
        else:
            self.setWindowOpacity(1.0)
            self.show()
        self.raise_()
