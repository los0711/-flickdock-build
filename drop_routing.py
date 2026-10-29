"""Route drops over every overview surface to the shelf's content importer."""
from PySide6.QtCore import QObject,QEvent
from PySide6.QtWidgets import QApplication,QWidget

class OverviewDropRouter(QObject):
    def __init__(self,popup):
        super().__init__(popup)
        self.popup=popup
        for target in [popup,*popup.findChildren(QWidget)]:target.setAcceptDrops(True)
        QApplication.instance().installEventFilter(self)

    def eventFilter(self,watched,event):
        kind=event.type()
        if kind not in (QEvent.Type.DragEnter,QEvent.Type.DragMove,QEvent.Type.DragLeave,QEvent.Type.Drop):return False
        if not isinstance(watched,QWidget) or not (watched is self.popup or self.popup.isAncestorOf(watched)):return False
        shelf=self.popup.shelf
        target=getattr(self.popup,'empty_drop',None)
        if target is not None and target.isVisible():
            target._active=(kind in (QEvent.Type.DragEnter,QEvent.Type.DragMove)
                            and shelf.accepts_content_drop(event))
            if target._active:target._ants_timer.start()
            else:target._ants_timer.stop()
            target.update()
        if kind==QEvent.Type.DragEnter:shelf.dragEnterEvent(event)
        elif kind==QEvent.Type.DragMove:shelf.dragMoveEvent(event)
        elif kind==QEvent.Type.DragLeave:shelf.dragLeaveEvent(event)
        else:shelf.dropEvent(event)
        return True
