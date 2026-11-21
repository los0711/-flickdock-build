"""Cached presentation layers for the shelf and overview chrome.

Only endpoint capture touches widgets. Frames paint cached layers; text keeps
its aspect ratio while card backgrounds change shape. An interrupted frame is
flattened per layer, so reversing never resurrects an old endpoint.
"""
from dataclasses import dataclass
import math

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap, QRegion, QTransform
from PySide6.QtWidgets import QWidget

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


def _sibling_capture(widget, owner, ratio):
    """The mini transfer button is a live sibling, outside the card snapshot."""
    bounds=QRectF(widget.pos()-owner.pos(),widget.size())
    image=QPixmap(max(1,round(bounds.width()*ratio)),max(1,round(bounds.height()*ratio)))
    image.setDevicePixelRatio(ratio);image.fill(Qt.transparent)
    painter=QPainter(image)
    try:widget.render(painter,QPoint(),QRegion(),QWidget.RenderFlag.DrawChildren)
    finally:painter.end()
    return bounds,image


def capture(shelf, image):
    """Keep file cards in endpoint fades; capture the small chrome once."""
    card=shelf.card
    mode='overview' if shelf.expanded else 'mini'
    zone=shelf.zones['permanent']
    if shelf.expanded:
        controls=(('brand',shelf.popup.brand_logo),
                  ('capacity',shelf.popup.capacity_button),('clipboard',shelf.popup.clipboard_button),
                  ('count',shelf.popup.count_label))
    else:
        controls=(('brand',shelf.brand_logo),('capacity',zone.capacity_button),
                  ('clipboard',zone.clipboard_button),('count',zone.caption),
                  ('content',zone.icon),('hint',zone.hint))
    result=[]
    for role,widget in controls:
        if not widget.isVisible():continue
        bounds=_bounds(widget,card)
        result.append(Element(('chrome',mode,role),bounds,crop(image,bounds)))
    if not shelf.expanded and shelf.lan_button.isVisible():
        bounds,pixels=_sibling_capture(shelf.lan_button,card,image.devicePixelRatioF())
        result.append(Element(('chrome',mode,'transfer'),bounds,pixels))
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
        self.keys=[key for key in self.source if key in common and key[0]!='chrome' and reachable(key)]
        self.chrome=[key for key in dict.fromkeys((*self.source,*self.target)) if key[0]=='chrome']
        self.roots=[*self.keys,*self.chrome]

    def mask(self,source,offset):
        table=self.source if source else self.target
        path=QPainterPath();path.setFillRule(Qt.FillRule.WindingFill)
        for key in self.roots:
            if key in table:path.addPath(table[key].outline(offset))
        return path

    @staticmethod
    def _smooth(value):
        value=max(0.,min(1.,value))
        return value*value*(3.-2.*value)

    def frames(self,progress,source_offset,target_offset,motion_progress=None):
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

        # Use the linear frame clock, not the window's eased geometry value:
        # arrival begins only when the resize is close to its endpoint.
        t=max(0.,min(1.,progress if motion_progress is None else motion_progress))
        leaving=self._smooth(t/.30)
        entering=self._smooth((t-.56)/.44)
        for key in self.chrome:
            role=key[-1]
            shift=(QPointF(-3,0) if role in ('brand','count') else
                   QPointF(0,4) if role in ('content','hint') else QPointF(0,3))
            first=self.source.get(key)
            if first is not None and leaving<1:
                rect=QRectF(first.rect);rect.translate(source_offset+shift*leaving)
                yield key,rect,first.angle,[(first,first.opacity*(1-leaving))]
            last=self.target.get(key)
            if last is not None and entering>0:
                rect=QRectF(last.rect);rect.translate(target_offset+shift*(1-entering))
                yield key,rect,last.angle,[(last,last.opacity*entering)]

    def paint(self,painter,progress,source_offset,target_offset,motion_progress=None):
        for _,rect,angle,layers in self.frames(progress,source_offset,target_offset,motion_progress):
            paint_element(painter,rect,angle,layers)

    def current(self,progress,source_offset,target_offset,ratio,motion_progress=None):
        result=[]
        for key,rect,angle,layers in self.frames(progress,source_offset,target_offset,motion_progress):
            image=QPixmap(max(1,round(rect.width()*ratio)),max(1,round(rect.height()*ratio)))
            image.setDevicePixelRatio(ratio);image.fill(Qt.transparent)
            painter=QPainter(image);painter.setRenderHints(QPainter.Antialiasing|QPainter.SmoothPixmapTransform)
            paint_element(painter,QRectF(QPointF(),image.deviceIndependentSize()),0,layers);painter.end()
            result.append(Element(key,rect,image,angle,1.,layers[0][0].stretch))
        return result
