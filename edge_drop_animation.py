"""Post-drop artwork and a bounded Genie mesh, never a native drag image.

No files, clipboard, decoders or shell extensions are opened here. The image
is drawn once after the shelf accepts the payload. Only this visual copy is
deformed; the system pointer / source drag artwork is never changed.
"""
from __future__ import annotations

from dataclasses import dataclass,replace
import math
from pathlib import PureWindowsPath

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBitmap, QColor, QFont, QImage, QPainter, QPainterPath, QPen, QRegion, QTransform

from edge_magnetic_model import DEFAULT_CONFIG, clamp, ease_out, smoothstep


@dataclass(frozen=True, slots=True)
class DropPose:
    inner: float
    outer: float
    half_height: float
    neck: float
    bend: float
    opacity: float
    done: bool = False

    def span_at(self, u):
        """Inward depth, tangent centre and half-height of one mesh column."""
        return (self.inner+(self.outer-self.inner)*u,
                self.bend*math.sin(math.pi*u),
                self.half_height*(1-self.neck*(1-smoothstep(u))))


def drop_pose(elapsed_ms, start_depth, available_depth, config=DEFAULT_CONFIG, *, depth_ratio=1.0, tangent_ratio=1.0):
    """Settle to a safe distance, then progressively pinch and pull the page.

    At the edge the copy starts fully inside the screen and backs off by about
    50 DIP. It is NOT a rectangle being scaled down: inner/outer edges travel
    independently and each cross-section has a different height.
    """
    half=min(config.drop_icon_size_px*depth_ratio/2, max(4.0,(available_depth-4)/4))
    tangent_half=config.drop_icon_size_px*tangent_ratio/2
    start=clamp(float(start_depth),half+1,max(half+1,available_depth-half-1))
    rest=clamp(config.drop_rest_depth_px,half+8,max(half+8,available_depth-half-1))
    if elapsed_ms<=config.drop_settle_ms:
        center=start+(rest-start)*ease_out(elapsed_ms/config.drop_settle_ms)
        return DropPose(center-half,center+half,tangent_half,0,0,1)
    p=clamp((elapsed_ms-config.drop_settle_ms)/config.drop_absorb_ms)
    if p>=1:return DropPose(0,0,0,1,0,0,True)
    inner=(rest-half)*(1-smoothstep(p/.56))
    outer=(rest+half)*(1-p**2.3)
    height=tangent_half*(1-smoothstep(max(0,p-.38)/.62))**.65
    neck=.975*smoothstep(p/.58)
    return DropPose(max(0,inner),max(inner,outer),height,neck,
                    3.5*math.sin(math.pi*p),1-smoothstep(max(0,p-.88)/.12))


def file_artwork(urls=(), config=DEFAULT_CONFIG, dpr=1.0):
    """System stock icon for no-path callers / explicit offscreen fixtures.

    Production file drops inject the asynchronously resolved per-file image;
    they never use the former hand-drawn unified document.
    """
    from edge_native_icons import generic_icon
    return generic_icon(round(config.drop_icon_size_px*max(1.0,min(3.0,dpr))))


def file_stack_artwork(images,count,config=DEFAULT_CONFIG,dpr=1.0):
    """Compose at most four real icons once; frame cost is independent of count."""
    images=[image for image in images[:config.drop_stack_max_icons] if not image.isNull()]
    if not images:return QImage()
    if len(images)==1 and count<=1:return QImage(images[0])
    size=config.drop_icon_size_px;offset=config.drop_stack_offset_px
    positions=(0.,-1.,1.,-1.65)
    pad=max(abs(value) for value in positions[:len(images)])*offset+10;extent=size+2*pad
    dpr=max(1.,min(3.,float(dpr)))
    result=QImage(math.ceil(extent*dpr),math.ceil(extent*dpr),QImage.Format_ARGB32_Premultiplied)
    result.setDevicePixelRatio(dpr);result.fill(Qt.transparent)
    painter=QPainter(result);painter.setRenderHints(QPainter.Antialiasing|QPainter.SmoothPixmapTransform)
    for index in range(len(images)-1,-1,-1):
        center=QPointF(extent/2+positions[index]*offset,extent/2-index*offset*.38)
        painter.save();painter.translate(center);painter.rotate(positions[index]*config.drop_stack_angle_degrees)
        painter.drawImage(QRectF(-size/2,-size/2,size,size),images[index]);painter.restore()
    if count>1:
        text=str(count) if count<=999 else '999+'
        font=QFont(painter.font());font.setPixelSize(12);font.setBold(True);painter.setFont(font)
        from PySide6.QtGui import QFontMetricsF
        width=max(22.,QFontMetricsF(font).horizontalAdvance(text)+12)
        badge=QRectF(extent/2+size/2-width+3,extent/2+size/2-16,width,22)
        painter.setPen(QPen(QColor('#ffffff'),1.3));painter.setBrush(QColor('#1e2636'));painter.drawRoundedRect(badge,11,11)
        painter.setPen(QColor('#ffffff'));painter.drawText(badge,Qt.AlignCenter,text)
    painter.end();return result


class DropArtwork:
    """One accepted drop; fixed source samples and display-paced mesh drawing."""
    def __init__(self, target, urls=(), *, config=DEFAULT_CONFIG, dpr=1.0, image=None,images=None):
        self.layer_count=min(len(images or ()),config.drop_stack_max_icons)
        self.file_count=len(urls) or self.layer_count or 1
        if images:
            image=file_stack_artwork(images,self.file_count,config,dpr)
            if self.file_count>1:
                config=replace(config,drop_icon_size_px=min(96,config.drop_icon_size_px+config.drop_stack_offset_px*max(1,self.layer_count-1)))
        self.config=config;self.elapsed_ms=0.0
        self.target=target;self.start_depth=target.distance
        area=target.area
        self.available_depth=area.width() if target.edge in ('left','right') else area.height()
        tangent=target.point.y() if target.edge in ('left','right') else target.point.x()
        low,high=(area.top(),area.bottom()) if target.edge in ('left','right') else (area.left(),area.right())
        margin=config.drop_icon_size_px/2+4
        self.tangent=clamp(tangent,low+margin,max(low+margin,high-margin))
        image=QImage(image) if image is not None and not image.isNull() else file_artwork(urls,config,dpr)
        # Padding must not become a transparent neck: the actual page pixels
        # need to meet the edge. Trim once in Qt's native mask code, without a
        # Python pixel loop, then retain the document's original proportions.
        bounds=QRegion(QBitmap.fromImage(image.createAlphaMask())).boundingRect()
        if not bounds.isEmpty():image=image.copy(bounds)
        # Compensate the canonical inward/tangent basis once. The intact icon
        # remains upright on all four edges, rather than mirrored or sideways.
        if target.edge=='right':image=image.flipped(Qt.Horizontal)
        elif target.edge=='top':image=image.transformed(QTransform(0,1,1,0,0,0))
        elif target.edge=='bottom':image=image.transformed(QTransform().rotate(90))
        self.image=image
        extent=max(image.width(),image.height())
        self.depth_ratio,self.tangent_ratio=image.width()/extent,image.height()/extent
        self._sources=[];self._weights=[]
        count=config.drop_mesh_strips
        width,height=image.width()/image.devicePixelRatio(),image.height()/image.devicePixelRatio()
        for index in range(count):
            a,b=index/count,(index+1)/count
            source=QRectF(width*a,0,width*(b-a),height)
            self._sources.append(source)
            self._weights.append((a,b,(a+b)/2))

    @property
    def pose(self):return drop_pose(self.elapsed_ms,self.start_depth,self.available_depth,self.config,
                                    depth_ratio=self.depth_ratio,tangent_ratio=self.tangent_ratio)

    @property
    def running(self):return self.elapsed_ms<self.config.drop_settle_ms+self.config.drop_absorb_ms

    def step(self,dt):self.elapsed_ms+=max(0,float(dt))*1000

    def paint(self,painter,origin):
        pose=self.pose
        if pose.done:return
        area,edge=self.target.area,self.target.edge
        x,y=origin.x(),origin.y()
        transforms={
            'left':QTransform(1,0,0,1,area.left()+.5-x,self.tangent-y),
            'right':QTransform(-1,0,0,1,area.right()+.5-x,self.tangent-y),
            'top':QTransform(0,1,1,0,self.tangent-x,area.top()+.5-y),
            'bottom':QTransform(0,-1,1,0,self.tangent-x,area.bottom()+.5-y),
        }
        painter.save();painter.setWorldTransform(transforms[edge],combine=True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform);painter.setOpacity(pose.opacity)
        if pose.neck==0:
            painter.drawImage(QRectF(pose.inner,-pose.half_height,pose.outer-pose.inner,2*pose.half_height),self.image)
        else:
            # Sample the full original image with bounded affine columns.
            # Cropping each source strip introduces bilinear seams; projective
            # quads become singular as the neck closes. Affine columns avoid
            # both, with a half-pixel shared clip to cover raster boundaries.
            height=self.image.height()/self.image.devicePixelRatio()
            for source,(a,b,middle) in zip(self._sources,self._weights):
                left,c0,h0=pose.span_at(a);right,c1,h1=pose.span_at(b)
                _,center,half=pose.span_at(middle)
                sx=(right-left)/source.width();sy=2*half/height
                if sx<1e-5 or sy<1e-5:continue
                shear=(c1-c0)/source.width()
                mesh=QTransform(sx,shear,0,sy,left-sx*source.left(),center-half-shear*source.center().x())
                painter.save();painter.setWorldTransform(mesh,combine=True)
                overlap=min(source.width(),.55/max(.01,sx))
                painter.setClipRect(source.adjusted(-overlap,0,overlap,0),Qt.IntersectClip)
                painter.drawImage(QPointF(),self.image);painter.restore()
        painter.restore()
