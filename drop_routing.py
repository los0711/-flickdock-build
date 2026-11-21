"""Route overview drops to the shelf and keep feedback inside the destination."""
from PySide6.QtCore import QObject, QEvent, QRectF, Qt
from PySide6.QtGui import QPainter, QPainterPath
from PySide6.QtWidgets import QApplication, QWidget

from native_ui_common import DropBorderMotion, RADIUS_CONTROL_PX, colors, qss_color
import ui_theme


class _FileAreaDrop(QWidget):
    def __init__(self,parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.motion=DropBorderMotion(self)
        self.motion.animation.finished.connect(self._hide_if_idle)
        self.motion.landing.finished.connect(self._hide_if_idle)
        parent.installEventFilter(self)
        self.hide()

    def eventFilter(self,watched,event):
        if watched is self.parentWidget() and event.type()==QEvent.Resize:
            self.setGeometry(watched.rect())
        return super().eventFilter(watched,event)

    def track_drag(self,global_position):
        parent=self.parentWidget()
        if not self.isVisible():
            self.setGeometry(parent.rect())
            self.show();self.raise_()
        point=self.mapFromGlobal(global_position)
        point.setX(max(0,min(point.x(),self.width()-1)))
        point.setY(max(0,min(point.y(),self.height()-1)))
        self.motion.move_drag(point)

    def end_drag(self,*,accepted=False):
        self.motion.end_drag(accepted=accepted)
        self._hide_if_idle()

    def _hide_if_idle(self):
        if not (self.motion.dragging or self.motion.progress or self.motion.landing_progress):
            self.hide()

    def paintEvent(self,event):
        gain=max(self.motion.progress,self.motion.landing_progress)
        if gain<=0.:return
        bounds=QRectF(self.rect()).adjusted(8,3,-8,-5)
        if bounds.isEmpty():return
        theme=ui_theme.current_theme()
        fill=qss_color(colors(theme)['card'])
        fill.setAlpha(51)
        path=QPainterPath();path.addRoundedRect(bounds,RADIUS_CONTROL_PX,RADIUS_CONTROL_PX)
        painter=QPainter(self);painter.setOpacity(gain*.82)
        self.motion.paint(painter,path,dark=theme=='dark',radius=RADIUS_CONTROL_PX,idle_fill=fill,quiet=False)
        painter.end()


class OverviewDropRouter(QObject):
    def __init__(self,popup):
        super().__init__(popup)
        self.popup=popup
        self.file_area=_FileAreaDrop(popup.scroll.viewport())
        for target in [popup,*popup.findChildren(QWidget)]:
            if target is not self.file_area:target.setAcceptDrops(True)
        QApplication.instance().installEventFilter(self)

    def eventFilter(self,watched,event):
        kind=event.type()
        if kind not in (QEvent.DragEnter,QEvent.DragMove,QEvent.DragLeave,QEvent.Drop):return False
        if not isinstance(watched,QWidget) or not (watched is self.popup or self.popup.isAncestorOf(watched)):return False
        entry=getattr(self.popup,'transfer_entry',None)
        if entry is not None and entry.eventFilter(watched,event):
            self.file_area.end_drag()
            return True
        shelf=self.popup.shelf
        target=self.popup.empty_drop
        if kind in (QEvent.DragEnter,QEvent.DragMove):
            accepts=shelf.accepts_content_drop(event)
            point=watched.mapToGlobal(event.position().toPoint())
            if accepts and target.isVisible():
                self.file_area.end_drag()
                local=target.mapFromGlobal(point)
                target.set_drag_active(True,local if target.rect().contains(local) else target.rect().center())
            else:
                target.set_drag_active(False)
                if accepts and self.popup.scroll.isVisible():self.file_area.track_drag(point)
                else:self.file_area.end_drag()
            if kind==QEvent.DragEnter:shelf.dragEnterEvent(event,origin=watched)
            else:shelf.dragMoveEvent(event,origin=watched)
        elif kind==QEvent.DragLeave:
            target.set_drag_active(False);self.file_area.end_drag();shelf.dragLeaveEvent(event)
        else:
            shelf.dropEvent(event,origin=watched)
            accepted=event.isAccepted()
            target.set_drag_active(False,accepted=accepted)
            self.file_area.end_drag(accepted=accepted)
        return True
