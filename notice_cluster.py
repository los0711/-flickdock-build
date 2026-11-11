"""Short-lived metaball joins between the existing fixed native notice hosts.

The circular-cap Bézier pattern follows Paper.js's MIT Meta Balls example
(v0.12.18), originally by SATO Hiroyuki. No blur pass or full-screen overlay.
"""
import math
import weakref
from PySide6.QtCore import QPoint, QPointF, QRectF
from PySide6.QtGui import QPainterPath, QPainterPathStroker, QTransform
from shiboken6 import isValid
from ui_transitions import NOTICE_SPLIT_BRIDGE_END

CAP_HEIGHT_PX = 44
MAX_JOIN_GAP_PX = 24
JOIN_HANDLE_RATIO = .6


def _bounds(window):
    rect=QRectF(window.capsule_rect())
    rect.setTop(max(rect.top(),rect.bottom()-CAP_HEIGHT_PX))
    return rect.translated(QPointF(window.pos()))


def _linked(window):
    peer=window._liquid_peer() if window._liquid_peer is not None else None
    return [other for other in [peer,*window._liquid_children]
            if other is not None and isValid(other) and other.isVisible()]


def clear(window):
    neighbors=_linked(window)
    peer=window._liquid_peer() if window._liquid_peer is not None else None
    if peer is not None and isValid(peer):peer._liquid_children.discard(window)
    window._liquid_peer=None
    window._liquid_mode=None
    for other in neighbors:other._clip_finished();other.update()
    window._clip_finished();window.update()


def _attach(window,peer):
    clear(window)
    window._liquid_peer=weakref.ref(peer)
    peer._liquid_children.add(window)


def prepare_split(window,peers):
    if not peers:return
    peer=min(peers,key=lambda other:abs(other.x()-window.x()))
    target=window.pos()
    center=_bounds(peer).center()
    local=_bounds(window).center()-QPointF(window.pos())
    _attach(window,peer)
    window._liquid_mode='split'
    window.move((center-local).toPoint())
    window._liquid_target=target


def start_split(window):
    target=window._liquid_target
    window._liquid_target=None
    if target is None:return
    window._position_motion.setStartValue(window.pos())
    window._position_motion.setEndValue(target)
    window._position_motion.start()
    peer=window._liquid_peer()
    if peer is not None and isValid(peer):peer.raise_()


def start_merge(window,peers):
    if not peers:return
    peer=min(peers,key=lambda other:abs(_bounds(other).center().x()-_bounds(window).center().x()))
    _attach(window,peer)
    window._liquid_mode='merge'
    target=peer._position_motion.endValue() if peer._position_motion.state()==peer._position_motion.Running else peer.pos()
    center=_bounds(peer).center()+QPointF(target-peer.pos())
    local=_bounds(window).center()-QPointF(window.pos())
    window._position_motion.stop()
    window._position_motion.setDuration(window._morph.duration())
    window._position_motion.setEasingCurve(window._morph.easingCurve())
    window._position_motion.setStartValue(window.pos())
    window._position_motion.setEndValue((center-local).toPoint())
    window._position_motion.start()
    peer.raise_()


def frame(window,*_):
    window._clip_finished();window.update()
    for other in _linked(window):other._clip_finished();other.update()


def _bridge(left,right,strength):
    path=QPainterPath()
    gap=right.left()-left.right()
    if not 0<gap<MAX_JOIN_GAP_PX or abs(left.center().y()-right.center().y())>CAP_HEIGHT_PX/2:return path
    radius1,radius2=left.height()/2,right.height()/2
    angle=math.pi/4*strength
    if angle<=0:return path
    c1=QPointF(left.right()-radius1,left.center().y())
    c2=QPointF(right.left()+radius2,right.center().y())
    sine,cosine=math.sin(angle),math.cos(angle)
    a=c1+QPointF(radius1*cosine,-radius1*sine)
    b=c2+QPointF(-radius2*cosine,-radius2*sine)
    c=c2+QPointF(-radius2*cosine,radius2*sine)
    d=c1+QPointF(radius1*cosine,radius1*sine)
    handle=min((b.x()-a.x())/2,min(radius1,radius2)*JOIN_HANDLE_RATIO)
    tangent=QPointF(sine*handle,cosine*handle)
    path.moveTo(a);path.cubicTo(a+tangent,b+QPointF(-tangent.x(),tangent.y()),b)
    path.lineTo(c);path.cubicTo(c-tangent,d+QPointF(tangent.x(),-tangent.y()),d)
    path.closeSubpath()
    return path


def shape(window,base,*,mask=False):
    for other in _linked(window):
        moving=window if window._liquid_peer is not None and window._liquid_peer() is other else other
        clock=moving._position_motion
        if clock.state()!=clock.Running:continue
        progress=clock.currentTime()/max(1,clock.duration())
        end=NOTICE_SPLIT_BRIDGE_END if moving._liquid_mode=='split' else 1.
        if progress>=end:continue
        strength=math.sin(math.pi*progress/end)
        own,peer=_bounds(window),_bounds(other)
        left,right=(own,peer) if own.center().x()<peer.center().x() else (peer,own)
        bridge=_bridge(left,right,strength)
        if mask and not bridge.isEmpty():
            fringe=QPainterPathStroker();fringe.setWidth(2.)
            bridge=bridge.united(fringe.createStroke(bridge))
        # Each host paints its half, preserving alpha and native hit testing
        # across overlapping fixed-size tool windows.
        middle=(left.right()+right.left())/2
        clip=QPainterPath();clip.addRect(QRectF(own.left()-MAX_JOIN_GAP_PX,own.top()-2,
            own.width()+2*MAX_JOIN_GAP_PX,own.height()+4))
        half=QPainterPath();half.addRect(QRectF(middle if own is right else own.left()-MAX_JOIN_GAP_PX,
            own.top()-2,own.right()+MAX_JOIN_GAP_PX-middle if own is right else middle-own.left()+MAX_JOIN_GAP_PX,own.height()+4))
        transform=QTransform.fromTranslate(-window.x(),-window.y())
        base=base.united(transform.map(bridge.intersected(clip).intersected(half)))
    return base
