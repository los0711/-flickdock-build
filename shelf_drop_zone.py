"""Compact shelf drop target and stacked-card pointer feedback."""
from __future__ import annotations

from PySide6.QtCore import QPoint
from PySide6.QtCore import QTimer
from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QFrame
from PySide6.QtWidgets import QLabel
from flickdock_icons import glyph_pixmap
from flickdock_motion import Spring
import ui_theme


class DropZone(QFrame):
    def __init__(self,shelf,mode):
        super().__init__(shelf.card)
        from reference_widgets import MiniStack,label,css
        self.shelf,self.mode=shelf,mode;self.origin=QPoint();self.did_drag=False;self.drop_highlight=False
        self._click_armed=False;self._pointer_hovered=False
        self.setObjectName('dropZone');self.setAcceptDrops(True);self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor);self.setFixedSize(330,268);self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName('搁架，拖入内容或打开总览')
        self.symbol=QLabel(self);self.symbol.hide();self.title=QLabel(self);self.title.hide();self.description=QLabel(self);self.description.hide()
        self.icon=MiniStack(shelf,self,motion=shelf.manager.motion);self.icon.setGeometry(45,18,240,170)
        self.hint=label('拖入文件、文字或链接',12,400,'secondary',self);self.hint.setGeometry(40,194,250,18);self.hint.setAlignment(Qt.AlignCenter)
        self.caption=QLabel(self);self.caption.setGeometry(131,230,68,32);self.caption.setAlignment(Qt.AlignCenter);self.caption.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.count_arrow=QLabel(self);self.count_arrow.setGeometry(180,240,12,12);self.count_arrow.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._pill_hovered=False;self._pill_timer=QTimer(self);self._pill_timer.setSingleShot(True);self._pill_timer.timeout.connect(self._open_from_pill_hover)
        self.feedback=Spring(self,{'hover':0},lambda _:self.icon.update(),motion=shelf.manager.motion)
        self._style_caption(False);self._apply_style()
        from clipboard_preview import ClipboardButton
        self.clipboard_button=ClipboardButton(shelf.manager,self);self.clipboard_button.move(200,230)
        from shelf_capacity import ShelfCapacityButton
        self.capacity_button=ShelfCapacityButton(self);self.capacity_button.move(160,229)

    @property
    def paths(self):
        return self.shelf.display_refs()

    def _style_caption(self,hovered):
        from reference_widgets import css
        css(self.caption,"color:#F5F5F7;font:600 12px 'HarmonyOS Sans SC';background:rgba(255,255,255,28);border:0;border-radius:8px;padding-right:12px;",
            "color:#1D1D1F;font:600 12px 'HarmonyOS Sans SC';background:rgba(0,0,0,15);border:0;border-radius:8px;padding-right:12px;")

    def _apply_style(self):
        from reference_widgets import css
        css(self,'QFrame#dropZone{background:transparent;border:1px solid transparent;border-radius:8px;}')
        self.icon.update()

    def _open_from_pill_hover(self):
        # Hover only reveals feedback. Opening the workspace requires a click.
        return

    def _track_pill_hover(self, position, buttons=Qt.MouseButton.NoButton):
        hovering = (self.caption.geometry().adjusted(-4, -4, 4, 4).contains(position)
                    and not buttons and not self.drop_highlight)
        if hovering == self._pill_hovered:
            return
        self._pill_hovered = hovering
        self._style_caption(hovering)
        self._pill_timer.stop()

    def refresh(self):
        self._apply_style()
        self.clipboard_button.refresh()
        self.count_arrow.setPixmap(glyph_pixmap('chevron',14,ui_theme.color('secondary'),dpr=self.devicePixelRatioF()))
        self.icon.set_stack(self.shelf.content,self.paths,self.shelf.preview_cache)
        if not self.paths:
            self.icon.set_content("empty")
            self.caption.setText("0 项")
            return
        self.caption.setText(f"{len(self.paths)} 项")

    def pulse(self, kind="drop", finished=None):
        self.icon.pulse(kind, finished)

    def dragEnterEvent(self, event):
        self._click_armed=False
        if self.shelf.accepts_content_drop(event):
            self._pill_hovered = False
            self._pill_timer.stop()
            self._style_caption(False)
            self.shelf.active_mode = self.mode
            self.drop_highlight = True
            self.icon.set_hover(True)
            self._apply_style()
            self.shelf.touch()
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self.shelf.accepts_content_drop(event):
            self.shelf.touch()
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.drop_highlight = False
        self.icon.set_hover(False)
        self._apply_style()

    def dropEvent(self, event):
        self.drop_highlight = False
        self.icon.set_hover(False)
        if self.shelf.accepts_content_drop(event) and self.shelf.receive_mime(self.mode, event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()
        self._apply_style()

    def enterEvent(self, event):
        self.shelf.touch()
        self._pointer_hovered=True;self._apply_style()
        self.feedback.animate({"hover": 1.0}, response=.14, damping=1.0)
        self.icon.set_hover(self.drop_highlight or self.icon.geometry().contains(event.position().toPoint()))
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._pointer_hovered=False;self._apply_style()
        self._pill_hovered = False
        self._pill_timer.stop()
        self._style_caption(False)
        self.feedback.animate({"hover": 0.0}, response=.14, damping=1.0)
        self.icon.set_hover(False)
        super().leaveEvent(event)

    def showEvent(self,event):
        super().showEvent(event)
        # Cocoa can reveal a tool window beneath a stationary pointer without
        # delivering Enter. Reconcile once after layout, without a polling loop.
        QTimer.singleShot(0,self,self._reconcile_card_hover)

    def _reconcile_card_hover(self):
        if not self.isVisible():return
        cursor=QCursor.pos();point=self.mapFromGlobal(cursor);hit=QApplication.widgetAt(cursor)
        inside=self.rect().contains(point) and (hit is None or hit.window() is self.window())
        if inside!=self._pointer_hovered:
            self._pointer_hovered=inside;self._apply_style()
            self.feedback.animate({'hover':float(inside)},response=.14,damping=1.0)
        self.icon.set_hover(self.drop_highlight or inside and self.icon.geometry().contains(point))

    def hideEvent(self, event):
        self._click_armed=False;self._pointer_hovered=False;self._apply_style()
        self._pill_hovered = False
        self._pill_timer.stop()
        self._style_caption(False)
        super().hideEvent(event)

    def mousePressEvent(self, event):
        if self.shelf.intake_blocks_overview():
            self._click_armed=False;event.accept();return
        self.shelf.touch()
        self.shelf.active_mode = self.mode
        if event.button() == Qt.MouseButton.LeftButton:
            self._click_armed=True
            self.origin = event.position().toPoint()
            self.did_drag = False
            self.icon.set_pressed(True)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        self.shelf.touch()
        self._track_pill_hover(event.position().toPoint(), event.buttons())
        self.icon.set_hover(self.drop_highlight or self.icon.geometry().contains(event.position().toPoint()))
        if not self.shelf.intake_blocks_overview() and self._click_armed and event.buttons() & Qt.MouseButton.LeftButton and self.paths and not self.did_drag and (event.position().toPoint() - self.origin).manhattanLength() > 8:
            self.did_drag = True
            self.icon.set_pressed(False)
            self.shelf.drag_files(self.mode, list(self.paths))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.shelf.touch()
        self.icon.set_pressed(False)
        armed,self._click_armed=self._click_armed,False
        if event.button() == Qt.MouseButton.LeftButton and armed and not self.did_drag and not self.shelf.incoming_drop:
            self.shelf.show_contents(self.mode) if self.paths else self.shelf.pick_files(self.mode)
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        self.shelf.show_menu(self.mode, event.globalPos())
        event.accept()

    def keyPressEvent(self,event):
        if event.key() in (Qt.Key_Return,Qt.Key_Enter,Qt.Key_Space):
            self.shelf.show_contents(self.mode);event.accept();return
        super().keyPressEvent(event)
