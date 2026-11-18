"""Icon-view items for FlickDock's single content workspace.

The item's data and drag MIME remain owned by the shelf. This view only adds
selection, a larger file icon, and small painted hover/press feedback.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QTextLayout, QTextOption
from PySide6.QtWidgets import QMenu, QWidget, QSizePolicy

from flickdock_motion import FeedbackButton, Spring
from native_ui_common import set_ui_weight
from transfer_visuals import (CLOUD_STATUS_LABELS, cloud_status_for_ref, file_presence_label,
                              draw_cloud_status_badge, draw_progress_badge, thumbnail_transfer_progress_visible)

try:
    from ui_theme import color
except ImportError:
    # Standalone offscreen component tests can run before the app theme loads.
    def color(key: str) -> str:
        return {"text": "#303a48", "secondary": "#7d899a", "hover": "#e6effb",
                "surface": "#ffffff", "border": "#d8e1ed", "icon": "#43546a",
                "accent": "#1876db"}[key]


GRID_CARD_HEIGHT_PX=136
GRID_COLUMN_WIDTH_PX=174
GRID_CARD_MIN_WIDTH_PX=GRID_COLUMN_WIDTH_PX


class IconItem(QWidget):
    """Reference grid cell; ``popup.selected`` is the selection authority.

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
        self.setFixedSize(GRID_COLUMN_WIDTH_PX,GRID_CARD_HEIGHT_PX)
        self.setSizePolicy(QSizePolicy.Fixed,QSizePolicy.Fixed)
        self._keyboard_focus=False
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setAccessibleName(popup.shelf.content.title(ref))
        self.setAccessibleDescription("文字内容" if popup.shelf.content.is_text(ref) else "文件内容")
        self.cloud_status = 'local'
        self.refresh_cloud_status()
        self.motion = Spring(self, {"hover": 0, "down": 0},
                             lambda _: self.update(), motion=popup.shelf.manager.motion)
        # Artwork is requested by paintEvent only when the card is visible.
        # Resolving an unused second native icon here could block category
        # transitions on shell/file IO.
        self.title = popup.shelf.content.title(ref)
        from reference_widgets import ToolbarButton
        self.remove_button = ToolbarButton("close", self, motion=popup.shelf.manager.motion)
        self.remove_button.setFixedSize(32,32)
        self.remove_button.move(self.width()-36,4)
        self.remove_button.setToolTip("从搁架移除")
        self.remove_button.setAccessibleName("从搁架移除 " + self.title)
        self.remove_button.clicked.connect(lambda: self.popup.remove_path(self.ref))
        self.remove_button.hide()
        self.pin_button=ToolbarButton('star',self,motion=popup.shelf.manager.motion);self.pin_button.setFixedSize(24,24);self.pin_button.setProperty('glyphSize',14)
        self.pin_button.clicked.connect(lambda:self.popup.toggle_pin(self.ref));self.pin_button.hide()
        self.refresh_pin()
        self.refresh_upload_time()

    def refresh_pin(self):
        pinned=getattr(self.popup,'is_pinned',lambda ref:False)(self.ref)
        self.pin_button.set_glyph('star-filled' if pinned else 'star');self.pin_button.set_glyph_tint('#E9AF20' if pinned else None)
        self.pin_button.setToolTip('取消星标置顶' if pinned else '星标置顶');self.pin_button.setAccessibleName(self.pin_button.toolTip()+' '+self.title)
        self._refresh_actions();self.update()

    def _refresh_actions(self):
        hovered=self.underMouse();focused=self.hasFocus() and self._keyboard_focus
        self.remove_button.setVisible(self._selected or hovered or focused or self.remove_button.hasFocus())
        if hasattr(self,'pin_button'):
            self.pin_button.setVisible(getattr(self.popup,'is_pinned',lambda ref:False)(self.ref) or hovered or focused or self.pin_button.hasFocus())
            self.pin_button.move((self.width()-52)//2+40,4)

    def refresh_upload_time(self):
        from reference_widgets import item_detail
        self._detail_text=item_detail(self.popup.shelf,self.ref)
        caption=getattr(self.popup,'upload_caption',None)
        from shelf_content import shelf_time_info
        label,stamp=shelf_time_info(self.popup.shelf.content,self.ref,compact=True)
        self._upload_text=(caption(self.ref,compact=True) if callable(caption) else
                           ('记录于 '+stamp if label=='记录时间' else stamp))
        self.update()

    def refresh_content(self):
        from reference_widgets import invalidate_thumbnail
        invalidate_thumbnail(self.popup.shelf,self.ref)
        self.title=self.popup.shelf.content.title(self.ref)
        self.setAccessibleName(self.title)
        self.remove_button.setAccessibleName('从搁架移除 '+self.title)
        self.refresh_cloud_status();self.refresh_upload_time();self.refresh_pin();self.update()

    def refresh_cloud_status(self):
        lookup = getattr(self.popup, 'cloud_status', None)
        status = lookup(self.ref) if callable(lookup) else cloud_status_for_ref(self.popup.shelf, self.ref)
        if status == self.cloud_status and self.toolTip(): return
        self.cloud_status = status
        self.setToolTip(file_presence_label(status))
        kind='文字内容' if self.popup.shelf.content.is_text(self.ref) else '文件内容'
        self.setAccessibleDescription(f'{kind}；{self.toolTip()}')
        self.update()

    def set_selected(self, value: bool):
        value=bool(value)
        if self._selected!=value:
            self._selected=value;self.remove_button.set_glyph_tint(None)
            self.remove_button.setProperty('inverseOnSelection',False);self.update()
        self._refresh_actions()

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
        if not hasattr(self,'_dwell_hint'):
            from ui_transitions import DwellHint
            self._dwell_hint=DwellHint(self)
        if self.popup.shelf.manager.motion:self._dwell_hint.enter()
        self.remove_button.show();self.pin_button.show()
        self.popup.shelf.touch()
        self.motion.animate({"hover": 1}, response=.14, damping=1)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._refresh_actions()
        self.motion.animate({"hover": 0}, response=.14, damping=1)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self._keyboard_focus=False
        self.popup.shelf.touch()
        if event.button() == Qt.MouseButton.LeftButton:
            self.origin = event.position().toPoint()
            self.did_drag = False
            if QRectF(4,4,32,32).contains(event.position()):
                self.popup.select_item(self.ref,toggle=True)
                self._pending_deselect=False
                self._refresh_selection();event.accept();return
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
            event.accept();return
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
        if event.button()==Qt.LeftButton:event.accept();return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.mousePressEvent(event);return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_C and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.popup.shelf.copy_items(self._chosen())
            event.accept()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.popup.select_item(self.ref,toggle=True)
            event.accept()
        elif event.key() == Qt.Key.Key_Space:
            if hasattr(self.popup,'select_item'):self.popup.select_item(self.ref,toggle=True)
            self._refresh_selection();event.accept()
        elif event.key() == Qt.Key.Key_Delete:
            self.popup.remove_path(self.ref)
            event.accept()
        else:
            super().keyPressEvent(event)

    def resizeEvent(self,event):
        if hasattr(self,'remove_button'):self.remove_button.move(self.width()-36,4)
        if hasattr(self,'pin_button'):self.pin_button.move((self.width()-52)//2+40,4)
        super().resizeEvent(event)

    def focusInEvent(self,event):
        self._keyboard_focus=event.reason() in (Qt.TabFocusReason,Qt.BacktabFocusReason,Qt.ShortcutFocusReason)
        self._refresh_actions();self.update();super().focusInEvent(event)

    def focusOutEvent(self,event):
        self._keyboard_focus=False;self._refresh_actions();self.update();super().focusOutEvent(event)

    def contextMenuEvent(self, event):
        if hasattr(self.popup,'show_item_menu'):
            self.popup.show_item_menu(self.ref,event.globalPos());event.accept();return
        from shelf_ui_common import RoundedMenu
        from native_ui_common import rounded_popup,RADIUS_CARD_PX
        menu = RoundedMenu(self);menu.selection_inset=(0,0);rounded_popup(menu)
        menu.setStyleSheet(f"QMenu {{background:transparent;color:{color('text')};border:0;padding:6px;}} QMenu::item {{padding:8px 16px;background:transparent;border:0;}} QMenu::item:selected {{background:transparent;}}")
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
        viewport=getattr(getattr(self.popup,'scroll',None),'viewport',lambda:None)()
        if viewport is not None and not QRectF(self.mapTo(viewport,QPoint()),self.size()).intersects(QRectF(viewport.rect())):return
        from reference_widgets import palette,ink,thumbnail,draw_status
        from native_ui_common import paint_rounded_surface
        actual = self.ref in self.popup.selected
        if actual != self._selected:
            self.set_selected(actual)
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        v=self.motion.values;c=palette()
        scale=1-.025*max(0,min(1,v['down']))
        center = self.rect().center()
        painter.translate(center.x(), center.y())
        painter.scale(scale, scale)
        painter.translate(-center.x(), -center.y())
        focused=self.hasFocus() and self._keyboard_focus
        fill=ink(c['grid_selected'] if actual else c['file_card_hover'] if v['hover']>.3 or focused else c['file_card'])
        paint_rounded_surface(painter,self.rect(),c['card_radius'],fill,ink(c['grid_selected_border'] if actual else c['border']))
        checkbox_border=c['grid_unchecked_hover_border'] if v['hover']>.3 or v['down']>.1 else c['grid_unchecked_border']
        paint_rounded_surface(painter,QRectF(12.5,12.5,14,14),4,
                              QColor(c['action']) if actual else QColor(Qt.transparent),
                              None if actual else ink(checkbox_border))
        if actual:
            painter.setPen(QPen(QColor(c['on_action']),1.8,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin));painter.drawLine(QPointF(16,19),QPointF(18,22));painter.drawLine(QPointF(18,22),QPointF(23,16))
        art=thumbnail(self.popup.shelf,self.ref,52,self.devicePixelRatioF());x=(self.width()-52)//2
        lift=2*getattr(getattr(self,'_dwell_hint',None),'value',0.)
        state=getattr(self.popup.shelf.content,'transfers',{}).get(self.ref)
        painter.drawPixmap(QPointF(x,11-lift),art);draw_status(painter,QRectF(x+34,45-lift,18,18),self.cloud_status,state)
        if thumbnail_transfer_progress_visible(state):
            draw_progress_badge(painter,QRectF(x+55,46,16,16),state)
        font=QFont(painter.font());font.setPixelSize(12);set_ui_weight(font,600);painter.setFont(font);painter.setPen(QColor(c['grid_selected_text'] if actual else c['text']))
        painter.drawText(QRectF(12,68,self.width()-24,18),Qt.AlignCenter,painter.fontMetrics().elidedText(self.title.replace('\n',' '),Qt.ElideRight,self.width()-24))
        font.setPixelSize(10);set_ui_weight(font,400);painter.setFont(font);painter.setPen(QColor(c['file_description']))
        painter.drawText(QRectF(10,90,self.width()-20,16),Qt.AlignCenter,painter.fontMetrics().elidedText(self._detail_text,Qt.ElideRight,self.width()-20))
        from native_ui_common import technical_font
        painter.setFont(technical_font(font))
        painter.drawText(QRectF(10,108,self.width()-20,16),Qt.AlignCenter,painter.fontMetrics().elidedText(self._upload_text,Qt.ElideRight,self.width()-20))
        painter.end()
