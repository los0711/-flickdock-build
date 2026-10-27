"""Small, nonmodal application window for an available FlickDock update."""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication
from PySide6.QtWidgets import (QFrame, QGraphicsDropShadowEffect, QHBoxLayout,
                               QLabel, QPushButton, QVBoxLayout, QWidget)


class UpdateNotice(QWidget):
    """Present one update offer without a system notification or focus steal."""

    update_requested = Signal()

    def __init__(self, parent=None, *, theme="light"):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("flickdockUpdateNotice")
        self.setWindowTitle("FlickDock · 新版本")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(376, 164)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 14, 18)
        self.surface = QFrame(self)
        self.surface.setObjectName("updateNoticeSurface")
        outer.addWidget(self.surface)
        shadow = QGraphicsDropShadowEffect(self.surface)
        shadow.setBlurRadius(23)
        shadow.setOffset(0, 5)
        shadow.setColor(QColor(20, 34, 58, 52))
        self.surface.setGraphicsEffect(shadow)

        content = QVBoxLayout(self.surface)
        content.setContentsMargins(19, 15, 19, 16)
        content.setSpacing(5)
        self.title = QLabel("FlickDock 有新版本", self.surface)
        self.title.setObjectName("updateNoticeTitle")
        content.addWidget(self.title)
        self.version_label = QLabel(self.surface)
        self.version_label.setObjectName("updateNoticeVersion")
        self.version_label.setTextFormat(Qt.TextFormat.PlainText)
        content.addWidget(self.version_label)
        content.addStretch(1)

        actions = QHBoxLayout()
        actions.setSpacing(9)
        actions.addStretch(1)
        self.later_button = QPushButton("稍后", self.surface)
        self.later_button.setObjectName("updateNoticeLater")
        self.later_button.setFixedSize(72, 34)
        self.later_button.setAccessibleName("稍后更新")
        self.later_button.clicked.connect(self.hide)
        actions.addWidget(self.later_button)
        self.update_button = QPushButton("立即更新", self.surface)
        self.update_button.setObjectName("updateNoticePrimary")
        self.update_button.setFixedSize(94, 34)
        self.update_button.clicked.connect(self.update_requested.emit)
        actions.addWidget(self.update_button)
        content.addLayout(actions)

        self._appearance = QPropertyAnimation(self, b"windowOpacity", self)
        self._appearance.setDuration(170)
        self._appearance.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.apply_theme(theme)

    def apply_theme(self, theme):
        dark = theme == "dark"
        surface = "#252b35" if dark else "#ffffff"
        ink = "#edf2fa" if dark else "#263344"
        secondary = "#acb9cc" if dark else "#637186"
        edge = "#465266" if dark else "#dce3ed"
        hover = "#334966" if dark else "#eaf0f7"
        accent = "#276ab6" if dark else "#1876db"
        accent_hover = "#2c70bd" if dark else "#176cc8"
        self.setStyleSheet(f"""
            QFrame#updateNoticeSurface {{ background:{surface}; border:1px solid {edge}; border-radius:17px; }}
            QLabel#updateNoticeTitle {{ color:{ink}; border:0; background:transparent;
                font-family:'HarmonyOS Sans SC'; font-size:15px; font-weight:600; }}
            QLabel#updateNoticeVersion {{ color:{secondary}; border:0; background:transparent;
                font-family:'HarmonyOS Sans SC'; font-size:13px; }}
            QPushButton {{ border:1px solid {edge}; border-radius:9px; background:transparent;
                color:{ink}; font-family:'HarmonyOS Sans SC'; font-size:13px; }}
            QPushButton:hover {{ background:{hover}; }}
            QPushButton:pressed {{ background:{edge}; }}
            QPushButton:focus {{ border:1px solid {edge}; }}
            QPushButton#updateNoticePrimary {{ background:{accent}; border-color:{accent}; color:white; font-weight:600; }}
            QPushButton#updateNoticePrimary:hover {{ background:{accent_hover}; border-color:{accent_hover}; }}
        """)

    def show_version(self, version):
        version = str(version).strip()[:32]
        if not version:
            return
        self.version_label.setText(f"v{version} 已发布")
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
