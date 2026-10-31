"""Curvature-continuous application corners, in logical painter coordinates.

Each 90-degree corner has two cubic Beziers. Their middle first/second
derivatives agree (C2); both straight-edge contacts have zero curvature and
nonzero tangent (G2). This is not a circular arc with a larger radius. Native
OS-owned title bars/dialogs and existing circles remain system/original art.

No widgets, timers, event filters, native masks or background captures are
installed here. Callers must use the same path for their actual fill, stroke,
clip, shadow and native material mask; a QRegion alone cannot fix a QSS fill.
"""
from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
import math

from PySide6.QtCore import QRectF,Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen


G2_SMOOTHING = .22


def _pair(value):
    if isinstance(value,Sequence) and not isinstance(value,(str,bytes)):
        if len(value)!=2: raise ValueError('Corner radius requires x/y values')
        x,y=map(float,value)
    elif hasattr(value,'width') and hasattr(value,'height'):
        x,y=float(value.width()),float(value.height())
    else:x=y=float(value)
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError('Corner radii must be finite')
    x,y=max(0.,x),max(0.,y)
    # CSS zero on either axis denotes a square corner.
    return (x,y) if x>0 and y>0 else (0.,0.)


def clamped_corner_radii(rect,radius=10.,*,corner_radii=None):
    """Return TL/TR/BR/BL x/y radii without changing the requested rectangle.

    A scalar is kept circular and clamped to half the smaller dimension. An
    x/y pair permits an elliptical affine transform. Explicit per-corner radii
    follow the CSS shared overlap scale, preserving each requested proportion.
    """
    rect=QRectF(rect)
    if any(not math.isfinite(n) for n in (rect.x(),rect.y(),rect.width(),rect.height())):
        raise ValueError('Corner rectangle must be finite')
    if rect.isEmpty():return ((0.,0.),)*4
    width,height=rect.width(),rect.height()
    if corner_radii is None:
        x,y=_pair(radius)
        if x==y:
            x=y=min(x,width/2,height/2)
        else:x,y=min(x,width/2),min(y,height/2)
        return ((x,y),)*4
    if len(corner_radii)!=4:raise ValueError('Four corner radii are required: TL/TR/BR/BL')
    radii=tuple(_pair(value) for value in corner_radii)
    tl,tr,br,bl=radii
    scale=1.
    for extent,total in ((width,tl[0]+tr[0]),(width,bl[0]+br[0]),
                         (height,tl[1]+bl[1]),(height,tr[1]+br[1])):
        if total>0:scale=min(scale,extent/total)
    return tuple((x*scale,y*scale) for x,y in radii)


def continuous_corner_segments(radius_x=1.,radius_y=None):
    """Local left-edge (0,ry) to top-edge (rx,0), including both endpoints."""
    x,y=_pair((radius_x,radius_x if radius_y is None else radius_y));m=G2_SMOOTHING
    return (((0.,y),(0.,4*m*y),(0.,2*m*y),(m*x,m*y)),
            ((m*x,m*y),(2*m*x,0.),(4*m*x,0.),(x,0.)))


def continuous_rect_path(rect,radius=10.,*,corner_radii=None):
    """A clockwise closed true-G2 rectangle; radius/coordinates are logical."""
    rect=QRectF(rect)
    radii=clamped_corner_radii(rect,radius,corner_radii=corner_radii)
    if rect.isEmpty():return QPainterPath()
    # Cache only a bounded, origin-independent geometry, not widget snapshots.
    # Always copy: callers may mutate their path without corrupting the cache.
    path=QPainterPath(_normalized_rect_path(rect.width(),rect.height(),radii))
    path.translate(rect.left(),rect.top())
    return path


@lru_cache(maxsize=256)
def _normalized_rect_path(width,height,radii):
    path=QPainterPath()
    left=top=0.;right=width;bottom=height
    tl,tr,br,bl=radii
    path.moveTo(left+tl[0],top)
    # Transform the single canonical corner, preserving C2 under arbitrary
    # affine DPI scaling instead of rounding its control points to pixels.
    corners=(
        (tr,(right-tr[0],top),lambda u,v:(right-v,top+u)),
        (br,(right,bottom-br[1]),lambda u,v:(right-u,bottom-v)),
        (bl,(left+bl[0],bottom),lambda u,v:(left+v,bottom-u)),
        (tl,(left,top+tl[1]),lambda u,v:(left+u,top+v)),
    )
    for index,((rx,ry),start,transform) in enumerate(corners):
        path.lineTo(*start)
        if rx<=0 or ry<=0:
            endpoint=((right,top),(right,bottom),(left,bottom),(left,top))[index]
            path.lineTo(*endpoint)
            continue
        # TR/BL exchange canonical axes when mapping left->top clockwise.
        canonical=(ry,rx) if index in (0,2) else (rx,ry)
        for segment in continuous_corner_segments(*canonical):
            p1,p2,p3=(transform(*point) for point in segment[1:])
            path.cubicTo(*p1,*p2,*p3)
    path.closeSubpath()
    return path


def draw_continuous_rect(painter,rect,radius=10.,*,corner_radii=None,fill=None,pen=None):
    """Draw the real fill/stroke path, preserving all caller painter state.

    None uses the caller's current brush/pen; Qt.NoBrush/NoPen are accepted.
    No clipping/masking of descendants is implicitly added.
    """
    path=continuous_rect_path(rect,radius,corner_radii=corner_radii)
    if path.isEmpty():return path
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if fill is not None:
            painter.setBrush(QColor(fill) if isinstance(fill,(str,QColor)) else fill)
        if pen is not None:painter.setPen(QPen(QColor(pen)) if isinstance(pen,(str,QColor)) else pen)
        painter.drawPath(path)
    finally:painter.restore()
    return path


def paint_continuous_panel(painter,rect,radius=10.,*,fill=None,border=None,border_width=1.,corner_radii=None):
    """Real G2 background and border ring with one exact outer silhouette.

    Useful for replacing a QSS panel, where clipping a circular QSS fill to a
    different mask would leave the old arc visible. The inner border contour
    is also a G2 path. Native control text/IME/selection painting stays separate.
    """
    if not math.isfinite(border_width) or border_width<0:
        raise ValueError('Panel border width must be finite and nonnegative')
    rect=QRectF(rect);outer=continuous_rect_path(rect,radius,corner_radii=corner_radii)
    if outer.isEmpty():return outer
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if fill is not None:painter.fillPath(outer,QColor(fill) if isinstance(fill,(str,QColor)) else fill)
        if border is not None and border_width>0:
            radii=clamped_corner_radii(rect,radius,corner_radii=corner_radii)
            ring=QPainterPath(_normalized_border_ring(rect.width(),rect.height(),radii,float(border_width)))
            ring.translate(rect.x(),rect.y())
            painter.fillPath(ring,QColor(border) if isinstance(border,(str,QColor)) else border)
    finally:painter.restore()
    return outer


@lru_cache(maxsize=256)
def _normalized_border_ring(width,height,radii,border_width):
    """Exact cubic outer/inner contours, no flattening boolean operation."""
    outer=QPainterPath(_normalized_rect_path(width,height,radii))
    inner_rect=QRectF(border_width,border_width,width-2*border_width,height-2*border_width)
    inner_radii=tuple((max(0.,x-border_width),max(0.,y-border_width)) for x,y in radii)
    inner=continuous_rect_path(inner_rect,corner_radii=inner_radii)
    if not inner.isEmpty():
        # QPainterPath.subtracted flattens these Beziers into hundreds of line
        # segments. Odd-even subpaths preserve the real G2 curves on both edges.
        outer.addPath(inner);outer.setFillRule(Qt.FillRule.OddEvenFill)
    return outer
