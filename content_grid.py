"""Icon-view items for FlickDock's single content workspace.

The item's data and drag MIME remain owned by the shelf. This view only adds
selection, a larger file icon, and small painted hover/press feedback.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, QDateTime
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QTextLayout, QTextOption
from PySide6.QtWidgets import QMenu, QWidget, QSizePolicy

from flickdock_motion import FeedbackButton, Spring
from transfer_visuals import (CLOUD_STATUS_LABELS, cloud_status_for_ref,
                              draw_cloud_status_badge, draw_progress_badge)
from transfer_visuals import CloudStatusBadge

GRID_ITEM_MIN_WIDTH=136
GRID_ITEM_MAX_WIDTH=176
GRID_ITEM_HEIGHT=168
GRID_ITEM_GAP=10
FILE_ROW_HEIGHT=52


def added_time_for_ref(shelf,ref,*,full=False):
    timestamp=shelf.content.metadata.get(ref,{}).get('shelf_added_at')
    stamp=QDateTime.fromString(str(timestamp or ''),Qt.ISODate)
    return stamp.toLocalTime().toString('yyyy-MM-dd HH:mm:ss' if full else 'MM-dd HH:mm:ss') if stamp.isValid() else None

try:
    from ui_theme import color
except ImportError:
    # Standalone offscreen component tests can run before the app theme loads.
    def color(key: str) -> str:
        return {"text": "#303a48", "secondary": "#7d899a", "hover": "#e6effb",
                "surface": "#ffffff", "border": "#d8e1ed", "icon": "#43546a",
                "accent": "#1876db"}[key]


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
        self.setMinimumWidth(GRID_ITEM_MIN_WIDTH);self.setMaximumWidth(GRID_ITEM_MAX_WIDTH);self.setFixedHeight(GRID_ITEM_HEIGHT)
        self.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed)
        self._keyboard_focus=False
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setAccessibleName(popup.shelf.content.title(ref))
        self.setAccessibleDescription("文字内容" if popup.shelf.content.is_text(ref) else "文件内容")
        self.cloud_status = 'local'
        self.status_badge=CloudStatusBadge(self,glyph_size=14)
        self.status_badge.setFixedSize(24,24)
        self.status_pill=self.status_badge  # Existing status observers share this widget.
        self.refresh_cloud_status()
        self.motion = Spring(self, {"hover": 0, "down": 0, "selected": float(self._selected)},
                             lambda _: self.update(), motion=popup.shelf.manager.motion)
        provider = getattr(popup.shelf, 'item_pixmap', popup.shelf.content.item_pixmap)
        try: self.icon = provider(ref, 64, dpr=self.devicePixelRatioF())
        except TypeError: self.icon = provider(ref, 64)
        self.title = popup.shelf.content.title(ref)
        from reference_widgets import ToolbarButton
        self.remove_button = ToolbarButton("close", self, motion=popup.shelf.manager.motion)
        self.remove_button.setFixedSize(28,28)
        self.remove_button.move(self.width()-32,2)
        self.remove_button.setToolTip("从搁架移除")
        self.remove_button.setAccessibleName("从搁架移除 " + self.title)
        self.remove_button.clicked.connect(lambda: self.popup.remove_path(self.ref))
        self.remove_button.show()

    def refresh_content(self):
        self.title=self.popup.shelf.content.title(self.ref)
        self.setAccessibleName(self.title)
        self.remove_button.setAccessibleName('从搁架移除 '+self.title)
        provider=getattr(self.popup.shelf,'item_pixmap',self.popup.shelf.content.item_pixmap)
        try:self.icon=provider(self.ref,64,dpr=self.devicePixelRatioF())
        except TypeError:self.icon=provider(self.ref,64)
        self.refresh_cloud_status();self.update()

    def refresh_cloud_status(self):
        lookup = getattr(self.popup, 'cloud_status', None)
        status = lookup(self.ref) if callable(lookup) else cloud_status_for_ref(self.popup.shelf, self.ref)
        previous_time=getattr(self,'added_time',None)
        self.added_time=added_time_for_ref(self.popup.shelf,self.ref) or '暂无时间'
        self.status_badge.set_status(status,getattr(self.popup.shelf.content,'transfers',{}).get(self.ref))
        if status == self.cloud_status and self.toolTip():
            if previous_time!=self.added_time:self.update()
            return
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
        if not hasattr(self,'_dwell_hint'):
            from ui_transitions import DwellHint
            self._dwell_hint=DwellHint(self)
        if self.popup.shelf.manager.motion:self._dwell_hint.enter()
        self.remove_button.show()
        if hasattr(self.popup, 'hover_item'): self.popup.hover_item(self.ref)
        else: self.popup.preview.show_path(self.ref)
        self.popup.shelf.touch()
        self.motion.animate({"hover": 1}, response=.14, damping=1)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.remove_button.show()
        if hasattr(self.popup, 'end_hover'): self.popup.end_hover(self.ref)
        self.motion.animate({"hover": 0}, response=.14, damping=1)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self._keyboard_focus=False
        self.popup.shelf.touch()
        if event.button() == Qt.MouseButton.LeftButton:
            self.origin = event.position().toPoint()
            self.did_drag = False
            if QRectF(4,2,28,28).contains(event.position()):
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
        elif event.key() == Qt.Key.Key_Space:
            if hasattr(self.popup,'select_item'):self.popup.select_item(self.ref,toggle=True)
            self._refresh_selection();event.accept()
        elif event.key() == Qt.Key.Key_Delete:
            self.popup.remove_path(self.ref)
            event.accept()
        else:
            super().keyPressEvent(event)

    def resizeEvent(self,event):
        if hasattr(self,'remove_button'):self.remove_button.move(self.width()-32,2)
        if hasattr(self,'status_badge'):self.status_badge.move(self.width()-33,89)
        super().resizeEvent(event)

    def focusInEvent(self,event):
        self._keyboard_focus=event.reason() in (Qt.TabFocusReason,Qt.BacktabFocusReason,Qt.ShortcutFocusReason)
        self.remove_button.show();self.update();super().focusInEvent(event)

    def focusOutEvent(self,event):
        self._keyboard_focus=False;self.remove_button.show();self.update();super().focusOutEvent(event)

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
        viewport=getattr(getattr(self.popup,'scroll',None),'viewport',lambda:None)()
        if viewport is not None and not QRectF(self.mapTo(viewport,QPoint()),self.size()).intersects(QRectF(viewport.rect())):return
        from reference_widgets import palette,ink,thumbnail,draw_status,item_detail
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
        painter.setBrush(ink(c['selected'] if actual else c['card_hover'] if v['hover']>.3 else c['card']))
        painter.setPen(QPen(ink(c['selected_border'] if actual else c['border']),1))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5,.5,-.5,-.5),c['card_radius'],c['card_radius'])
        if self.hasFocus() and self._keyboard_focus:
            painter.setBrush(Qt.NoBrush);painter.setPen(QPen(QColor(c['blue']),1.5))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(2,2,-2,-2),c['card_radius']-1,c['card_radius']-1)
        painter.setPen(QPen(QColor('#0A84FF') if actual else ink(c['input_border']),1));painter.setBrush(QColor('#0A84FF') if actual else Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(12.5,8.5,14,14),4,4)
        if actual:
            painter.setPen(QPen(QColor('white'),1.5,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin));painter.drawLine(QPointF(16,15),QPointF(18,18));painter.drawLine(QPointF(18,18),QPointF(23,12))
        area=QRectF(12,32,self.width()-24,80)
        from PySide6.QtGui import QPainterPath,QImage,QPixmap
        clip=QPainterPath();clip.addRoundedRect(area,6,6);painter.save();painter.setClipPath(clip)
        painter.fillRect(area,QColor(c['preview']))
        cached=getattr(self.popup.shelf,'preview_cache',{}).get(self.ref)
        cache=getattr(self.popup.shelf,'image_cache',None)
        decoded=cache.image(self.ref,256) if cache and self.popup.shelf.content.category(self.ref)=='images' else cached
        art=QPixmap.fromImage(decoded) if isinstance(decoded,QImage) and not decoded.isNull() else decoded if isinstance(decoded,QPixmap) and not decoded.isNull() else thumbnail(self.popup.shelf,self.ref,96)
        scale=min(area.width()/max(1,art.width()),area.height()/max(1,art.height()))
        width,height=art.width()*scale,art.height()*scale
        painter.drawPixmap(QRectF(area.center().x()-width/2,area.center().y()-height/2,width,height),art,QRectF(art.rect()));painter.restore()
        state=getattr(self.popup.shelf.content,'transfers',{}).get(self.ref)
        font=QFont(painter.font());font.setPixelSize(12);font.setWeight(QFont.DemiBold);painter.setFont(font);painter.setPen(QColor(c['text']))
        painter.drawText(QRectF(12,118,self.width()-24,20),Qt.AlignVCenter,painter.fontMetrics().elidedText(self.title.replace('\n',' '),Qt.ElideRight,self.width()-24))
        font.setPixelSize(11);font.setWeight(QFont.Normal);painter.setFont(font);painter.setPen(QColor(c['secondary']))
        from transfer_visuals import transfer_fraction
        fraction=transfer_fraction(state) if state else None
        detail=item_detail(self.popup.shelf,self.ref).split(' · ')[0]+(f' · {round(fraction*100)}%' if fraction is not None else '')
        font.setPixelSize(10);painter.setFont(font)
        time_width=painter.fontMetrics().horizontalAdvance(self.added_time)
        detail_width=max(0,self.width()-24-time_width-6)
        painter.drawText(QRectF(12,140,detail_width,18),Qt.AlignVCenter,painter.fontMetrics().elidedText(detail,Qt.ElideRight,detail_width))
        painter.drawText(QRectF(self.width()-12-time_width,140,time_width,18),Qt.AlignRight|Qt.AlignVCenter,self.added_time)
        painter.end()
