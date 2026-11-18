"""Small accessible native theme swatches; custom colour remains available."""
from PySide6.QtCore import QPointF,QRectF,Qt
from PySide6.QtGui import QColor,QPainter,QPen
from PySide6.QtWidgets import QAbstractButton
from theme_accent import contrasting_ink

PRESETS=(('默认黑','#000000'),('蓝色','#0066D6'),('紫色','#7C3AED'),('青色','#0891B2'),
         ('绿色','#16A34A'),('橙色','#EA580C'),('红色','#DC2626'))

class AccentSwatch(QAbstractButton):
    def __init__(self,name,color,parent=None):
        super().__init__(parent);self.color=color;self.dark=False
        self.setCheckable(True);self.setFixedSize(32,32);self.setCursor(Qt.PointingHandCursor);self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(name+'主题色');self.setToolTip(name+' '+color)
    def enterEvent(self,event):self.update();super().enterEvent(event)
    def leaveEvent(self,event):self.update();super().leaveEvent(event)
    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        ring=QColor('#e6e6e6' if self.dark else '#171717')
        if self.isChecked() or self.hasFocus():p.setPen(QPen(ring,1.5));p.setBrush(Qt.NoBrush);p.drawRoundedRect(QRectF(1.5,1.5,29,29),8,8)
        color=QColor(self.color)
        if self.isDown():color=color.darker(120)
        elif self.underMouse():color=color.lighter(112)
        p.setPen(QPen(QColor('#68686d' if self.dark else '#c2c4c8'),.7));p.setBrush(color);p.drawRoundedRect(QRectF(5,5,22,22),5,5)
        if self.isChecked():
            p.setPen(QPen(QColor(contrasting_ink(color)),1.7,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin))
            p.drawLine(QPointF(11,16),QPointF(14.5,19));p.drawLine(QPointF(14.5,19),QPointF(21,12))
        p.end()
