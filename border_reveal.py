"""Event-driven monochrome light on existing native-painted outlines only.

No overlay windows, blur passes or idle polling. A spatial index limits each
pointer update to nearby surfaces; the existing AA border path is reused.
"""
import math
import weakref
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QPen, QRadialGradient
from PySide6.QtWidgets import QAbstractScrollArea, QWidget
from shiboken6 import isValid

RADIUS=92.
CELL=160


def outline_light_color(theme):
    return QColor(255,255,255,155) if theme=='dark' else QColor(Qt.transparent)


def _theme(widget):
    import ui_theme
    return getattr(widget,'_applied_settings_theme',None) or widget.property('uiTheme') or ui_theme.current_theme()


def border_distance(rect,point):
    x,y=point.x(),point.y()
    if rect.contains(point):return min(x-rect.left(),rect.right()-x,y-rect.top(),rect.bottom()-y)
    return math.hypot(max(rect.left()-x,0,x-rect.right()),max(rect.top()-y,0,y-rect.bottom()))


class BorderReveal(QObject):
    def __init__(self,app):
        super().__init__(app);self.surfaces=weakref.WeakSet();self.bins={};self.dirty=True
        self.point=None;self.window=None
        self._ancestors=weakref.WeakSet()
        self._scroll_areas=weakref.WeakSet()
        self._dirty_regions=weakref.WeakKeyDictionary()
        self.pending=QTimer(self);self.pending.setSingleShot(True);self.pending.timeout.connect(self._flush)
        app.installEventFilter(self)

    def observe(self,widget):
        if widget in self.surfaces:return
        self.surfaces.add(widget)
        widget.setAttribute(Qt.WA_Hover,True)
        widget.window().setAttribute(Qt.WA_Hover,True)
        ancestor=widget.parentWidget()
        while ancestor is not None:
            if ancestor in self._ancestors:break
            self._ancestors.add(ancestor)
            if isinstance(ancestor,QAbstractScrollArea) and ancestor not in self._scroll_areas:
                self._scroll_areas.add(ancestor)
                ancestor.verticalScrollBar().valueChanged.connect(self._geometry_changed)
                ancestor.horizontalScrollBar().valueChanged.connect(self._geometry_changed)
            if ancestor.isWindow():break
            ancestor=ancestor.parentWidget()
        self._geometry_changed()

    @staticmethod
    def bounds(widget):return QRectF(widget.mapToGlobal(QPoint()),widget.size())

    def _geometry_changed(self,*_):
        self.dirty=True
        if self.point is not None and not self.pending.isActive():self.pending.start(0)

    def _index(self,owner):
        self.bins={}
        for widget in tuple(self.surfaces):
            if not isValid(widget) or not widget.isVisible() or widget.window() is not owner:continue
            rect=self.bounds(widget).adjusted(-RADIUS,-RADIUS,RADIUS,RADIUS)
            reference=weakref.ref(widget)
            for x in range(math.floor(rect.left()/CELL),math.floor(rect.right()/CELL)+1):
                for y in range(math.floor(rect.top()/CELL),math.floor(rect.bottom()/CELL)+1):self.bins.setdefault((x,y),set()).add(reference)
        self.dirty=False

    def set_position(self,point,window):
        if window is not None and _theme(window)!='dark':
            point=window=None
        point=QPointF(point) if point is not None else None
        previous=self.window() if self.window else None
        if point==self.point and previous is window:return
        self.point=point;self.window=weakref.ref(window) if window is not None else None
        if previous is not window:self.dirty=True
        if not self.pending.isActive():self.pending.start(0)

    def _flush(self):
        owner=self.window() if self.window else None
        regions=weakref.WeakKeyDictionary()
        if owner is not None and isValid(owner) and _theme(owner)!='dark':
            self.point=self.window=None
        if self.point is not None and owner is not None and isValid(owner):
            if self.dirty:self._index(owner)
            cell=math.floor(self.point.x()/CELL),math.floor(self.point.y()/CELL)
            for reference in self.bins.get(cell,()):
                widget=reference()
                if widget is None or not isValid(widget) or not widget.isVisible():continue
                bounds=self.bounds(widget)
                if border_distance(bounds,self.point)>RADIUS:continue
                local=self.point-bounds.topLeft()
                area=QRectF(local.x()-RADIUS-2,local.y()-RADIUS-2,
                            2*RADIUS+4,2*RADIUS+4).toAlignedRect()
                regions[widget]=area.intersected(widget.rect())
        # Scroll blits can carry a highlight along with a moving card. Erase its
        # old local rectangle even when that card has left the current index cell.
        previous=self._dirty_regions
        self._dirty_regions=regions
        for widget in set(previous)|set(regions):
            if isValid(widget):widget.update(previous.get(widget,QRect()).united(regions.get(widget,QRect())))

    def eventFilter(self,watched,event):
        if not isinstance(watched,QWidget):return False
        kind=event.type()
        owner=self.window() if self.window else None
        if watched is owner and kind in (QEvent.Hide,QEvent.Leave,QEvent.WindowDeactivate):
            self.set_position(None,None)
        elif kind in (QEvent.Move,QEvent.Resize,QEvent.Show,QEvent.Hide,QEvent.ParentChange,QEvent.WindowStateChange):
            if watched is owner or watched in self.surfaces or watched in self._ancestors:
                self._geometry_changed()
        elif kind==QEvent.MouseMove:self.set_position(event.globalPosition(),watched.window())
        elif kind==QEvent.HoverMove:self.set_position(event.globalPosition(),watched.window())
        return False
    def paint_stroke(self,painter,path,width,*,pen=None,widget=None):
        if widget is None:widget=painter.device()
        if not isinstance(widget,QWidget) or not widget.isVisible() or not widget.isEnabled():return
        if _theme(widget.window())!='dark':
            if self.window and widget.window() is self.window():self.set_position(None,None)
            return
        self.observe(widget)
        if self.point is None or not self.window or widget.window() is not self.window():return
        local=self.point-QPointF(widget.mapToGlobal(QPoint()))
        if border_distance(path.boundingRect(),local)>RADIUS:return
        shade=outline_light_color('dark')
        alpha=shade.alpha()
        gradient=QRadialGradient(local,RADIUS)
        for at,gain in ((0.,1.),(.25,.78),(.5,.32),(.75,.07),(1.,0.)):
            shade.setAlpha(round(alpha*gain));gradient.setColorAt(at,shade)
        highlight=QPen(pen) if pen is not None else QPen(gradient,width)
        highlight.setBrush(gradient);painter.setPen(highlight);painter.setBrush(Qt.NoBrush);painter.drawPath(path)


def install_border_reveal(app):
    if not hasattr(app,'_flickdock_border_reveal'):app._flickdock_border_reveal=BorderReveal(app)
    return app._flickdock_border_reveal
