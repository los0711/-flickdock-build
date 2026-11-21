"""Shared interactive monitor preview, independent of settings storage."""
from copy import deepcopy
from PySide6.QtCore import QEvent,QRectF,Qt,Signal
from PySide6.QtGui import QColor,QPainter,QPen
from PySide6.QtWidgets import QWidget,QPushButton
from magnetic_settings import EDGES,LABELS,default_edges,region_rect

class MonitorDiagram(QWidget):
    edge_clicked=Signal(str)
    def __init__(self,parent=None):
        super().__init__(parent);self.setFixedHeight(180);self.setMinimumWidth(240)
        self.rows=default_edges();self.enabled=True;self.selected='left';self.dark=False
        self.buttons={};self.hover_edge=None
        for edge in EDGES:
            button=QPushButton(self);button.setFlat(True);button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet('QPushButton{background:transparent;border:0;}')
            button.clicked.connect(lambda _,edge=edge:self.edge_clicked.emit(edge))
            button.installEventFilter(self)
            self.buttons[edge]=button

    def eventFilter(self,watched,event):
        edge=next((key for key,button in self.buttons.items() if button is watched),None)
        if event.type()==QEvent.Enter:self.hover_edge=edge;self.update()
        elif event.type()==QEvent.Leave:self.hover_edge=None;self.update()
        elif event.type() in (QEvent.MouseButtonPress,QEvent.MouseButtonRelease,QEvent.FocusIn,QEvent.FocusOut):self.update()
        return super().eventFilter(watched,event)

    def frame(self):
        width=min(280,self.width()-60);height=min(138,self.height()-40)
        return QRectF((self.width()-width)/2,12,width,height)

    def set_values(self,rows,enabled,selected):
        self.rows=deepcopy(rows);self.enabled=enabled;self.selected=selected
        for edge,button in self.buttons.items():
            button.setAccessibleName(LABELS[edge]+('磁吸已开启，点击关闭' if rows[edge]['enabled'] else '磁吸已关闭，点击开启'))
        self.update()

    def resizeEvent(self,event):
        super().resizeEvent(event);r=self.frame()
        for edge,button in self.buttons.items():
            if edge in ('left','right'):
                rect=QRectF(r.left()-12 if edge=='left' else r.right()-12,r.top()+12,24,r.height()-24)
            else:rect=QRectF(r.left()+12,r.top()-12 if edge=='top' else r.bottom()-12,r.width()-24,24)
            button.setGeometry(rect.toAlignedRect())

    def paintEvent(self,event):
        # This preview represents the blue magnetic field, independently from
        # the theme-coloured controls. It never owns a native animation host.
        from native_ui_common import colors as ui_colors
        c=ui_colors('dark' if self.dark else 'light')
        accent=QColor(c['brand_accent'])
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing);r=self.frame()
        border=QColor('#4A4E59' if self.dark else '#C2C6D1')
        p.setPen(QPen(border,1.5));p.setBrush(QColor(c['card']))
        p.drawRoundedRect(r,14,14)
        p.setPen(QPen(border,2));p.drawLine(r.center().x(),r.bottom()+2,r.center().x(),r.bottom()+14)
        p.drawLine(r.center().x()-27,r.bottom()+15,r.center().x()+27,r.bottom()+15)
        virtual=r.adjusted(4,4,-4,-4).toRect()
        for edge,row in self.rows.items():
            region=region_rect(virtual,edge,{**row,'width':max(3,round(row['width']*.10))})
            enabled=self.enabled and row['enabled']
            color=QColor(accent if enabled else border);color.setAlpha(38 if enabled else 10)
            p.fillRect(region,color)
            color=QColor(accent if enabled else border);color.setAlpha(255 if enabled else 80)
            if self.hover_edge==edge or self.buttons[edge].hasFocus():color.setAlpha(255);color=color.lighter(115)
            if self.buttons[edge].isDown():color=color.darker(115)
            p.setPen(QPen(color,5 if self.hover_edge==edge else 4 if enabled else 2,Qt.SolidLine,Qt.RoundCap))
            if edge in ('left','right'):
                x=r.left() if edge=='left' else r.right()
                p.drawLine(x,region.top(),x,region.bottom())
            else:
                y=r.top() if edge=='top' else r.bottom()
                p.drawLine(region.left(),y,region.right(),y)
        p.end()


