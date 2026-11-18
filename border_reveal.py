"""Event-driven monochrome light on existing native-painted outlines only.

No overlay windows, blur passes or idle polling. A spatial index limits each
pointer update to nearby surfaces; the existing AA border path is reused.
"""
import math
import weakref
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QPen, QRadialGradient
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid

RADIUS=92.
CELL=160


def outline_light_color(theme):
    return QColor(255,255,255,155) if theme=='dark' else QColor(0,0,0,174)


def border_distance(rect,point):
    x,y=point.x(),point.y()
    if rect.contains(point):return min(x-rect.left(),rect.right()-x,y-rect.top(),rect.bottom()-y)
    return math.hypot(max(rect.left()-x,0,x-rect.right()),max(rect.top()-y,0,y-rect.bottom()))


class BorderReveal(QObject):
    def __init__(self,app):
        super().__init__(app);self.surfaces=weakref.WeakSet();self.bins={};self.dirty=True
        self.point=None;self.window=None;self.previous=None;self.previous_window=None
        self.pending=QTimer(self);self.pending.setSingleShot(True);self.pending.timeout.connect(self._flush)
        app.installEventFilter(self)
    def observe(self,widget):
        if widget in self.surfaces:return
        self.surfaces.add(widget);widget.setAttribute(Qt.WA_Hover,True);widget.window().setAttribute(Qt.WA_Hover,True);self.dirty=True
    @staticmethod
    def bounds(widget):return QRectF(widget.mapToGlobal(QPoint()),widget.size())
    def _index(self):
        self.bins={}
        for widget in tuple(self.surfaces):
            if not isValid(widget) or not widget.isVisible():continue
            rect=self.bounds(widget).adjusted(-RADIUS,-RADIUS,RADIUS,RADIUS)
            reference=weakref.ref(widget)
            for x in range(math.floor(rect.left()/CELL),math.floor(rect.right()/CELL)+1):
                for y in range(math.floor(rect.top()/CELL),math.floor(rect.bottom()/CELL)+1):self.bins.setdefault((x,y),set()).add(reference)
        self.dirty=False
    def _near(self,point,window):
        if point is None or window is None:return set()
        owner=window()
        if owner is None or not isValid(owner):return set()
        found=set()
        for reference in self.bins.get((math.floor(point.x()/CELL),math.floor(point.y()/CELL)),()):
            widget=reference()
            if (widget is not None and isValid(widget) and widget.isVisible() and widget.window() is owner
                    and border_distance(self.bounds(widget),point)<=RADIUS):found.add(widget)
        return found
    def set_position(self,point,window):
        point=QPointF(point) if point is not None else None
        if point==self.point and (self.window() if self.window else None) is window:return
        self.point=point;self.window=weakref.ref(window) if window is not None else None
        if not self.pending.isActive():self.pending.start(0)
    def _flush(self):
        if self.dirty:self._index()
        for widget in self._near(self.previous,self.previous_window)|self._near(self.point,self.window):
            top=widget.mapToGlobal(QPoint())
            region=QRect()
            for point in (self.previous,self.point):
                if point is not None:
                    local=point-QPointF(top);area=QRectF(local.x()-RADIUS-2,local.y()-RADIUS-2,2*RADIUS+4,2*RADIUS+4).toAlignedRect()
                    region=region.united(area)
            widget.update(region.intersected(widget.rect()))
        self.previous=QPointF(self.point) if self.point is not None else None;self.previous_window=self.window
    def eventFilter(self,watched,event):
        if not isinstance(watched,QWidget):return False
        kind=event.type()
        if kind in (QEvent.Move,QEvent.Resize,QEvent.Show,QEvent.Hide,QEvent.ParentChange,QEvent.WindowStateChange):
            if watched in self.surfaces or watched.isWindow():self.dirty=True
            if kind==QEvent.Hide and self.window and watched is self.window():self.set_position(None,None)
        elif kind==QEvent.MouseMove:self.set_position(event.globalPosition(),watched.window())
        elif kind==QEvent.HoverMove:self.set_position(watched.mapToGlobal(event.position().toPoint()),watched.window())
        elif kind==QEvent.Leave and watched.isWindow() and self.window and watched is self.window():self.set_position(None,None)
        return False
    def paint_stroke(self,painter,path,width,*,pen=None,widget=None):
        if widget is None:widget=painter.device()
        if not isinstance(widget,QWidget) or not widget.isVisible() or not widget.isEnabled():return
        self.observe(widget)
        if self.point is None or not self.window or widget.window() is not self.window():return
        local=self.point-QPointF(widget.mapToGlobal(QPoint()))
        if border_distance(path.boundingRect(),local)>RADIUS:return
        import ui_theme
        shade=outline_light_color(ui_theme.current_theme())
        alpha=shade.alpha()
        gradient=QRadialGradient(local,RADIUS)
        for at,gain in ((0.,1.),(.25,.78),(.5,.32),(.75,.07),(1.,0.)):
            shade.setAlpha(round(alpha*gain));gradient.setColorAt(at,shade)
        highlight=QPen(pen) if pen is not None else QPen(gradient,width)
        highlight.setBrush(gradient);painter.setPen(highlight);painter.setBrush(Qt.NoBrush);painter.drawPath(path)


def install_border_reveal(app):
    if not hasattr(app,'_flickdock_border_reveal'):app._flickdock_border_reveal=BorderReveal(app)
    return app._flickdock_border_reveal
