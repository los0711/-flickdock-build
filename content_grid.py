"""Icon-view items for FlickDock's single content workspace.

The item's data and drag MIME remain owned by the shelf. This view only adds
selection, a larger file icon, and small painted hover/press feedback.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QTextLayout, QTextOption
from PySide6.QtWidgets import QMenu, QWidget

from flickdock_motion import FeedbackButton, Spring
from transfer_visuals import (CLOUD_STATUS_LABELS, cloud_status_for_ref,
                              draw_cloud_status_badge, draw_progress_badge)

try:
    from ui_theme import color
except ImportError:
    # Standalone offscreen component tests can run before the app theme loads.
    def color(key: str) -> str:
        return {"text": "#303a48", "secondary": "#7d899a", "hover": "#e6effb",
                "surface": "#ffffff", "border": "#d8e1ed", "icon": "#43546a"}[key]


class IconItem(QWidget):
    """112 × 116 icon cell; ``popup.selected`` is the selection authority.

    Root may call ``set_selected(bool)`` after selection changes elsewhere.
    The method updates presentation only and never changes the stored set.
    """
    def __init__(self, popup, ref: str):
        super().__init__(popup.rows_body)
        self.popup = popup
        self.setAcceptDrops(True)
        self.ref = self.path = ref
        self.origin = QPoint()
        self.did_drag = False
        self._selected = ref in popup.selected
        self.setFixedSize(112, 116)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setAccessibleName(popup.shelf.content.title(ref))
        self.setAccessibleDescription("文字内容" if popup.shelf.content.is_text(ref) else "文件内容")
        self.cloud_status = 'local'
        self.refresh_cloud_status()
        self.motion = Spring(self, {"hover": 0, "down": 0, "selected": float(self._selected)},
                             lambda _: self.update(), motion=popup.shelf.manager.motion)
        provider = getattr(popup.shelf, 'item_pixmap', popup.shelf.content.item_pixmap)
        try: self.icon = provider(ref, 64, dpr=self.devicePixelRatioF())
        except TypeError: self.icon = provider(ref, 64)
        self.title = popup.shelf.content.title(ref)
        self.remove_button = FeedbackButton("close", self, motion=popup.shelf.manager.motion)
        self.remove_button.setFixedSize(28, 28)
        self.remove_button.move(82, 2)
        self.remove_button.setToolTip("从搁架移除")
        self.remove_button.setAccessibleName("从搁架移除 " + self.title)
        self.remove_button.clicked.connect(lambda: self.popup.remove_path(self.ref))
        self.remove_button.hide()

    def refresh_cloud_status(self):
        lookup = getattr(self.popup, 'cloud_status', None)
        status = lookup(self.ref) if callable(lookup) else cloud_status_for_ref(self.popup.shelf, self.ref)
        if status == self.cloud_status and self.toolTip(): return
        self.cloud_status = status
        self.setToolTip(CLOUD_STATUS_LABELS.get(status, CLOUD_STATUS_LABELS['local']))
        kind='文字内容' if self.popup.shelf.content.is_text(self.ref) else '文件内容'
        self.setAccessibleDescription(f'{kind}；{self.toolTip()}')
        self.update()

    def set_selected(self, value: bool):
        self._selected = bool(value)
        self.motion.animate({"selected": float(value)}, response=.17, damping=1)

    def _refresh_selection(self):
        for item in self.popup.rows_body.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly):
            if getattr(item, "popup", None) is not self.popup or not hasattr(item, "path"):
                continue
            if hasattr(item, "set_selected"):
                item.set_selected(item.path in self.popup.selected)
            else:
                item.update()

    def _chosen(self) -> list[str]:
        chosen = [ref for ref in self.popup.shelf.files[self.popup.mode] if ref in self.popup.selected]
        return chosen or [self.ref]

    def enterEvent(self, event):
        self.remove_button.show()
        if hasattr(self.popup, 'hover_item'): self.popup.hover_item(self.ref)
        else: self.popup.preview.show_path(self.ref)
        self.popup.shelf.touch()
        self.motion.animate({"hover": 1}, response=.14, damping=1)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.remove_button.hide()
        if hasattr(self.popup, 'end_hover'): self.popup.end_hover(self.ref)
        self.motion.animate({"hover": 0}, response=.14, damping=1)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self.popup.shelf.touch()
        if event.button() == Qt.MouseButton.LeftButton:
            self.origin = event.position().toPoint()
            self.did_drag = False
            self._pending_deselect=(self.ref in self.popup.selected and not event.modifiers() &
                (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier))
            if hasattr(self.popup, 'select_item') and not self._pending_deselect:
                self.popup.select_item(self.ref, bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier),bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
            elif event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                if self.ref in self.popup.selected:
                    self.popup.selected.remove(self.ref)
                else:
                    self.popup.selected.add(self.ref)
            elif self.ref not in self.popup.selected:
                self.popup.selected = {self.ref}
            self._refresh_selection()
            if hasattr(self.popup,'restore_selected_preview'): self.popup.restore_selected_preview()
            else:self.popup.preview.show_path(getattr(self.popup, 'primary_selection', None) or self.ref)
            self.motion.animate({"down": 1}, response=.12, damping=1)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and not self.did_drag and (event.position().toPoint() - self.origin).manhattanLength() > 8:
            self.did_drag = True
            self.motion.animate({"down": 0}, response=.18, damping=.9)
            self.popup.shelf.drag_files(self.popup.mode, self._chosen())
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            QTimer.singleShot(0, self.popup.rebuild)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button()==Qt.MouseButton.LeftButton and not self.did_drag and getattr(self,'_pending_deselect',False):
            if hasattr(self.popup,'select_item'):self.popup.select_item(self.ref,toggle=True)
            else:self.popup.selected.discard(self.ref);self._refresh_selection()
        self._pending_deselect=False
        self.motion.animate({"down": 0}, response=.20, damping=.8)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.popup.shelf.open_path(self.ref)
            self.popup.shelf.touch()
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_C and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.popup.shelf.copy_items(self._chosen())
            event.accept()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.popup.shelf.open_path(self.ref)
            event.accept()
        elif event.key() == Qt.Key.Key_Delete:
            self.popup.remove_path(self.ref)
            event.accept()
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        if hasattr(self.popup,'show_item_menu'):
            self.popup.show_item_menu(self.ref,event.globalPos());event.accept();return
        menu = QMenu(self)
        menu.setStyleSheet(f"QMenu {{background:{color('surface')};color:{color('text')};border:1px solid {color('border')};border-radius:10px;padding:5px;}} QMenu::item {{padding:8px 16px;border-radius:6px;}} QMenu::item:selected {{background:{color('hover')};}}")
        menu.addAction("复制文字" if self.popup.shelf.content.is_text(self.ref) else "复制文件",
                       lambda: self.popup.shelf.copy_items(self._chosen()))
        menu.addAction("从搁架移除", lambda: self.popup.remove_path(self.ref))
        was_open = getattr(self.popup.shelf, '_menu_open', False)
        self.popup.shelf._menu_open = True
        try:
            menu.exec(event.globalPos())
        finally:
            self.popup.shelf._menu_open = was_open
            self.popup.shelf.touch()
            menu.deleteLater()
        event.accept()

    @staticmethod
    def _draw_title(painter: QPainter, rect: QRectF, title: str):
        font = QFont(painter.font())
        font.setPixelSize(14)
        painter.setFont(font)
        title = title.replace('\r', '').replace('\n', ' ')
        metrics = painter.fontMetrics()
        chunk = metrics.elidedText(title, Qt.TextElideMode.ElideRight, round(rect.width()))
        painter.drawText(QPointF(rect.center().x()-metrics.horizontalAdvance(chunk)/2, rect.top()+metrics.ascent()), chunk)

    def paintEvent(self, event):
        actual = self.ref in self.popup.selected
        if actual != self._selected:
            self.set_selected(actual)
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        v = self.motion.values
        scale = 1 - .025 * max(0, min(1, v['down']))
        center = self.rect().center()
        painter.translate(center.x(), center.y())
        painter.scale(scale, scale)
        painter.translate(-center.x(), -center.y())
        mix = max(.86 * v['selected'], .56 * v['hover'])
        fill = QColor(color('hover'))
        fill.setAlphaF(max(0, min(1, mix)))
        painter.setBrush(fill)
        border = QColor('#6aa8ee')
        border.setAlphaF(max(0, min(.8, .8 * v['selected'])))
        painter.setPen(QPen(border, 1))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 12, 12)
        if self.hasFocus() and not actual:
            painter.setPen(QPen(QColor('#6aa8ee'), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(2, 2, -2, -2), 11, 11)
        state=getattr(self.popup.shelf.content,'transfers',{}).get(self.ref)
        icon_rect=QRectF((self.width()-64)/2,13,64,64)
        painter.drawPixmap(icon_rect,self.icon,QRectF(self.icon.rect()))
        status_rect=QRectF(icon_rect.right()-12,icon_rect.bottom()-17,20,20)
        draw_cloud_status_badge(painter,status_rect,self.cloud_status,
                                None if state and state.get('phase') in ('copying','downloading') else state)
        if state and state.get('phase') in ('copying','downloading'):
            draw_progress_badge(painter,QRectF(icon_rect.left()-5,icon_rect.bottom()-17,18,18),state)
        painter.setPen(QColor(color('text')))
        self._draw_title(painter, QRectF(6, 85, self.width() - 12, 38), self.title)
        painter.end()
