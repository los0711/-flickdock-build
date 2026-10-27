"""In-surface clear confirmation; no secondary native/dialog window.

The overlay is a sibling of the shelf card inside the same native window.
Keeping it outside the card's graphics effect and surface snapshots prevents
those presentation caches from capturing confirmation pixels.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QEventLoop, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (QApplication, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
                               QPushButton, QSizePolicy, QVBoxLayout, QWidget)
from shiboken6 import isValid


MODAL_PROPERTY = "flickdockConfirmationActive"


def confirmation_active(widget: QWidget) -> bool:
    """The owner poll/shortcut entry points can use this lightweight guard."""
    if not isValid(widget):
        return False
    return bool(widget.property(MODAL_PROPERTY) or widget.window().property(MODAL_PROPERTY))


class _SingleLineLabel(QLabel):
    def __init__(self, text: str, parent=None, *, compact_text: str = ""):
        super().__init__(parent)
        self.full_text = text
        self.compact_text = compact_text or text
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(False)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(text)

    def minimumSizeHint(self):
        return QSize(0, self.fontMetrics().height() + 4)

    def sizeHint(self):
        return QSize(self.fontMetrics().horizontalAdvance(self.full_text), self.fontMetrics().height() + 4)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_text()

    def _fit_text(self):
        width = max(0, self.contentsRect().width())
        text = self.full_text if self.fontMetrics().horizontalAdvance(self.full_text) <= width else self.compact_text
        self.setText(self.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, width))


class _PointerConfirmButton(QPushButton):
    """Keyboard defaults cannot accidentally activate the destructive action."""
    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Enter, Qt.Key.Key_Return):
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Enter, Qt.Key.Key_Return):
            event.accept()
            return
        super().keyReleaseEvent(event)


class ClearConfirmOverlay(QWidget):
    """ClearConfirmOverlay(card,count,...).exec() -> bool.

    exec() is a local QEventLoop, not a native modal window. The owner must
    honour confirmation_active(owner) in automatic hide and shortcut handlers.
    The card should be expanded before calling: at 788 logical pixels the full
    one-line explanation fits. A compact card uses a shorter equivalent
    sentence, without taking space away from the buttons.

    open() + finished(bool) is also available for asynchronous callers.
    """
    finished = Signal(bool)

    def __init__(self, card: QWidget, count: int, *, font_family: str | None = None,
                 theme: str = "light"):
        super().__init__(card.window())
        self.setObjectName("clearConfirmOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("确认清空搁架")
        self.owner = card.window()
        self.card = card
        self._owner_alive = self._card_alive = True
        self._focus_timer = QTimer(self)
        self._focus_timer.setSingleShot(True)
        self._focus_timer.timeout.connect(self._focus_cancel)
        self.owner.destroyed.connect(self._owner_destroyed)
        self.card.destroyed.connect(self._card_destroyed)
        self.count = max(0, int(count))
        self.active = False
        self.result = False
        self._loop = None
        self._previous_focus = None
        self._scope_owned = False
        self._theme = "dark" if theme == "dark" else "light"
        family = font_family or card.font().family()

        self.panel = QFrame(self)
        self.panel.setObjectName("clearConfirmPanel")
        shadow = QGraphicsDropShadowEffect(self.panel)
        shadow.setBlurRadius(20)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(10, 23, 42, 82))
        self.panel.setGraphicsEffect(shadow)
        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(28, 25, 28, 24)
        layout.setSpacing(14)
        self.title = QLabel("清空全部内容？", self.panel)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        font = QFont(family)
        font.setPixelSize(18)
        font.setWeight(QFont.Weight.DemiBold)
        self.title.setFont(font)
        layout.addWidget(self.title)

        self.description = _SingleLineLabel(
            f"将移除搁架中的 {self.count} 个条目，磁盘上的原文件会保留。", self.panel,
            compact_text=f"将移除 {self.count} 项，原文件保留。")
        body_font = QFont(family)
        body_font.setPixelSize(13)
        self.description.setFont(body_font)
        layout.addWidget(self.description)
        layout.addSpacing(7)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(12)
        buttons.addStretch(1)
        self.cancel_button = QPushButton("取消", self.panel)
        self.cancel_button.setObjectName("clearCancelButton")
        self.cancel_button.setAccessibleName("取消清空")
        self.cancel_button.setDefault(True)
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.cancel)
        self.delete_button = _PointerConfirmButton("删除", self.panel)
        self.delete_button.setObjectName("clearDeleteButton")
        self.delete_button.setAccessibleName("删除全部搁架内容")
        self.delete_button.setDefault(False)
        self.delete_button.setAutoDefault(False)
        self.delete_button.clicked.connect(lambda: self._finish(True))
        button_font = QFont(family)
        button_font.setPixelSize(13)
        button_font.setWeight(QFont.Weight.Medium)
        for button in (self.cancel_button, self.delete_button):
            button.setFont(button_font)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setMinimumSize(max(112, button.fontMetrics().horizontalAdvance(button.text()) + 40),
                                  max(42, button.fontMetrics().height() + 20))
            button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        QWidget.setTabOrder(self.cancel_button, self.delete_button)
        QWidget.setTabOrder(self.delete_button, self.cancel_button)
        self.apply_theme(self._theme)
        self.hide()
        self._layout_overlay()

    @property
    def minimum_host_size(self):
        width = max(440, self.description.fontMetrics().horizontalAdvance(self.description.full_text) + 104)
        return QSize(width, self.panel.layout().sizeHint().height() + 32)

    def apply_theme(self, theme: str):
        self._theme = "dark" if theme == "dark" else "light"
        dark = self._theme == "dark"
        surface, border = ("#252b35", "#465266") if dark else ("#ffffff", "#dce3ed")
        text, secondary = ("#edf2fa", "#b5c3d8") if dark else ("#263344", "#637186")
        button, hover, pressed = ("#394555", "#46556b", "#51627b") if dark else ("#edf1f6", "#e2eaf4", "#d5e0ef")
        accent = "#8bbfff" if dark else "#1876db"
        self.panel.setStyleSheet(f"QFrame#clearConfirmPanel {{background:{surface};border:1px solid {border};border-radius:18px;}}")
        self.title.setStyleSheet(f"color:{text};background:transparent;border:0;")
        self.description.setStyleSheet(f"color:{secondary};background:transparent;border:0;")
        self.cancel_button.setStyleSheet(
            f"QPushButton {{background:{button};color:{text};border:1px solid {border};border-radius:10px;padding:0 18px;}}"
            f"QPushButton:hover {{background:{hover};}} QPushButton:pressed {{background:{pressed};}}"
            f"QPushButton:focus {{border:2px solid {accent};}}")
        self.delete_button.setStyleSheet(
            "QPushButton {background:#c83e49;color:#ffffff;border:1px solid #b72f3b;border-radius:10px;padding:0 18px;}"
            "QPushButton:hover {background:#bb3440;} QPushButton:pressed {background:#a72b36;}"
            f"QPushButton:focus {{border:2px solid {accent};}}")
        self.update()

    def _layout_overlay(self):
        if not self._owner_alive or not self._card_alive or not isValid(self.owner) or not isValid(self.card):
            return
        self.setGeometry(QRect(self.card.mapTo(self.owner, QPoint()), self.card.size()))
        hint = self.panel.layout().sizeHint()
        preferred = max(480, self.description.fontMetrics().horizontalAdvance(self.description.full_text) + 56)
        width = min(preferred, max(1, self.width() - 36))
        height = min(hint.height(), max(1, self.height() - 24))
        self.panel.setGeometry((self.width() - width) // 2, (self.height() - height) // 2, width, height)
        self.panel.layout().activate()
        self.description._fit_text()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(5, 12, 24, 112 if self._theme == "dark" else 48))
        painter.drawRoundedRect(self.rect(), 24, 24)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_overlay()

    def _within_owner(self, watched):
        return (self._owner_alive and isValid(self.owner) and isinstance(watched, QWidget)
                and (watched is self.owner or self.owner.isAncestorOf(watched)))

    def eventFilter(self, watched, event):
        if not self.active:
            return False
        kind = event.type()
        if watched in (self.owner, self.card) and kind in (QEvent.Type.Move, QEvent.Type.Resize):
            self._layout_overlay()
            self.raise_()
        elif watched is self.owner and kind == QEvent.Type.Paint:
            self._layout_overlay()
            self.raise_()
        elif watched in (self.owner, self.card) and kind in (QEvent.Type.Hide, QEvent.Type.Close):
            self.cancel()
        if kind in (QEvent.Type.KeyPress, QEvent.Type.ShortcutOverride) and self._within_owner(watched):
            if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Enter, Qt.Key.Key_Return):
                event.accept()
                if kind == QEvent.Type.KeyPress:
                    self.cancel()
                return True
            if not (watched is self or self.isAncestorOf(watched)):
                event.accept()
                return True
        return False

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self.panel.geometry().contains(event.position().toPoint()):
            self.cancel()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def open(self) -> bool:
        if (self.active or not self._owner_alive or not self._card_alive
                or not isValid(self.owner) or not isValid(self.card)
                or not self.owner.isVisible() or confirmation_active(self.owner)):
            return False
        self.result = False
        self.active = True
        self._previous_focus = QApplication.focusWidget()
        self.owner.setProperty(MODAL_PROPERTY, True)
        self.card.setProperty(MODAL_PROPERTY, True)
        self._scope_owned = True
        QApplication.instance().installEventFilter(self)
        self._layout_overlay()
        self.show()
        self.raise_()
        self.cancel_button.setFocus(Qt.FocusReason.OtherFocusReason)
        self._focus_timer.start(0)
        return True

    def _focus_cancel(self):
        if isValid(self) and self.active:
            self.cancel_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def exec(self) -> bool:
        loop = QEventLoop()
        self._loop = loop
        self.destroyed.connect(loop.quit)
        try:
            if not self.open():
                return False
            if self.active:
                loop.exec()
            return bool(self.result)
        finally:
            if isValid(self):
                self._loop = None
                if self.active:
                    self._finish(False)
                try:
                    self.destroyed.disconnect(loop.quit)
                except (RuntimeError, TypeError):
                    pass

    def cancel(self):
        self._finish(False)

    def _card_destroyed(self, _card=None):
        self._card_alive = False
        if isValid(self):
            self._finish(False)

    def _owner_destroyed(self, _owner=None):
        self._owner_alive = False
        if isValid(self):
            self._finish(False)

    def _finish(self, accepted: bool):
        if not self.active:
            return
        self._focus_timer.stop()
        self.result = bool(accepted)
        self.active = False
        app = QApplication.instance()
        if app:
            app.removeEventFilter(self)
        if self._scope_owned and self._owner_alive and isValid(self.owner):
            self.owner.setProperty(MODAL_PROPERTY, False)
        if self._scope_owned and self._card_alive and isValid(self.card):
            self.card.setProperty(MODAL_PROPERTY, False)
        self._scope_owned = False
        previous_geometry = QRect(self.geometry())
        self.hide()
        if self._owner_alive and isValid(self.owner):
            self.owner.update(previous_geometry)
        if self._card_alive and isValid(self.card):
            self.card.update()
        if self._loop is not None:
            self._loop.quit()
        previous = self._previous_focus
        self._previous_focus = None
        if previous is not None and isValid(previous) and previous.isVisible() and self._within_owner(previous):
            previous.setFocus(Qt.FocusReason.OtherFocusReason)
        self.finished.emit(self.result)

    def closeEvent(self, event):
        self.cancel()
        event.accept()
