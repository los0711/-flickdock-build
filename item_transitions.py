"""Short, interruptible item exits; no data ownership or native window resize."""
from __future__ import annotations

import time
from dataclasses import dataclass, replace

from PySide6.QtCore import QEvent, QEasingCurve, QObject, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontInfo, QFontMetricsF, QPainter, QPainterPath, QPen, QPixmap, QRegion
from PySide6.QtWidgets import QGraphicsEffect,QWidget
from shiboken6 import isValid
from ui_transitions import CARD_REBOUND_SPRING, spring_progress


def _curve(kind):
    curve=QEasingCurve(QEasingCurve.Type.BezierSpline)
    if kind=='out':curve.addCubicBezierSegment(QPointF(.23,1),QPointF(.32,1),QPointF(1,1))
    else:curve.addCubicBezierSegment(QPointF(.77,0),QPointF(.175,1),QPointF(1,1))
    return curve


class _EntryEffect(QGraphicsEffect):
    """Move the entire visible file viewport together, without changing hit bounds."""
    def __init__(self,parent,owner=None):
        super().__init__(parent);self.progress=1.;self.owner=owner

    def set_progress(self,value):
        self.progress=max(0.,min(1.,float(value)));self.update()

    def boundingRectFor(self,rect):return rect.adjusted(0,0,0,4)

    def draw(self,painter):
        if self.owner is not None and self.owner.mode_from:
            self.owner.draw_mode(painter)
            return
        # drawSource() may restore painter state for QWidget sources; paint a
        # logical-coordinate pixmap so translation and opacity actually apply.
        offset=QPoint();source=self.sourcePixmap(Qt.LogicalCoordinates,offset,QGraphicsEffect.PixmapPadMode.NoPad)
        painter.save();painter.setOpacity(.45+.55*self.progress)
        painter.drawPixmap(QPointF(offset)+QPointF(0,4*(1-self.progress)),source);painter.restore()


class _Exits(QWidget):
    def __init__(self,owner):
        # Remain visible for the final item's exit after the empty state hides
        # its scroll area. This is a transparent, input-pass-through sibling.
        super().__init__(owner.scroll.parentWidget())
        self.owner=owner
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.hide()

    def paintEvent(self,event):
        painter=QPainter(self)
        now=time.perf_counter()
        for pixmap,position,started,alpha in self.owner.ghosts:
            progress=min(1,max(0,(now-started)/self.owner.duration))
            painter.setOpacity(alpha*(1-self.owner.fade.valueForProgress(progress)))
            painter.drawPixmap(position,pixmap)
        painter.end()


@dataclass(frozen=True)
class SharedText:
    rect: QRectF
    text: str
    font: QFont
    ink: QColor
    align: float=0.

    def between(self,other,t):
        font=QFont(other.font)
        first=self.font.pixelSize() if self.font.pixelSize()>0 else QFontInfo(self.font).pixelSize()
        last=font.pixelSize() if font.pixelSize()>0 else QFontInfo(font).pixelSize()
        font.setPixelSize(max(1,round(_between(first,last,t))))
        return SharedText(_rect_between(self.rect,other.rect,t),other.text if t>=.5 else self.text,
                          font,_ink_between(self.ink,other.ink,t),_between(self.align,other.align,t))

    def paint(self,painter,available):
        rect=self.rect.intersected(available)
        if not self.text or rect.isEmpty():return
        metrics=QFontMetricsF(self.font)
        value=metrics.elidedText(self.text,Qt.ElideRight,rect.width())
        left=rect.left()+(rect.width()-metrics.horizontalAdvance(value))*self.align
        baseline=self.rect.center().y()+(metrics.ascent()-metrics.descent())/2
        painter.setFont(self.font);painter.setPen(self.ink)
        painter.drawText(QPointF(left,baseline),value)


@dataclass(frozen=True)
class FileSelection:
    rect: QRectF
    fill: QColor
    edge: QColor
    tick: QColor

    def between(self,other,t):
        return FileSelection(_rect_between(self.rect,other.rect,t),_selection_ink(self.fill,other.fill,t),
                             _selection_ink(self.edge,other.edge,t),_selection_ink(self.tick,other.tick,t))

    def paint(self,painter):
        from native_ui_common import paint_rounded_surface
        rect=self.rect
        paint_rounded_surface(painter,rect,rect.width()/2,self.fill,self.edge)
        if self.tick.alpha():
            painter.setPen(QPen(self.tick,1.6,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin))
            def point(x,y):return rect.topLeft()+QPointF(x*rect.width(),y*rect.height())
            painter.drawPolyline([point(.27,.49),point(.41,.66),point(.73,.33)])


@dataclass(frozen=True)
class FilePose:
    bounds: QRectF
    thumbnail: QRectF
    badge: QRectF
    art: object
    status: str
    transfer: object
    title: SharedText
    summary: SharedText
    stamp: SharedText
    fill: QColor
    stroke: QColor
    radius: float
    selection: FileSelection
    decorations: tuple=()

    def between(self,other,shape,horizontal,vertical,width,height,details):
        bounds=_rect_between(self.bounds,other.bounds,shape)
        origin=QPointF(_between(self.bounds.x(),other.bounds.x(),horizontal),
                       _between(self.bounds.y(),other.bounds.y(),vertical))
        delta=origin-bounds.topLeft()
        bounds=QRectF(origin.x(),origin.y(),max(1.,_between(self.bounds.width(),other.bounds.width(),width)),
                      max(1.,_between(self.bounds.height(),other.bounds.height(),height)))
        art=self.art if self.art.width()>=other.art.width() else other.art
        title=self.title.between(other.title,shape)
        summary=self.summary.between(other.summary,shape)
        stamp=self.stamp.between(other.stamp,shape)
        selection=self.selection.between(other.selection,shape)
        selection=replace(selection,rect=selection.rect.translated(delta))
        return FilePose(bounds,_rect_between(self.thumbnail,other.thumbnail,shape).translated(delta),
                        _rect_between(self.badge,other.badge,shape).translated(delta),art,other.status,other.transfer,
                        replace(title,rect=title.rect.translated(delta)),replace(summary,rect=summary.rect.translated(delta)),
                        replace(stamp,rect=stamp.rect.translated(delta)),_ink_between(self.fill,other.fill,shape),
                        _ink_between(self.stroke,other.stroke,shape),_between(self.radius,other.radius,shape),selection,
                        _faded(self.decorations,1-min(1.,shape*4),origin-self.bounds.topLeft())+
                        _faded(other.decorations,details,origin-other.bounds.topLeft()))

    def paint(self,painter,opacity,dy):
        from native_ui_common import paint_rounded_surface
        from reference_widgets import draw_status
        painter.save();painter.setOpacity(opacity);painter.translate(0,dy)
        paint_rounded_surface(painter,self.bounds,self.radius,self.fill,self.stroke)
        clip=QPainterPath();clip.addRoundedRect(self.bounds,self.radius,self.radius)
        painter.setClipPath(clip,Qt.IntersectClip)
        painter.drawPixmap(self.thumbnail,self.art,QRectF(self.art.rect()))
        draw_status(painter,self.badge,self.status,self.transfer)
        available=self.bounds.adjusted(8,0,-8,0)
        for part in (self.title,self.summary,self.stamp):
            part.paint(painter,available)
        self.selection.paint(painter)
        for rect,pixmap,alpha in self.decorations:
            painter.setOpacity(opacity*alpha)
            painter.drawPixmap(rect,pixmap,QRectF(pixmap.rect()))
        painter.restore()


def _between(a,b,t):return a+(b-a)*t


def _rect_between(a,b,t):
    return QRectF(_between(a.x(),b.x(),t),_between(a.y(),b.y(),t),
                  max(0.,_between(a.width(),b.width(),t)),max(0.,_between(a.height(),b.height(),t)))


def _ink_between(a,b,t):
    t=max(0.,min(1.,t))
    return QColor.fromRgbF(*(_between(x,y,t) for x,y in zip(a.getRgbF(),b.getRgbF())))


def _selection_ink(a,b,t):
    # Interpolate premultiplied color so Qt.transparent does not inject black
    # into the moving circle, border or tick as it fades in or out.
    t=max(0.,min(1.,t))
    start,end=a.getRgbF(),b.getRgbF()
    alpha=_between(start[3],end[3],t)
    if not alpha:return QColor(Qt.transparent)
    channels=[max(0.,min(1.,_between(x*start[3],y*end[3],t)/alpha)) for x,y in zip(start[:3],end[:3])]
    return QColor.fromRgbF(*channels,alpha)


def _faded(parts,fraction,delta):
    return tuple((rect.translated(delta),pixmap,alpha*fraction) for rect,pixmap,alpha in parts if alpha*fraction>.01)


def control_decoration(widget,viewport):
    if not widget.isVisible():return ()
    ratio=widget.devicePixelRatioF()
    pixmap=QPixmap(round(widget.width()*ratio),round(widget.height()*ratio))
    pixmap.setDevicePixelRatio(ratio);pixmap.fill(Qt.transparent)
    widget.render(pixmap,QPoint(),QRegion(),QWidget.DrawChildren)
    return ((QRectF(widget.mapTo(viewport,QPoint()),widget.size()),pixmap,1.),)


class ItemTransitions(QObject):
    duration=.2
    MODE_DURATION=.64
    MODE_TILE_LIMIT=16
    settled=Signal()

    def __init__(self,scroll,body):
        super().__init__(body)
        self.scroll,self.body=scroll,body
        self.fade=_curve('out');self.move=_curve('move')
        self.ghosts=[];self.moves={};self.generation=0
        self.entry_effect=None;self.entry_started=None;self.entry_pending=False
        self.mode_from={};self.mode_to={};self.mode='view'
        self.layout_pending=False
        self.overlay=_Exits(self)
        body.destroyed.connect(self.overlay.deleteLater)
        from display_frames import FramePulse
        self.timer=FramePulse(self);self.timer.timeout.connect(self._tick)
        # The app-level wheel router can consume a wheel event before the
        # viewport filter sees it. Stop entrance effects before that first step.
        self.scroll._flickdock_before_scroll=self.finish
        # Scroll is an immediate user operation. Never drag old pixels along
        # with it, or animate to positions from a previous viewport size.
        scroll.verticalScrollBar().valueChanged.connect(self._scrolled)
        scroll.horizontalScrollBar().valueChanged.connect(self._scrolled)
        scroll.viewport().installEventFilter(self)
        scroll.verticalScrollBar().sliderPressed.connect(self.finish)
        scroll.horizontalScrollBar().sliderPressed.connect(self.finish)

    def eventFilter(self,watched,event):
        if event.type() in (QEvent.Wheel,QEvent.KeyPress,QEvent.TouchBegin):self.finish()
        return super().eventFilter(watched,event)

    def _scrolled(self,*_):
        if not self.layout_pending and not self.entry_pending:self.finish()

    def capture(self,widgets):
        self.generation+=1
        self.layout_pending=True
        positions={ref:QPoint(widget.pos()) for ref,widget in widgets.items() if isValid(widget)}
        # A second edit starts at each row's CURRENT painted position.
        self._finish_entrances()
        self.moves.clear()
        return positions

    def _visible_tiles(self,widgets):
        viewport=self.scroll.viewport()
        tiles={}
        for ref,widget in widgets.items():
            if not isValid(widget) or not widget.isVisible():continue
            rect=QRectF(widget.rect().translated(widget.mapTo(viewport,QPoint())))
            if not rect.intersects(QRectF(viewport.rect())):continue
            tiles[ref]=widget.transition_pose(viewport)
            if len(tiles)>=self.MODE_TILE_LIMIT:break
        return tiles

    def capture_view_mode(self,widgets):
        if not self.mode_from:return self._visible_tiles(widgets)
        # Preserve the painted positions when the toggle is reversed mid-flight.
        return {ref:pose for ref,pose,opacity,_ in self._mode_frames() if opacity>=.5}

    def prepare_entrance(self,*,enabled,mode_source=None,mode='view'):
        from ui_transitions import motion_allowed
        if not enabled or not motion_allowed():return False
        viewport=self.scroll.viewport()
        if self.entry_effect is None:
            if viewport.graphicsEffect() is not None:return False
            self.entry_effect=_EntryEffect(viewport,self);viewport.setGraphicsEffect(self.entry_effect)
        self.mode_from=mode_source or {}
        self.mode_to={}
        self.mode=mode
        self.entry_effect.set_progress(0);self.entry_effect.setEnabled(True)
        self.entry_pending=True;self.entry_started=None
        return True

    def enter(self,widgets=None):
        if not self.entry_pending:return
        if self.mode_from:self.mode_to=self._visible_tiles(widgets or {})
        self.entry_pending=False;self.entry_started=time.perf_counter();self.timer.start()

    def _mode_frames(self):
        if self.entry_started is None:
            for ref,pose in self.mode_from.items():yield ref,pose,1.,0.
            return
        t=max(0.,time.perf_counter()-self.entry_started)
        def shape_at(delay):
            return max(0.,min(1.04,spring_progress(max(0.,t-delay),240,21)))
        def move_at(delay):
            return spring_progress(max(0.,t-delay),300,26)
        position=spring_progress(t,300,26)
        detail=max(0.,min(1.,spring_progress(max(0.,t-.16),260,28)))
        arrival=max(0.,min(1.,spring_progress(t,300,30)))
        for ref in dict.fromkeys((*self.mode_from,*self.mode_to)):
            old=self.mode_from.get(ref);new=self.mode_to.get(ref)
            if old and new:
                if self.mode=='sort':
                    shift=spring_progress(t,*CARD_REBOUND_SPRING)
                    detail=1. if old.bounds==new.bounds else arrival
                    yield ref,old.between(new,arrival,shift,shift,1.,1.,detail),1.,0.
                    continue
                # Move the compact footprint before widening long rows.
                # Collapsing list widths early leaves room for neighboring cards.
                grid=new.bounds.width()<=old.bounds.width()
                width=shape_at(0 if grid else .17)
                height=shape_at(.145 if grid else 0)
                shape=shape_at(.075 if grid else .085)
                horizontal=move_at(.02 if grid else .15)
                vertical=move_at(.18 if grid else 0)
                yield ref,old.between(new,shape,horizontal,vertical,width,height,detail),1.,0.
            elif old:yield ref,old,1.-arrival,10*position
            elif new:yield ref,new,arrival,10*(1-position)

    def draw_mode(self,painter):
        from native_ui_common import qss_color
        import ui_theme
        painter.fillRect(self.scroll.viewport().rect(),qss_color(ui_theme.color('card')))
        painter.save()
        painter.setRenderHints(QPainter.Antialiasing|QPainter.SmoothPixmapTransform)
        for _,pose,opacity,offset in self._mode_frames():
            pose.paint(painter,opacity,offset)
        painter.restore()

    def _finish_entrances(self):
        self.entry_started=None;self.entry_pending=False
        self.mode_from={};self.mode_to={}
        if self.entry_effect is not None and isValid(self.entry_effect):
            self.entry_effect.set_progress(1);self.entry_effect.setEnabled(False)

    def remove(self,widgets,*,enabled):
        from ui_transitions import motion_allowed
        enabled=enabled and motion_allowed()
        if not enabled:
            self.finish();return
        viewport=self.scroll.viewport()
        now=time.perf_counter()
        for widget in widgets:
            if not isValid(widget) or not widget.isVisible():continue
            position=widget.mapTo(viewport,QPoint())
            if not viewport.rect().intersects(widget.rect().translated(position)):continue
            self.ghosts.append((widget.grab(),position,now,1.0))
        if self.ghosts:
            self.overlay.setGeometry(viewport.rect().translated(viewport.mapTo(self.overlay.parentWidget(),QPoint())))
            self.overlay.show();self.overlay.raise_()
            self.timer.start()

    def reflow(self,widgets,positions,*,enabled):
        from ui_transitions import motion_allowed
        enabled=enabled and motion_allowed()
        generation=self.generation
        def begin():
            if not isValid(self) or generation!=self.generation:return
            self.layout_pending=False
            if not enabled:self.finish();return
            now=time.perf_counter()
            moved=0
            for ref,widget in widgets.items():
                if ref not in positions or not isValid(widget):continue
                origin=positions[ref];target=QPoint(widget.pos())
                if origin!=target:
                    widget.move(origin)
                    self.moves[ref]=(widget,origin,target,now)
                    moved+=1
            if moved:
                layout=self.body.layout()
                if layout is not None:layout.setEnabled(False)
                self.duration=.22
                self.timer.start()
            elif not self.ghosts:self.finish()
        QTimer.singleShot(0,self,begin)

    def _tick(self):
        now=time.perf_counter()
        if self.entry_started is not None and self.entry_effect is not None and isValid(self.entry_effect):
            progress=min(1,max(0,(now-self.entry_started)/(self.MODE_DURATION if self.mode_from else self.duration)))
            self.entry_effect.set_progress(self.fade.valueForProgress(progress))
            if progress>=1:self._finish_entrances()
        for ref,(widget,origin,target,started) in tuple(self.moves.items()):
            if not isValid(widget):self.moves.pop(ref,None);continue
            progress=min(1,max(0,(now-started)/self.duration))
            fraction=self.move.valueForProgress(progress)
            widget.move(QPoint(round(origin.x()+(target.x()-origin.x())*fraction),
                               round(origin.y()+(target.y()-origin.y())*fraction)))
            if progress>=1:self.moves.pop(ref,None)
        self.ghosts=[ghost for ghost in self.ghosts if now-ghost[2]<self.duration]
        if self.ghosts:self.overlay.update()
        else:self.overlay.hide()
        if not self.moves and not self.ghosts and self.entry_started is None:
            layout=self.body.layout() if isValid(self.body) else None
            if layout is not None and not layout.isEnabled():layout.setEnabled(True)
            self.timer.stop();self.settled.emit()

    def finish(self,*_):
        self.generation+=1
        self.layout_pending=False
        running=self.timer.isActive() or self.entry_pending or self.entry_started is not None or bool(self.moves or self.ghosts)
        for widget,origin,target,started in self.moves.values():
            if isValid(widget):widget.move(target)
        layout=self.body.layout() if isValid(self.body) else None
        if layout is not None and not layout.isEnabled():layout.setEnabled(True)
        self.moves.clear();self.ghosts.clear();self._finish_entrances();self.timer.stop();self.overlay.hide()
        if running:self.settled.emit()
