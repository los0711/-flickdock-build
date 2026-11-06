"""Identity-matched Qt presentation layers for the shelf's two layouts.

Only endpoint capture touches widgets. Frames paint cached layers; text keeps
its aspect ratio while card backgrounds change shape. An interrupted frame is
flattened per layer, so reversing never resurrects an old endpoint.
"""
from dataclasses import dataclass
import math

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap, QTransform

STACK_PARTS=(('thumbnail',QRectF(14,14,44,44)),('title',QRectF(14,77,164,20)),
             ('detail',QRectF(14,99,164,18)),('extension',QRectF(100,14,78,18)),
             ('progress',QRectF(162,40,16,16)))
STACK_CARD_WIDTH_PX=192
STACK_CARD_HEIGHT_PX=128
EMPTY_WIDTH_PX=176
EMPTY_TOP_PX=28
EMPTY_PARTS=(('symbol',QRectF(76,33,24,24)),('title',QRectF(1,69,174,24)))


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


def _parts(key, image, transform, opacity, fields, fill):
    """Split a card into a stretchable skin and aspect-preserving content."""
    skin=QPixmap(image)
    painter=QPainter(skin)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
    for _,rect in fields:
        painter.fillRect(rect,fill)
    painter.end()
    parts=[('skin',QRectF(QPointF(),image.deviceIndependentSize()),skin,True)]
    parts.extend((name,rect,crop(image,rect),False) for name,rect in fields)
    scale=(transform.m11()**2+transform.m12()**2)**.5
    angle=math.degrees(math.atan2(transform.m12(),transform.m11()))
    result=[]
    for name,rect,art,stretch in parts:
        center=transform.map(rect.center())
        bounds=QRectF(0,0,rect.width()*scale,rect.height()*scale);bounds.moveCenter(center)
        result.append(Element((*key,name),bounds,art,angle,opacity,stretch))
    return result


def capture(shelf, image):
    """Capture at most the five visible stack files, matched by repository ID."""
    from reference_widgets import palette
    result=[];card=shelf.card
    if shelf.expanded:
        popup=shelf.popup
        logo=popup.brand_logo
        # The target capture has already activated the layout. Restrict matches
        # to actual viewport pixels, never stale rows hidden by a category.
        viewport=popup.scroll.viewport();visible=_bounds(viewport,card)
        compact_refs=shelf.zones['permanent'].icon.refs
        for ref in compact_refs:
            widget=popup._item_widgets.get(ref)
            if widget is None or not widget.isVisible():continue
            bounds=_bounds(widget,card)
            if not visible.contains(bounds):continue
            art=crop(image,bounds)
            transform=QTransform().translate(bounds.x(),bounds.y())
            if hasattr(widget,'name_label'):
                fields=[('thumbnail',_bounds(widget.icon_label,widget)),
                        ('title',_bounds(widget.name_label,widget)),
                        ('detail',_bounds(widget.detail_label,widget)),
                        ('selection',_bounds(widget.selection_check,widget)),
                        ('remove',_bounds(widget.remove_button,widget))]
                highlighted=widget.motion.values['highlight']>.3
            else:
                width=widget.width();x=(width-44)//2
                lift=2*getattr(getattr(widget,'_dwell_hint',None),'value',0.)
                fields=[('thumbnail',QRectF(x,13-lift,44,44)),
                        ('title',QRectF(12,66,width-24,18)),
                        ('detail',QRectF(12,86,width-24,14)),
                        ('selection',QRectF(12,12,16,16)),
                        ('remove',_bounds(widget.remove_button,widget))]
                highlighted=widget.motion.values['hover']>.3
            colors=palette()
            fill=QColor(colors['file_card_selected'] if ref in popup.selected else
                        colors['file_card_hover'] if highlighted else colors['file_card'])
            result.extend(_parts(('file',ref),art,transform,1.,fields,fill))
        if not shelf.has_files() and popup.empty_drop.isVisible():
            bounds=_bounds(popup.empty_drop,card);art=crop(image,bounds)
            fields=[('symbol',_bounds(popup.empty_symbol,popup.empty_drop)),
                    ('title',_bounds(popup.empty_title,popup.empty_drop)),
                    ('hint',_bounds(popup.empty_hint,popup.empty_drop))]
            fill=art.toImage().pixelColor(art.width()//2,round(8*art.devicePixelRatioF()))
            result.extend(_parts(('empty','drop'),art,QTransform.fromTranslate(bounds.x(),bounds.y()),1.,fields,fill))
    else:
        logo=shelf.brand_logo;stack=shelf.zones['permanent'].icon
        origin=stack.mapTo(card,QPoint())
        for ref,transform,opacity,front in stack.presentation_cards():
            ratio=stack.devicePixelRatioF()
            art=QPixmap(round(STACK_CARD_WIDTH_PX*ratio),round(STACK_CARD_HEIGHT_PX*ratio));art.setDevicePixelRatio(ratio);art.fill(Qt.transparent)
            painter=QPainter(art);stack.paint_card(painter,ref,front);painter.end()
            # Qt composes transforms in row-vector order.
            transform=transform*QTransform.fromTranslate(origin.x(),origin.y())
            depth=len(stack.refs)-1-stack.refs.index(ref)
            result.extend(_parts(('file',ref),art,transform,opacity,STACK_PARTS,QColor(palette()['stack_cards'][depth])))
        if not stack.refs:
            bounds=QRectF(origin.x()+(stack.width()-EMPTY_WIDTH_PX)/2,origin.y()+EMPTY_TOP_PX,EMPTY_WIDTH_PX,STACK_CARD_HEIGHT_PX)
            art=crop(image,bounds)
            fill=art.toImage().pixelColor(art.width()//2,round(8*art.devicePixelRatioF()))
            result.extend(_parts(('empty','drop'),art,QTransform.fromTranslate(bounds.x(),bounds.y()),1.,EMPTY_PARTS,fill))
    bounds=_bounds(logo,card)
    result.append(Element(('brand',),bounds,crop(image,bounds)))
    clipboard=(shelf.popup.clipboard_button if shelf.expanded else shelf.zones['permanent'].clipboard_button)
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
    def __init__(self,source,target):
        self.source={item.key:item for item in source};self.target={item.key:item for item in target}
        # Only real file identities appearing at both endpoints participate.
        common={key[:2] for key in self.source if key[-1]=='skin'} & {
            key[:2] for key in self.target if key[-1]=='skin'}
        singles={('brand',),('clipboard',)} & self.source.keys() & self.target.keys()
        self.keys=[key for key in self.source if key in singles or key[:2] in common]
        self.keys.extend(key for key in self.target if key not in self.keys and key[:2] in common)
        self.roots=[key for key in self.keys if key in singles or key[-1]=='skin']

    def mask(self,source,offset):
        table=self.source if source else self.target
        path=QPainterPath();path.setFillRule(Qt.FillRule.WindingFill)
        for key in self.roots:
            if key in table:path.addPath(table[key].outline(offset))
        return path

    def frames(self,progress,source_offset,target_offset):
        for key in self.keys:
            first=self.source.get(key);last=self.target.get(key)
            # Accessories without a counterpart ride with their own card.
            start=first or self.source[(*key[:2],'skin')]
            end=last or self.target[(*key[:2],'skin')]
            if first is None:
                start_rect=QRectF(last.rect);start_rect.translate(start.rect.center()-end.rect.center())
            else:start_rect=QRectF(first.rect)
            if last is None:
                end_rect=QRectF(first.rect);end_rect.translate(end.rect.center()-start.rect.center())
            else:end_rect=QRectF(last.rect)
            start_rect.translate(source_offset);end_rect.translate(target_offset)
            rect=QRectF(*[_interpolate(a,b,progress) for a,b in zip(
                (start_rect.x(),start_rect.y(),start_rect.width(),start_rect.height()),
                (end_rect.x(),end_rect.y(),end_rect.width(),end_rect.height()))])
            angle=_interpolate(start.angle,end.angle,progress)
            layers=[]
            if first is not None:layers.append((first,first.opacity if last is not None else first.opacity*(1-progress)))
            if last is not None:layers.append((last,last.opacity*progress))
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
            result.append(Element(key,rect,image,angle,1.,key[-1]=='skin'))
        return result
