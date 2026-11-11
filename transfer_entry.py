"""A child-surface drop chooser; never a second native popup or shelf import."""
from PySide6.QtCore import QObject,QEvent,QPoint,QRect,QRectF,Qt,QTimer,QEasingCurve,Signal
from PySide6.QtGui import QColor,QPainter,QPainterPath
from PySide6.QtWidgets import QApplication,QWidget
from shiboken6 import isValid
from native_ui_common import reduced_motion,icon
from display_frames import DisplayAnimation

TRANSFER_DROP_FEEDBACK_MS=160


def original_paths(mime):
    # The transfer service validates files off the entry path. Never resolve,
    # copy or stat a potentially disconnected network path during drag motion.
    if not mime.hasUrls():return []
    return list(dict.fromkeys(url.toLocalFile() for url in mime.urls()
                             if url.isLocalFile() and url.toLocalFile()))


class TransferDropSurface(QWidget):
    def __init__(self,parent):
        super().__init__(parent);self.active=None;self.press=0.;self.setAcceptDrops(True)
        self.setAttribute(Qt.WA_ShowWithoutActivating);self.hide()
        from native_ui_common import DropBorderMotion
        self.border_motions={mode:DropBorderMotion(self) for mode in ('lan','wan')}

    def set_target(self,target):
        self.active=target
        for mode,motion in self.border_motions.items():motion.set_active(mode==target)

    def target(self,point):
        if not self.rect().contains(point):return None
        return 'lan' if point.y()<self.height()/2 else 'wan'

    def paintEvent(self,event):
        from reference_widgets import palette
        c=palette();p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        for i,(mode,label,glyph) in enumerate((('lan','局域网传输','nearby'),('wan','远程传输','web'))):
            selected=self.active==mode
            rect=QRectF(3,3+i*52,self.width()-6,48)
            if selected and self.press:rect=rect.adjusted(self.press*3,self.press*2,-self.press*3,-self.press*2)
            p.setBrush(QColor(c['surface']));p.setPen(Qt.NoPen);p.drawRoundedRect(rect,8,8)
            path=QPainterPath();path.addRoundedRect(rect.adjusted(1,1,-1,-1),7,7)
            from ui_theme import current_theme
            self.border_motions[mode].paint(p,path,dark=current_theme()=='dark')
            motion=self.border_motions[mode]
            icon(glyph,motion.color(c['secondary'],c['link']).name()).paint(p,QRect(16,19+i*52,16,16))
            p.setPen(motion.color(c['secondary'],c['text']))
            p.drawText(rect.adjusted(40,0,-6,0),Qt.AlignVCenter|Qt.AlignLeft,label)
        p.end()


class TransferEntryRouter(QObject):
    active_changed=Signal(bool)
    def __init__(self,root,button,deliver):
        super().__init__(root);self.root=root;self.button=button;self.deliver=deliver
        self.surface=TransferDropSurface(root);self._dropping=False
        self.leave=QTimer(self);self.leave.setSingleShot(True);self.leave.timeout.connect(self.reset)
        self.feedback=DisplayAnimation(self);self.feedback.setDuration(TRANSFER_DROP_FEEDBACK_MS)
        self.feedback.setStartValue(1.);self.feedback.setEndValue(0.)
        self.feedback.setEasingCurve(QEasingCurve.OutCubic)
        self.feedback.valueChanged.connect(self._press);self.feedback.finished.connect(self.reset)
        button.setAcceptDrops(True)
        # Do not install another application-wide Python event filter. The
        # overview's existing drop router delegates only drag events here.
        for target in {root,button,self.surface,button.parentWidget()}:
            if target is not None:target.installEventFilter(self)

    def _press(self,value):self.surface.press=float(value);self.surface.update()

    def reset(self):
        was_visible=self.surface.isVisible()
        self.leave.stop();self.feedback.stop();self.surface.hide();self.surface.set_target(None)
        self.surface.press=0.;self._dropping=False
        if was_visible:self.active_changed.emit(False)

    def _show(self):
        origin=self.button.mapTo(self.root,QPoint())
        width=max(174,self.button.width())
        x=max(4,min(origin.x(),self.root.width()-width-4))
        y=max(4,min(origin.y()-108,self.root.height()-108))
        self.surface.setGeometry(x,y,width,108);self.surface.show();self.surface.raise_()
        self.active_changed.emit(True)

    def eventFilter(self,watched,event):
        if not isValid(self.root):return False
        kind=event.type()
        if watched is self.root and kind in (QEvent.Hide,QEvent.Close):self.reset();return False
        if kind not in (QEvent.DragEnter,QEvent.DragMove,QEvent.DragLeave,QEvent.Drop):return False
        if not isinstance(watched,QWidget) or not (watched is self.root or self.root.isAncestorOf(watched)):return False
        if kind==QEvent.DragLeave:
            if self.surface.isVisible() and not self._dropping:self.leave.start(60)
            return False
        if not self.button.isVisible() or not original_paths(event.mimeData()):return False
        point=watched.mapTo(self.root,event.position().toPoint())
        entry=QRect(self.button.mapTo(self.root,QPoint()),self.button.size()).adjusted(-8,-8,8,8)
        inside=entry.contains(point) or self.surface.isVisible() and self.surface.geometry().contains(point)
        if not inside:
            if self.surface.isVisible():self.reset()
            return False
        self.leave.stop()
        if not self.surface.isVisible():self._show()
        self.surface.set_target(self.surface.target(self.surface.mapFrom(self.root,point)))
        if kind==QEvent.Drop:
            mode=self.surface.active
            if mode is None:self.reset();event.ignore();return True
            paths=original_paths(event.mimeData());self._dropping=True
            if not reduced_motion():self.feedback.start()
            else:self.reset()
            event.acceptProposedAction();self.deliver(mode,paths);return True
        event.acceptProposedAction();return True
