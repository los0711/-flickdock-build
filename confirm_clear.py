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
from native_ui_common import colors as ui_colors
from flickdock_icons import draw_glyph


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


class _TrashMark(QLabel):
    """Vector equivalent of the reference's trash emoji on Qt font backends."""
    def paintEvent(self,event):
        super().paintEvent(event)
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        draw_glyph(painter,'trash',self.rect().adjusted(12,12,-12,-12),'#B8BAC2',1.8)


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
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)
        self.symbol=_TrashMark('',self.panel)
        self.symbol.setObjectName('clearConfirmSymbol')
        self.symbol.setFixedSize(44,44);self.symbol.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.symbol,0,Qt.AlignmentFlag.AlignHCenter)
        text_group=QWidget(self.panel)
        text_layout=QVBoxLayout(text_group);text_layout.setContentsMargins(0,0,0,0);text_layout.setSpacing(4)
        self.title = QLabel(f"清空全部 {self.count} 个项目？", text_group)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        font = QFont(family)
        font.setPixelSize(14)
        font.setWeight(QFont.Weight.Bold)
        self.title.setFont(font)
        text_layout.addWidget(self.title)

        self.description = _SingleLineLabel(
            "外部原文件将保留，仅移除搁架记录与受管副本。", text_group,
            compact_text="原文件保留，仅移除记录与受管副本。")
        body_font = QFont(family)
        body_font.setPixelSize(12)
        self.description.setFont(body_font)
        self.description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text_layout.addWidget(self.description)
        layout.addWidget(text_group)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(10)
        self.cancel_button = QPushButton("取消", self.panel)
        self.cancel_button.setObjectName("clearCancelButton")
        self.cancel_button.setAccessibleName("取消清空")
        self.cancel_button.setDefault(True)
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.cancel)
        self.delete_button = _PointerConfirmButton("清空", self.panel)
        self.delete_button.setObjectName("clearDeleteButton")
        self.delete_button.setAccessibleName("删除全部搁架内容")
        self.delete_button.setDefault(False)
        self.delete_button.setAutoDefault(False)
        self.delete_button.clicked.connect(lambda: self._finish(True))
        button_font = QFont(family)
        button_font.setPixelSize(12)
        button_font.setWeight(QFont.Weight.Medium)
        for button in (self.cancel_button, self.delete_button):
            button.setFont(button_font)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setMinimumSize(button.fontMetrics().horizontalAdvance(button.text()) + 32,32)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.setFixedHeight(32)
            buttons.addWidget(button,1)
        layout.addLayout(buttons)
        QWidget.setTabOrder(self.cancel_button, self.delete_button)
        QWidget.setTabOrder(self.delete_button, self.cancel_button)
        self.apply_theme(self._theme)
        self.hide()
        self._layout_overlay()

    @property
    def minimum_host_size(self):
        return QSize(376, self.panel.layout().sizeHint().height() + 32)

    def apply_theme(self, theme: str):
        self._theme = "dark" if theme == "dark" else "light"
        c=ui_colors(self._theme)
        text,secondary=c['text'],c['secondary']
        self.panel.setStyleSheet(f"QFrame#clearConfirmPanel {{background:{c['surface']};border:1px solid {c['glass_border']};border-radius:{c['radius']}px;}}")
        self.symbol.setStyleSheet(f"background:rgba(255,69,58,38);color:{c['red']};border:0;border-radius:16px;font-size:20px;")
        self.title.setStyleSheet(f"color:{text};background:transparent;border:0;")
        self.description.setStyleSheet(f"color:{secondary};background:transparent;border:0;")
        self.cancel_button.setStyleSheet(
            f"QPushButton {{background:{c['control']};color:{text};border:1px solid transparent;border-radius:12px;padding:0 12px;}}"
            f"QPushButton:hover {{background:{c['hover']};}} QPushButton:pressed {{background:{c['selected']};}}")
        self.delete_button.setStyleSheet(
            f"QPushButton {{background:{c['red']};color:#ffffff;border:1px solid transparent;border-radius:12px;padding:0 12px;font-weight:700;}}"
            "QPushButton:hover {background:rgba(255,69,58,230);} QPushButton:pressed {background:rgba(255,69,58,210);}")
        self.panel.graphicsEffect().setBlurRadius(32)
        self.panel.graphicsEffect().setColor(QColor(0,0,0,140 if self._theme=='dark' else 41))
        self.update()

    def _layout_overlay(self):
        if not self._owner_alive or not self._card_alive or not isValid(self.owner) or not isValid(self.card):
            return
        self.setGeometry(QRect(self.card.mapTo(self.owner, QPoint()), self.card.size()))
        hint = self.panel.layout().sizeHint()
        preferred = 340
        width = min(preferred, max(1, self.width() - 36))
        height = min(hint.height(), max(1, self.height() - 24))
        self.panel.setGeometry((self.width() - width) // 2, (self.height() - height) // 2, width, height)
        self.panel.layout().activate()
        self.description._fit_text()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0,0,0,128))
        radius=ui_colors(self._theme)['radius']
        painter.drawRoundedRect(self.rect(), radius, radius)

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
