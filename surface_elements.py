"""Qt presentation layers for the shelf header controls.

Only endpoint capture touches widgets. Frames paint cached layers; text keeps
its aspect ratio while card backgrounds change shape. An interrupted frame is
flattened per layer, so reversing never resurrects an old endpoint.
"""
from dataclasses import dataclass
import math

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap, QTransform

@dataclass
class Element:
    key: tuple
    rect: QRectF
    image: QPixmap
    angle: float = 0.
    opacity: float = 1.
    stretch: bool = False

    def outline(self, offset=QPointF()):
        transform=QTransform()
        transform.translate(self.rect.center().x()+offset.x(),self.rect.center().y()+offset.y())
        transform.rotate(self.angle)
        path=QPainterPath()
        path.addRect(QRectF(-self.rect.width()/2,-self.rect.height()/2,self.rect.width(),self.rect.height()))
        return transform.map(path)


def crop(image, rect):
    ratio=image.devicePixelRatioF()
    result=image.copy(QRect(round(rect.x()*ratio),round(rect.y()*ratio),
                           round(rect.width()*ratio),round(rect.height()*ratio)))
    result.setDevicePixelRatio(ratio)
    return result


def _bounds(widget, owner):
    return QRectF(widget.mapTo(owner,QPoint()),widget.size())


def capture(shelf, image):
    """File cards stay in endpoint fades; only header controls may be shared."""
    card=shelf.card
    logo=shelf.popup.brand_logo if shelf.expanded else shelf.brand_logo
    bounds=_bounds(logo,card)
    result=[Element(('brand',),bounds,crop(image,bounds))]
    clipboard=shelf.popup.clipboard_button if shelf.expanded else shelf.zones['permanent'].clipboard_button
    if clipboard.isVisible():
        bounds=_bounds(clipboard,card)
        result.append(Element(('clipboard',),bounds,crop(image,bounds),stretch=True))
    return result


def _interpolate(first,second,progress):
    return first+(second-first)*progress


def paint_element(painter, rect, angle, layers):
    painter.save();painter.translate(rect.center());painter.rotate(angle)
    local=QRectF(-rect.width()/2,-rect.height()/2,rect.width(),rect.height())
    painter.setClipRect(local,Qt.ClipOperation.IntersectClip)
    for element,opacity in layers:
        if opacity<=0:continue
        painter.setOpacity(opacity)
        size=element.image.deviceIndependentSize()
        width=local.width() if element.stretch else size.width()*local.height()/size.height()
        destination=QRectF(local.left(),local.top(),width,local.height())
        painter.drawPixmap(destination,element.image,QRectF(element.image.rect()))
    painter.restore()


class SharedElements:
    def __init__(self,source,target,*,source_size,target_size):
        from ui_transitions import SHARED_ELEMENT_MAX_TRAVEL_PX
        self.source={item.key:item for item in source};self.target={item.key:item for item in target}
        common=self.source.keys() & self.target.keys()
        def reachable(key):
            first=self.source[key].rect.center()-QPointF(source_size.width(),0)
            last=self.target[key].rect.center()-QPointF(target_size.width(),0)
            return math.hypot(last.x()-first.x(),last.y()-first.y())<=SHARED_ELEMENT_MAX_TRAVEL_PX
        self.keys=[key for key in self.source if key in common and reachable(key)]
        self.roots=self.keys

    def mask(self,source,offset):
        table=self.source if source else self.target
        path=QPainterPath();path.setFillRule(Qt.FillRule.WindingFill)
        for key in self.roots:
            if key in table:path.addPath(table[key].outline(offset))
        return path

    def frames(self,progress,source_offset,target_offset):
        for key in self.keys:
            first=self.source[key];last=self.target[key]
            start_rect=QRectF(first.rect);end_rect=QRectF(last.rect)
            start_rect.translate(source_offset);end_rect.translate(target_offset)
            rect=QRectF(*[_interpolate(a,b,progress) for a,b in zip(
                (start_rect.x(),start_rect.y(),start_rect.width(),start_rect.height()),
                (end_rect.x(),end_rect.y(),end_rect.width(),end_rect.height()))])
            angle=_interpolate(first.angle,last.angle,progress)
            layers=[(first,first.opacity),(last,last.opacity*progress)]
            yield key,rect,angle,layers

    def paint(self,painter,progress,source_offset,target_offset):
        for _,rect,angle,layers in self.frames(progress,source_offset,target_offset):
            paint_element(painter,rect,angle,layers)

    def current(self,progress,source_offset,target_offset,ratio):
        result=[]
        for key,rect,angle,layers in self.frames(progress,source_offset,target_offset):
            image=QPixmap(max(1,round(rect.width()*ratio)),max(1,round(rect.height()*ratio)))
            image.setDevicePixelRatio(ratio);image.fill(Qt.transparent)
            painter=QPainter(image);painter.setRenderHints(QPainter.Antialiasing|QPainter.SmoothPixmapTransform)
            paint_element(painter,QRectF(QPointF(),image.deviceIndependentSize()),0,layers);painter.end()
            result.append(Element(key,rect,image,angle,1.,self.target[key].stretch))
        return result
