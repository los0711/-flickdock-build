"""A child-surface drop chooser; never a second native popup or shelf import."""
from PySide6.QtCore import QObject,QEvent,QPoint,QRect,QRectF,Qt,QTimer,QVariantAnimation,QEasingCurve
from PySide6.QtGui import QColor,QPainter,QPen
from PySide6.QtWidgets import QApplication,QWidget
from shiboken6 import isValid
from native_ui_common import reduced_motion,icon


def original_paths(mime):
    # The transfer service validates files off the entry path. Never resolve,
    # copy or stat a potentially disconnected network path during drag motion.
    if not mime.hasUrls():return []
    return list(dict.fromkeys(url.toLocalFile() for url in mime.urls()
                             if url.isLocalFile() and url.toLocalFile()))


class TransferDropSurface(QWidget):
    def __init__(self,parent):
        super().__init__(parent);self.active=None;self.press=0.;self.enter=1.;self.setAcceptDrops(True)
        self.setAttribute(Qt.WA_ShowWithoutActivating);self.hide()

    def target(self,point):
        if not self.rect().contains(point):return None
        return next((mode for index,mode in enumerate(('lan','wan')) if QRectF(3,3+index*68,self.width()-6,64).contains(point)),None)

    def paintEvent(self,event):
        from reference_widgets import palette,ink
        c=palette();p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(self.enter);p.translate(self.width()/2,self.height());scale=.97+.03*self.enter;p.scale(scale,scale);p.translate(-self.width()/2,-self.height())
        for i,(mode,label,glyph) in enumerate((('lan','局域网传输','nearby'),('wan','远程传输','web'))):
            selected=self.active==mode
            rect=QRectF(3,3+i*68,self.width()-6,64)
            if selected and self.press:rect=rect.adjusted(self.press*3,self.press*2,-self.press*3,-self.press*2)
            p.setBrush(QColor(c['surface']));p.setPen(QPen(QColor('#56565C' if c['text']=='#F5F5F7' else '#B7BDC6'),1));p.drawRoundedRect(rect,8,8)
            if selected:
                fill=QColor(c['link']);fill.setAlpha(24);p.setBrush(fill)
                pen=QPen(QColor(c['link']),1.4,Qt.DashLine);pen.setDashPattern([4,3]);p.setPen(pen)
                p.drawRoundedRect(rect.adjusted(1,1,-1,-1),7,7)
            icon(glyph,c['link'] if selected else c['secondary']).paint(p,QRect(16,27+i*68,16,16))
            p.setPen(QColor(c['text'] if selected else c['secondary']))
            p.drawText(rect.adjusted(40,0,-6,0),Qt.AlignVCenter|Qt.AlignLeft,label)
        p.end()


class TransferEntryRouter(QObject):
    def __init__(self,root,button,deliver):
        super().__init__(root);self.root=root;self.button=button;self.deliver=deliver
        self.surface=TransferDropSurface(root);self._dropping=False
        self._pending_present=None
        from ui_transitions import exit_curve
        self.entrance=QVariantAnimation(self);self.entrance.setDuration(160);self.entrance.setEasingCurve(exit_curve())
        self.entrance.valueChanged.connect(self._enter)
        self.leave=QTimer(self);self.leave.setSingleShot(True);self.leave.timeout.connect(self.reset)
        self.feedback=QVariantAnimation(self);self.feedback.setDuration(160)
        self.feedback.setStartValue(1.);self.feedback.setEndValue(0.)
        self.feedback.setEasingCurve(exit_curve())
        self.feedback.valueChanged.connect(self._press);self.feedback.finished.connect(self._finish_drop)
        button.setAcceptDrops(True)
        # Do not install another application-wide Python event filter. The
        # overview's existing drop router delegates only drag events here.
        for target in {root,button,self.surface,button.parentWidget()}:
            if target is not None:target.installEventFilter(self)

    def _press(self,value):self.surface.press=float(value);self.surface.update()
    def _enter(self,value):self.surface.enter=float(value);self.surface.update()

    def _highlight(self,enabled):
        self.button.setProperty('transferDropActive',bool(enabled))
        self.button.style().unpolish(self.button);self.button.style().polish(self.button);self.button.update()

    def _finish_drop(self):
        callback=self._pending_present;self._pending_present=None;self.reset()
        if callable(callback):callback()

    def reset(self):
        self.leave.stop();self.feedback.stop();self.entrance.stop();self._pending_present=None
        self.surface.hide();self.surface.active=None;self._highlight(False)
        self.surface.press=0.;self._dropping=False

    def _show(self):
        origin=self.button.mapTo(self.root,QPoint())
        width=max(174,self.button.width())
        x=max(4,min(origin.x(),self.root.width()-width-4))
        y=max(4,min(origin.y()-138,self.root.height()-138))
        self.surface.setGeometry(x,y,width,138);self.surface.show();self.surface.raise_();self._highlight(True)
        if reduced_motion():self._enter(1.)
        else:self.entrance.setStartValue(0.);self.entrance.setEndValue(1.);self.entrance.start()

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
        self.surface.active=self.surface.target(self.surface.mapFrom(self.root,point));self.surface.update()
        if kind==QEvent.Drop:
            mode=self.surface.active
            if mode is None:self.reset();event.ignore();return True
            paths=original_paths(event.mimeData());self._dropping=True
            event.acceptProposedAction();self._pending_present=self.deliver(mode,paths)
            if not reduced_motion():self.entrance.stop();self._enter(1.);self.feedback.start()
            else:self._finish_drop()
            return True
        event.acceptProposedAction();return True
