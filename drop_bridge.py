"""Invisible native drop target between the dock strip and its visible shelf."""
import time
from PySide6.QtCore import QRect,Qt
from PySide6.QtGui import QColor,QPainter
from PySide6.QtWidgets import QWidget
from platform_native import keep_tool_visible_when_inactive,raise_edge_target


def bridge_rect(strip,surface,edge):
    strip,surface=QRect(strip),QRect(surface)
    card=surface.adjusted(14,7,-14,-20)
    if edge=='right':return QRect(card.right(),card.top(),max(0,strip.right()-card.right()+1),card.height())
    if edge=='left':return QRect(strip.left(),card.top(),max(0,card.left()-strip.left()+1),card.height())
    if edge=='top':return QRect(card.left(),strip.top(),card.width(),max(0,card.top()-strip.top()+1))
    return QRect(card.left(),card.bottom(),card.width(),max(0,strip.bottom()-card.bottom()+1))


class DropBridge(QWidget):
    def __init__(self,manager):
        super().__init__();self.manager=manager;self.shelf=None;self._entered=False;self._last_drag=0.
        self.setWindowFlags(Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint|Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground);self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAcceptDrops(True);self.setFocusPolicy(Qt.NoFocus);self.setWindowTitle('FlickDock drop bridge')
        keep_tool_visible_when_inactive(self)

    def sync(self,shelf,*,dragging=False):
        self.shelf=shelf
        valid=(shelf and shelf.isVisible() and shelf.presentation_kind=='dock' and shelf.visible_state!='hiding'
               and not self.manager.settings_open() and not self.manager.outgoing_drag)
        if dragging:self._last_drag=time.monotonic()
        if not valid or not (dragging or self._entered or time.monotonic()-self._last_drag<.25):
            self._entered=False;self.hide();return
        rect=bridge_rect(self.manager.edge.activation_region(),shelf.visual_geometry(),self.manager.settings.dock_edge)
        if rect.isEmpty():self.hide();return
        if self.geometry()!=rect:self.setGeometry(rect)
        if not self.isVisible():self.show();raise_edge_target(self)

    def paintEvent(self,event):
        painter=QPainter(self);painter.fillRect(self.rect(),QColor(0,0,0,1));painter.end()

    def dragEnterEvent(self,event):
        if self.shelf and self.shelf.accepts_content_drop(event):
            self._entered=True;self.shelf._highlight_content_drop(True);event.acceptProposedAction()
        else:event.ignore()

    def dragMoveEvent(self,event):self.dragEnterEvent(event)

    def dragLeaveEvent(self,event):
        self._entered=False
        if self.shelf:self.shelf._highlight_content_drop(False)
        event.accept()

    def dropEvent(self,event):
        self._entered=False
        if self.shelf:
            self.shelf._highlight_content_drop(False)
            if self.shelf.receive_mime('permanent',event.mimeData()):event.acceptProposedAction();return
        event.ignore()
