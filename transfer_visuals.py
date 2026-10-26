"""Lightweight progress artwork; repainting never rebuilds shelf layouts."""
import time
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QLabel, QWidget
import ui_theme


def draw_ring(painter,rect,state):
    if not state: return
    rect=QRectF(rect).adjusted(3,3,-3,-3)
    painter.save();painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen=QPen(QColor(ui_theme.color('border')),2.4);painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush);painter.drawEllipse(rect)
    pen.setColor(QColor('#3989ef'));pen.setCapStyle(Qt.PenCapStyle.RoundCap);painter.setPen(pen)
    received,total=state.get('received',0),state.get('total',0)
    if total>0:
        angle=-max(.015,min(1,received/total))*5760
        painter.drawArc(rect,90*16,round(angle))
    else:
        start=round((time.monotonic()*240%360)*16)
        painter.drawArc(rect,start,-100*16)
    painter.restore()


class TransferIconLabel(QLabel):
    def __init__(self,repository,ref,parent=None):
        super().__init__(parent);self.repository,self.ref=repository,ref

    def paintEvent(self,event):
        state=self.repository.transfers.get(self.ref)
        if not state: return super().paintEvent(event)
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        pixmap=self.pixmap()
        if pixmap and not pixmap.isNull():
            painter.setOpacity(.38);painter.drawPixmap(self.rect(),pixmap);painter.setOpacity(1)
        draw_ring(painter,self.rect(),state)


class TransferOverlay(QWidget):
    def __init__(self,parent,state):
        super().__init__(parent);self.state=state
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.hide()

    def paintEvent(self,event):
        state=self.state()
        if not state:return
        painter=QPainter(self)
        side=min(98,self.width()*.65,self.height()*.75)
        rect=QRectF((self.width()-side)/2,(self.height()-side)/2,side,side)
        painter.setPen(Qt.PenStyle.NoPen);painter.setBrush(QColor(0,0,0,55))
        painter.drawRoundedRect(rect,14,14)
        draw_ring(painter,rect.adjusted(7,7,-7,-7),state)
