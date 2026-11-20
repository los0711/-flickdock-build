"""Screen-edge trigger presentation and pointer/drop entry."""
from __future__ import annotations

from PySide6.QtCore import QRect
from PySide6.QtCore import QRectF
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtGui import QGuiApplication
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QWidget
from dock_geometry import trigger_rect
from flickdock_motion import Spring
from platform_native import keep_tool_visible_when_inactive
from platform_native import raise_edge_target
import sys


class DockHandle(QWidget):
    """One fully hittable activation strip per connected display."""
    def __init__(self,manager,screen=None):
        super().__init__()
        self.manager=manager
        self.display=screen or QGuiApplication.primaryScreen()
        self.setWindowTitle('FlickDock edge')
        self.setWindowFlags(Qt.WindowType.Tool|Qt.WindowType.FramelessWindowHint|Qt.WindowType.WindowStaysOnTopHint|Qt.WindowType.WindowDoesNotAcceptFocus)
        keep_tool_visible_when_inactive(self)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAcceptDrops(True); self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.appearance=Spring(self,{'alpha':0},lambda _:self.update(),motion=manager.motion)
        self.position_at_edge()

    def screen_area(self):
        return self.display.geometry() if self.values().get('_full_screen_edge') else self.display.availableGeometry()

    def values(self):
        from display_triggers import display_values
        return display_values(self.manager.settings,self.display,getattr(self.manager,'_dock_preview_values',None))

    def activation_region(self):
        if not self.active():return QRect()
        region=QRect(self.geometry())
        if sys.platform=='darwin':
            screen=self.display.geometry()
            edge=self.values()['dock_edge']
            if edge=='right':region.setRight(screen.right()+1)
            elif edge=='left':region.setLeft(screen.left()-1)
            elif edge=='top':region.setTop(screen.top()-1)
            else:region.setBottom(screen.bottom()+1)
        return region

    def file_preview_region(self):
        if sys.platform=='darwin':
            return QRect(self.geometry()) if self.active() else QRect()
        return self.activation_region()

    def position_at_edge(self):
        values=self.values()
        self.setGeometry(trigger_rect(self.screen_area(),values['dock_edge'],values['dock_position'],values['dock_length'],values['dock_thickness']))
        self.update()  # Color/opacity changes need paint even when geometry stays identical.

    def show_handle(self):
        if not self.active():self.appearance.stop();self.hide();return
        self.position_at_edge()
        if not self.isVisible():
            self.appearance.jump({'alpha':0}); self.show()
            if sys.platform!='darwin':self.raise_()
            self.repaint()
            self.appearance.animate({'alpha':1},response=.18,damping=1)
        raise_edge_target(self)

    def active(self):
        from display_triggers import trigger_active
        return trigger_active(self.values())

    def paintEvent(self,event):
        painter=QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(),QColor(0,0,0,1))
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        values=self.values()
        if values['trigger_visible']:
            tint=QColor(values['dock_color']);tint.setAlpha(round(255*values['dock_opacity']/100*self.appearance.values['alpha']))
            painter.setBrush(tint);painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5,.5,-.5,-.5),3,3)
        painter.end()

    def enterEvent(self,event):
        if not self.active():super().enterEvent(event);return
        self.manager.edge=self
        magnetic=getattr(self.manager,'edge_file_drag',None)
        if not self.manager.edge_suppressed:
            if not (magnetic and magnetic.prepared):
                self.manager.show_docked()
            elif (getattr(magnetic,'coexisting_preview',False) and
                  self.file_preview_region().contains(event.globalPosition().toPoint())):
                self.manager.show_docked()
        super().enterEvent(event)

    def _magnetic_event(self,event,*,dropping=False):
        magnetic=getattr(self.manager,'edge_file_drag',None)
        if not magnetic or not magnetic.prepared:return False
        from edge_file_drag import drop_position,file_urls
        position=drop_position(self,event)
        if not file_urls(event.mimeData()) or magnetic.visual.target_at(position) is None:return False
        # A native re-order can briefly route an event to the original narrow
        # handle. Keep one intake owner instead of revealing a second target.
        if dropping:magnetic._native_drop(event,position)
        else:magnetic._native_move(event,position,entering=True)
        if not dropping and getattr(magnetic,'coexisting_preview',False) and self.file_preview_region().contains(position):
            self.manager.show_docked()
        return True

    def dragEnterEvent(self,event):
        self.manager.edge=self
        if self._magnetic_event(event):return
        if not self.active():event.ignore();return
        if self.manager.shelf.accepts_content_drop(event):
            event.setDropAction(Qt.DropAction.CopyAction); event.accept()
            self.manager.show_docked()
        else: event.ignore()

    def dropEvent(self,event):
        if self._magnetic_event(event,dropping=True):return
        if not self.active():event.ignore();return
        if self.manager.shelf.accepts_content_drop(event) and self.manager.shelf.receive_mime('permanent',event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction); event.accept()
        else: event.ignore()
