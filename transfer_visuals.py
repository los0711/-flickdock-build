"""Lightweight progress artwork; repainting never rebuilds shelf layouts."""
import time
from PySide6.QtCore import QRectF, Qt, QTimer
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


def draw_progress_badge(painter,rect,state):
    if not state:return
    rect=QRectF(rect)
    painter.save();painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(ui_theme.color('border')),0.8))
    painter.setBrush(QColor(ui_theme.color('surface')))
    painter.drawEllipse(rect)
    painter.restore()
    draw_ring(painter,rect,state)


class TransferIconLabel(QLabel):
    def __init__(self,repository,ref,parent=None):
        super().__init__(parent);self.repository,self.ref=repository,ref

    def paintEvent(self,event):
        state=self.repository.transfers.get(self.ref)
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        pixmap=self.pixmap()
        if pixmap and not pixmap.isNull():
            painter.drawPixmap(0,0,28,28,pixmap)
        if state:draw_progress_badge(painter,QRectF(self.width()-15,self.height()-15,14,14),state)


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
        side=30
        rect=QRectF(self.width()-side-14,self.height()-side-14,side,side)
        draw_progress_badge(painter,rect,state)


class UploadCountBadge(QWidget):
    """Compact header feedback for outstanding uploads."""
    def __init__(self,parent=None):
        super().__init__(parent)
        self.count=0
        self.complete=False
        self.setFixedSize(26,26)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self._finish_timer=QTimer(self)
        self._finish_timer.setSingleShot(True)
        self._finish_timer.timeout.connect(self.hide)
        self.hide()

    def set_count(self,count):
        count=max(0,int(count))
        if count==self.count:return
        previous=self.count
        self.count=count
        self._finish_timer.stop()
        self.complete=previous>0 and count==0
        if count or self.complete:
            self.show();self.raise_();self.update()
        else:self.hide()
        if self.complete:self._finish_timer.start(1400)

    def paintEvent(self,event):
        painter=QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#22a46f' if self.complete else '#e4f1ff'))
        painter.drawEllipse(QRectF(1,1,24,24))
        if self.complete:
            painter.setPen(QPen(QColor('white'),2.2,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
            painter.drawLine(7,13,11,17)
            painter.drawLine(11,17,19,9)
        else:
            painter.setPen(QColor('#146bc8'))
            font=painter.font();font.setPointSize(9);font.setBold(True);painter.setFont(font)
            painter.drawText(self.rect(),Qt.AlignmentFlag.AlignCenter,'99+' if self.count>99 else str(self.count))
