"""Passive clipboard-history panel attached to the compact shelf reveal."""
from __future__ import annotations

from collections import OrderedDict, deque

import math
import sys
import time

from PySide6.QtCore import QEasingCurve, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QAbstractTextDocumentLayout,QColor,QCursor,QFont,QFontMetrics,QFontMetricsF,QPainter,
    QPainterPath,QPalette,QPixmap,QRegion,QTextDocument,QTextOption)
from PySide6.QtWidgets import QWidget

from flickdock_icons import glyph_pixmap
from flickdock_motion import SHELF_SHADOW_BLUR, SHELF_SHADOW_OFFSET, Spring, shelf_shadow_color
from display_frames import DisplayAnimation, FramePulse
from native_ui_common import (RADIUS_CARD_PX, RADIUS_CONTROL_PX, RADIUS_WINDOW_PX, colors, paint_rounded_surface,
                              qss_color, reduced_motion)
from settings_navigation import (MAC_TRACKPAD_PULL_PX, SCROLL_BOUNCE_DURATION_MS, SCROLL_BOUNCE_IDLE_MS,
                                 scroll_pull_offset)
from theme_accent import current as current_accent
from ui_transitions import exit_curve
from surface_morph import paint_surface_shadow
from platform_native import keep_tool_visible_when_inactive, native_input_available, order_companion_behind, remove_host_window_shadow


ROW = 62
GAP = 8
HEADER = 54
INSET = 18
PEEK = 9
CARD_PADDING = 13
THUMB = 36
DETAIL_PAD = 9
DETAIL_MAX_HEIGHT = 146
IMAGE_DETAIL_HEIGHT = 110
FOLDED_HEIGHT = HEADER + ROW + PEEK * 2 + INSET
PANEL_HEIGHT_LIMIT = 520
SCREEN_EDGE_GAP = 22
SHADOW_GUTTER = 14
SHADOW_TAIL = 20
SHELF_GAP = 7
TUCK_INSET = 12
TUCK_TRAVEL = 36
HOVER_REBOUND_PX = 6.
REVEAL_REBOUND_PX = 5.
THUMBNAIL_CACHE_LIMIT = 96
WHEEL_GLIDE_SECONDS = .044


def _smoothstep(value):
    return value*value*(3-2*value)


class ClipboardHistoryPanel(QWidget):
    activated = Signal(str)
    pinned = Signal(str)
    removed = Signal(str)
    dragged = Signal(str)
    more_requested = Signal()
    clear_requested = Signal()
    preview_requested = Signal(str)

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.setAccessibleName('剪贴板历史')
        keep_tool_visible_when_inactive(self)
        self.entries = []
        self._row_indices = {}
        self.total = 0
        self.dark = False
        self.expanded = False
        self.selected = ''
        self.scroll = 0.
        self._wheel_events = deque()
        self._wheel_key = ''
        self._wheel_remaining = 0.
        self._wheel_at = 0.
        self._wheel_frames = FramePulse(self,follow_refresh_rate=True)
        self._wheel_frames.timeout.connect(self._scroll_frame)
        self._pull_offset = 0.
        self._pull_key = ''
        self._pull_lock = 0
        self._pull_idle = QTimer(self)
        self._pull_idle.setSingleShot(True)
        self._pull_idle.timeout.connect(self._release_pull)
        self._pull_return = DisplayAnimation(self)
        self._pull_return.setDuration(SCROLL_BOUNCE_DURATION_MS+65)
        self._pull_return.setEasingCurve(exit_curve())
        self._pull_return.setEndValue(0.)
        self._pull_return.valueChanged.connect(self._set_pull)
        self._stack_progress = 0.
        self._detail_ids={'opening':'','closing':''}
        self._detail_amounts={'opening':0.,'closing':0.}
        self._hovered = None
        self._press = None
        self._hits = []
        self._pixmaps = OrderedDict()
        self._fonts = {}
        self._captions = OrderedDict()
        self._palette_key = None
        self._palette = None
        self._icons = {}
        self._above = False
        self._anchor = QRectF()
        self._slot = QRectF()
        self._tuck_anchor = QRectF()
        self._panel_frame = QRectF()
        self._panel_scale = 1.
        self._companion_opacity = 1.
        self._native_opacity = native_input_available()
        self._shadow_gutter = 0
        self._shadow_tail = 0
        self._shadow_tiles = OrderedDict()
        self._shadow_color = shelf_shadow_color()
        self._companion_amount = 1.
        self._shelf_alpha = 1.
        self._companion_closing = False
        self._reveal_rebound = 0.
        self._reveal = DisplayAnimation(self)
        self._reveal.setDuration(340)
        self._reveal.setStartValue(0.)
        self._reveal.setEndValue(1.)
        self._reveal.valueChanged.connect(self._reveal_frame)
        self._details = {}
        self._detail_hits = {}
        self._feedback = ''
        self.can_load_more = False
        self._feedback_timer=QTimer(self)
        self._feedback_timer.setSingleShot(True)
        self._feedback_timer.timeout.connect(self._reset_feedback)
        self._confirm_clear=QTimer(self)
        self._confirm_clear.setSingleShot(True)
        self._confirm_clear.timeout.connect(self.update)
        self._stack = DisplayAnimation(self)
        self._stack.setDuration(350)
        # Ease through one rounded peak and spend the latter half settling.
        # Both segments have zero vertical velocity where they meet.
        self._hover_curve=QEasingCurve(QEasingCurve.BezierSpline)
        self._hover_curve.addCubicBezierSegment(QPointF(.12,.16),QPointF(.26,1.064),QPointF(.52,1.064))
        self._hover_curve.addCubicBezierSegment(QPointF(.68,1.064),QPointF(.84,1.),QPointF(1.,1.))
        self._stack.valueChanged.connect(self._stack_frame)
        self._preview = Spring(self,self._detail_amounts,self._preview_frame)
        self._entry_motion = DisplayAnimation(self)
        self._entry_motion.setDuration(180)
        self._entry_motion.setEasingCurve(exit_curve())
        self._entry_motion.valueChanged.connect(self._entry_frame)
        self._entry_motion.finished.connect(self._finish_entries)
        self._entry_progress = 1.
        self._entry_offsets = {}
        self._leaving = []
        self._height_offset = 0.
        self._painted = QRectF()
        self._mask_geometry = QRect()

    def set_entries(self,entries, *, total=None):
        rows = list(entries)
        count = len(rows) if total is None else total
        if rows==self.entries and count==self.total:
            return
        previous = {row['id']:row for row in self.entries}
        order_changed = list(previous)!=[row['id'] for row in rows]
        animate = order_changed and self.isVisible() and not reduced_motion()
        before = {row['id']:(row,rect,extra,opacity,index)
                  for index,row,rect,extra,opacity in self._rows_at_current_frame()} if animate else {}
        old_height = self._surface_height()
        self.entries = rows
        self._row_indices={row['id']:index for index,row in enumerate(rows)}
        self.total = count
        self.can_load_more = count>len(rows)
        fresh = {row['id']:row for row in rows}
        if self.selected and self.selected not in fresh:
            self._open('')
        if animate:
            self._animate_entries(before,old_height)
        elif order_changed:
            self._finish_entries()
        for key in fresh.keys() & previous.keys():
            if previous[key].get('thumbnail')!=fresh[key].get('thumbnail'):
                self._pixmaps.pop(key,None)
        self._trim_cache()
        self._update_mask()

    def _trim_cache(self):
        retained={row['id'] for row in self.entries}
        visible=retained|{row['id'] for row,*_ in self._leaving}
        self._pixmaps=OrderedDict((key,image) for key,image in self._pixmaps.items() if key in visible)
        self._details={key:detail for key,detail in self._details.items() if key in visible}

    def _animate_entries(self,before,old_height):
        self._entry_motion.stop()
        self._entry_offsets={}
        for index,row,rect,_ in self._layout_rows():
            key=row['id']
            if key in before:
                _,old,_,opacity,_=before[key]
                self._entry_offsets[key]=(old.y()-rect.y(),opacity)
            elif rect.top()<self._surface_height():
                self._entry_offsets[key]=(6.,0.)
        retained={row['id'] for row in self.entries}
        self._leaving=[(row,rect,extra,opacity,index)
                       for key,(row,rect,extra,opacity,index) in before.items()
                       if key not in retained and rect.bottom()>=HEADER and rect.top()<old_height]
        self._height_offset=old_height-self._ideal_surface_height()
        self._entry_motion.setStartValue(0.)
        self._entry_motion.setEndValue(1.)
        self._entry_motion.start()

    def _entry_frame(self,value):
        self._entry_progress=float(value)
        self._update_mask()

    def _finish_entries(self):
        self._entry_motion.stop()
        self._entry_progress=1.
        self._entry_offsets.clear()
        self._leaving.clear()
        self._height_offset=0.
        self._trim_cache()
        self._update_mask()

    def set_dark(self,dark):
        self.dark=bool(dark)
        if self._palette_key!=(self.dark,current_accent()):
            self._colors()
            self.update()

    def place(self,anchor,area,*,shelf_alpha=1.,motion=None,front=None):
        width=min(anchor.width(),area.width()-12)
        below=area.bottom()-SCREEN_EDGE_GAP-anchor.bottom()
        above=anchor.top()-area.top()-SCREEN_EDGE_GAP
        use_below=below-SHELF_GAP>=FOLDED_HEIGHT or below>=above
        limit=min(PANEL_HEIGHT_LIMIT+HOVER_REBOUND_PX,
                  (below if use_below else above)-SHELF_GAP-REVEAL_REBOUND_PX)
        if width<=0 or limit<HEADER+ROW+INSET:
            self.hide()
            return False
        x=max(area.left()+6,min(anchor.left(),area.right()-width-5))
        y=anchor.bottom()+1+SHELF_GAP if use_below else anchor.top()-SHELF_GAP-limit
        slot=QRect(x,int(y),width,int(limit))
        # Reserve both positions. The companion paints an intact rounded
        # surface throughout the transition, including behind the shelf.
        travel=anchor.adjusted(-TUCK_TRAVEL,-TUCK_TRAVEL,TUCK_TRAVEL,TUCK_TRAVEL)
        geometry=slot.united(travel).adjusted(-SHADOW_GUTTER,-SHADOW_GUTTER,SHADOW_GUTTER,SHADOW_TAIL)
        self._above=not use_below
        self._shadow_gutter=SHADOW_GUTTER
        self._shadow_tail=SHADOW_TAIL
        self._anchor=QRectF(anchor.translated(-geometry.topLeft()))
        self._tuck_anchor=QRectF(self._anchor)
        self._slot=QRectF(slot.translated(-geometry.topLeft()))
        if self.geometry()!=geometry:self.setGeometry(geometry)
        self.follow_shelf(shelf_alpha,False,motion=motion,front=front,placing=True)
        return True

    def follow_shelf(self,alpha,closing,*,motion=None,front=None,placing=False):
        # Both directions share the shelf clock. There is no delay at the
        # start, and a reversal reuses the same path and opacity.
        alpha=max(0.,min(1.,alpha))
        visible=self.isVisible()
        if visible and not placing and alpha:
            # The shelf spring can cross alpha=1 and briefly dip again. Keep
            # the companion's fade/slide monotonic within each direction.
            alpha=min(self._shelf_alpha,alpha) if closing else max(self._shelf_alpha,alpha)
        self._shelf_alpha=alpha
        amount=alpha*(1.18-.18*alpha)
        opacity=alpha*(.72+.28*alpha)
        tuck=QRectF(self._anchor)
        if motion is not None and not tuck.isEmpty():
            center=tuck.center()+QPointF(motion['dx'],motion['dy'])
            tuck.setWidth(tuck.width()*motion['sx'])
            tuck.setHeight(tuck.height()*motion['sy'])
            tuck.moveCenter(center)
        previous=self._companion_amount
        changed=(amount!=previous or closing!=self._companion_closing or tuck!=self._tuck_anchor)
        self._companion_amount=amount
        self._companion_closing=closing
        self._tuck_anchor=tuck
        if closing:
            self._press=None
            self._reveal.stop()
        if opacity!=self._companion_opacity:
            self._companion_opacity=opacity
            if self._native_opacity:self.setWindowOpacity(opacity)
        if not alpha:
            if self.isVisible():self.hide()
            return
        if changed or placing or not visible:self._update_mask()
        if not visible and not closing:
            self.show()
            remove_host_window_shadow(self)
            if front is not None:order_companion_behind(self,front)
            if alpha<1. and not reduced_motion():self._reveal.start()
        if previous<.92<=amount and not closing and self.entries and self.contains_point(QCursor.pos()) and not self.expanded:
            self.show_all()

    def _reveal_frame(self,value):
        t=float(value)
        if t<.46:
            weight=_smoothstep(t/.46)
        else:
            weight=1-_smoothstep((t-.46)/.54)
        self._reveal_rebound=REVEAL_REBOUND_PX*weight
        self._update_mask()

    def contains_point(self,point):
        if not self.isVisible():return False
        local=self.mapFromGlobal(point)
        return self.rect().contains(local) and self._painted.contains(self._body_position(QPointF(local)))

    def _paint_origin(self):
        return self._panel_frame.topLeft()

    def _body_position(self,point):
        return (point-self._paint_origin())/self._panel_scale

    def _content_width(self):
        return self._slot.width() if not self._slot.isEmpty() else self.width()

    def _content_top(self):
        return self._panel_frame.top()

    def _frame_for(self,width,height):
        if self._slot.isEmpty():
            return QRectF(0,0,width,height),1.
        amount=self._companion_amount
        y=self._slot.bottom()-height if self._above else self._slot.top()
        rebound=self._reveal_rebound*amount**3*(-1 if self._above else 1)
        if amount==1. or not height:
            return QRectF(self._slot.left(),y+rebound,width,height),1.
        tuck=self._tuck_anchor
        inset=min(TUCK_INSET,tuck.height()/6)
        scale=min(1.,max(.1,(tuck.height()-2*inset)/height),tuck.width()/width*.96)
        tucked_y=tuck.top()+inset if self._above else tuck.bottom()-inset-height*scale
        x=tuck.center().x()-width*scale/2
        origin=QPointF(x,tucked_y)*(1-amount)+QPointF(self._slot.left(),y)*amount+QPointF(0.,rebound)
        scale=scale+(1-scale)*amount
        return QRectF(origin.x(),origin.y(),width*scale,height*scale),scale

    def _stack_frame(self,value):
        self._stack_progress=float(value)
        if not self.expanded and self._stack_progress==0.:self.scroll=0.
        self._update_mask()

    def _spread(self):
        return min(1.,max(0.,self._stack_progress))

    def _hover_rebound(self):
        excess=self._stack_progress-self._spread()
        return max(-HOVER_REBOUND_PX,min(HOVER_REBOUND_PX,excess*90.))

    def _preview_frame(self,values):
        self._detail_amounts=values
        self._update_mask()

    @property
    def _preview_progress(self):
        return self._detail_amounts['opening']

    def _detail_progress(self,key):
        for channel,identity in self._detail_ids.items():
            if identity==key:
                return min(1.,max(0.,self._detail_amounts[channel]))
        return 0.

    def _detail_height(self,row,width):
        if row['kind']=='image':
            pixmap=self._entry_image(row)
            return min(IMAGE_DETAIL_HEIGHT,width*pixmap.height()/pixmap.width()) if not pixmap.isNull() else IMAGE_DETAIL_HEIGHT
        detail=self._details.get(row['id'])
        if detail is None:
            return 36
        document,_=detail
        line_width=max(1.,width-DETAIL_PAD*2)
        if document.textWidth()!=line_width:
            document.setTextWidth(line_width)
        return min(DETAIL_MAX_HEIGHT,math.ceil(document.size().height())+DETAIL_PAD*2)

    def _preview_extra(self,row):
        progress=self._detail_progress(row['id'])
        width=max(1.,self._content_width()-2*(INSET+CARD_PADDING))
        return progress*(self._detail_height(row,width)+CARD_PADDING) if progress else 0.

    def _body_height(self):
        count=len(self.entries)
        if not count:
            return 38
        extra=sum(self._detail_extras().values())
        return ROW*count+GAP*(count-1)+extra+(GAP+25 if self.can_load_more else 0)

    def _detail_extras(self):
        indices=(self._row_indices[key] for key in set(self._detail_ids.values()) if key in self._row_indices)
        return {index:self._preview_extra(self.entries[index]) for index in indices}

    def _ideal_surface_height(self):
        count=len(self.entries)
        if not count:
            return min(self._paint_limit(),HEADER+38+INSET)
        folded=HEADER+ROW+PEEK*min(count-1,2)+INSET
        opened=HEADER+self._body_height()+INSET
        rest_limit=min(PANEL_HEIGHT_LIMIT,self._paint_limit()-HOVER_REBOUND_PX)
        return min(rest_limit,folded+(opened-folded)*self._spread())+self._hover_rebound()

    def _paint_limit(self):
        return self._slot.height() if not self._slot.isEmpty() else self.height()

    def _surface_height(self):
        return max(0.,min(self._paint_limit(),self._ideal_surface_height()+self._height_offset*(1-self._entry_progress)))

    def _update_mask(self,*,repaint=True):
        height=self._surface_height()
        self.scroll=min(self.scroll,max(0.,self._body_height()-max(0.,height-HEADER-INSET)))
        self._painted=QRectF(0,0,self._content_width(),height)
        self._panel_frame,self._panel_scale=self._frame_for(self._painted.width(),height)
        path_bounds=self._panel_frame.toAlignedRect()
        old=self._mask_geometry
        if path_bounds!=old:
            self._mask_geometry=path_bounds
            path=QPainterPath()
            # Include the antialias fringe and the soft shadow around the
            # transformed panel without accepting input in the unused host.
            gutter=max(2,self._shadow_gutter)
            radius=RADIUS_WINDOW_PX*self._panel_scale+gutter/2
            path.addRoundedRect(QRectF(path_bounds).adjusted(-gutter,-gutter,gutter,max(gutter,self._shadow_tail)),
                                radius,radius)
            self.setMask(QRegion(path.toFillPolygon().toPolygon()))
        if repaint:
            self.update(path_bounds.united(old).adjusted(-SHADOW_GUTTER,-SHADOW_GUTTER,SHADOW_GUTTER,SHADOW_TAIL))

    def resizeEvent(self,event):
        self._update_mask()
        super().resizeEvent(event)

    def hideEvent(self,event):
        self._stack.stop()
        self._reveal.stop()
        self._reveal_rebound=0.
        self._wheel_frames.stop()
        self._wheel_events.clear()
        self._wheel_remaining=0.
        self._reset_pull()
        self._stack_progress=0.
        self._finish_entries()
        self._detail_ids={'opening':'','closing':''}
        self._preview.jump({'opening':0,'closing':0})
        self.expanded=False
        self.selected=''
        self.scroll=0.
        self._confirm_clear.stop()
        self._feedback_timer.stop()
        self._feedback=''
        self._hovered=self._press=None
        self._details.clear()
        super().hideEvent(event)

    def show_all(self,expanded=True):
        if expanded==self.expanded:return
        self.expanded=expanded
        if not expanded:
            self._wheel_frames.stop()
            self._wheel_events.clear()
            self._wheel_remaining=0.
            self._reset_pull()
            self._open('')
        target=1. if expanded else 0.
        self._stack.stop()
        if reduced_motion():
            self._stack_frame(target)
        else:
            self._stack.setEasingCurve(self._hover_curve)
            self._stack.setStartValue(self._stack_progress)
            self._stack.setEndValue(target)
            self._stack.start()

    def _open(self,key):
        if key and key!=self._detail_ids['opening']:
            restart=self._detail_amounts['closing'] if key==self._detail_ids['closing'] else 0.
            closing=self._detail_amounts['opening']
            self._detail_ids['closing']=self._detail_ids['opening']
            self._detail_ids['opening']=key
            self._preview.jump({'opening':restart,'closing':closing})
        self.selected=key
        target={'opening':1 if key else 0,'closing':0}
        if reduced_motion():self._preview.jump(target)
        else:self._preview.animate_physical(target,300,26)

    def _colors(self):
        key=(self.dark,current_accent())
        if self._palette_key!=key:
            self._palette_key=key
            theme=colors('dark' if self.dark else 'light')
            self._palette={name:qss_color(theme[name]) for name in
                    ('window_backdrop','glass_border','card','border','input_border','text','secondary',
                     'control_hover','action','subcard','file_card','file_card_hover','file_card_selected',
                     'selected_border','hover_border')}
        return self._palette

    @staticmethod
    def _rounded(painter,rect,fill,outline,radius):
        paint_rounded_surface(painter,rect,radius,fill,outline)

    def _font(self, size, bold=False):
        key=(size,bold)
        if key not in self._fonts:
            font=QFont(self.font());font.setPixelSize(size)
            font.setWeight(QFont.Weight.DemiBold if bold else QFont.Weight.Normal)
            self._fonts[key]=font
        return self._fonts[key]

    def _caption(self,painter,text,rect,color,*,size=12,bold=False,align=Qt.AlignLeft):
        font=self._font(size,bold);painter.setFont(font);painter.setPen(QColor(color))
        width=max(0,math.floor(rect.width()))
        key=(text,width,size,bold)
        value=self._captions.get(key)
        if value is None:
            value=QFontMetrics(font).elidedText(text,Qt.ElideRight,width)
            self._captions[key]=value
            if len(self._captions)>480:self._captions.popitem(last=False)
        else:self._captions.move_to_end(key)
        painter.drawText(rect,Qt.AlignVCenter|align,value)

    def _glyph(self,painter,key,rect,color):
        size=round(min(rect.width(),rect.height()))
        dpr=self.devicePixelRatioF()
        cache_key=(key,size,color,dpr)
        if cache_key not in self._icons:self._icons[cache_key]=glyph_pixmap(key,size,color,1.6,dpr)
        pixmap=self._icons[cache_key]
        painter.drawPixmap(rect,pixmap,QRectF(pixmap.rect()))

    def _entry_image(self,row):
        key=row['id']
        if key not in self._pixmaps:
            pixmap=QPixmap()
            if row.get('thumbnail'):
                pixmap.loadFromData(row['thumbnail'])
            self._pixmaps[key]=pixmap
            if len(self._pixmaps)>THUMBNAIL_CACHE_LIMIT:self._pixmaps.popitem(last=False)
        else:self._pixmaps.move_to_end(key)
        return self._pixmaps[key]

    def set_detail(self,key,text):
        if key not in self._detail_ids.values():return
        document=QTextDocument()
        document.setDocumentMargin(0)
        document.setDefaultFont(self._font(12))
        options=document.defaultTextOption();options.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        document.setDefaultTextOption(options)
        preview=text[:12000]
        document.setPlainText(preview+('\n…' if len(text)>len(preview) else ''))
        self._details={item:detail for item,detail in self._details.items() if item in self._detail_ids.values()}
        self._details[key]=[document,0.]
        self._update_mask()

    def _draw_text_detail(self,painter,key,viewport,ink,muted,clip):
        detail=self._details.get(key)
        if detail is None or viewport.isEmpty():
            return
        document,scroll=detail
        total=document.size().height()
        maximum=max(0.,total-viewport.height())
        scroll=max(0.,min(maximum,scroll));detail[1]=scroll
        self._detail_hits[key]=(viewport.intersected(clip),maximum)
        context=QAbstractTextDocumentLayout.PaintContext()
        context.palette.setColor(QPalette.Text,ink)
        painter.save()
        painter.setClipRect(clip,Qt.IntersectClip)
        painter.setClipRect(viewport,Qt.IntersectClip)
        pull=self._pull_offset if self._pull_key==key else 0.
        painter.translate(viewport.x(),viewport.y()-scroll+pull)
        context.clip=QRectF(0,scroll-pull,viewport.width(),viewport.height())
        document.documentLayout().draw(painter,context)
        painter.restore()
        if maximum>0:
            height=max(10.,viewport.height()*viewport.height()/total)
            y=viewport.y()+(viewport.height()-height)*scroll/maximum
            painter.save();painter.setClipRect(clip,Qt.IntersectClip)
            self._rounded(painter,QRectF(viewport.right()+3,y,2,height),muted,None,1)
            painter.restore()

    def feedback(self,message):
        self._feedback=message
        self._feedback_timer.start(2200)
        self.update()

    def _reset_feedback(self):
        self._feedback=''
        self.update()

    @staticmethod
    def _between(before,after,progress):
        return QRectF(*(a+(b-a)*progress for a,b in zip(before.getRect(),after.getRect())))

    @staticmethod
    def _draw_thumbnail(painter,thumb,image,radius):
        clip=QPainterPath();clip.addRoundedRect(thumb,radius,radius)
        image_rect=QRectF(image.rect())
        scale=max(thumb.width()/image_rect.width(),thumb.height()/image_rect.height())
        crop=QRectF(0,0,thumb.width()/scale,thumb.height()/scale)
        crop.moveCenter(image_rect.center())
        painter.save();painter.setClipPath(clip,Qt.IntersectClip)
        painter.drawPixmap(thumb,image,crop)
        painter.restore()

    def _card(self,painter,row,rect,extra,palette,*,content=1.,interactive=True):
        key=row['id']
        hovered=interactive and self._hovered is not None and self._hovered[1]==key
        fill='file_card_selected' if self.selected==key else 'file_card_hover' if hovered else 'file_card'
        border='selected_border' if self.selected==key else 'hover_border' if hovered else 'border'
        ink,muted=palette['text'],palette['secondary']
        line=palette['border']
        self._rounded(painter,rect,palette[fill],palette[border],RADIUS_CARD_PX)
        if not content:return
        alpha=painter.opacity()
        painter.setOpacity(alpha*content)
        thumb=QRectF(rect.x()+CARD_PADDING,rect.y()+(ROW-THUMB)/2,THUMB,THUMB)
        image=self._entry_image(row)
        progress=self._detail_progress(key) if extra else 0.
        title_x=thumb.right()+9-(THUMB+9)*progress
        if progress:
            detail_width=rect.width()-2*CARD_PADDING
            height=self._detail_height(row,detail_width)
            final=QRectF(rect.x()+CARD_PADDING,rect.top()+ROW,detail_width,height)
            if not image.isNull():
                final.setWidth(min(detail_width,height*image.width()/image.height()))
                final.moveCenter(QPointF(rect.center().x(),rect.top()+ROW+height/2))
            visible=self._between(thumb,final,progress)
            radius=RADIUS_CONTROL_PX+(RADIUS_CARD_PX-RADIUS_CONTROL_PX)*progress
            if image.isNull():
                self._rounded(painter,visible,palette['subcard'],line,radius)
                glyph={'text':'text','link':'link','image':'image','file':'file'}.get(row['kind'],'text')
                detail_alpha=painter.opacity()
                if progress<.65:
                    painter.setOpacity(detail_alpha*(1-progress/.65))
                    glyph_box=QRectF(0,0,16,16);glyph_box.moveCenter(visible.center())
                    self._glyph(painter,glyph,glyph_box,muted.name())
                painter.setOpacity(detail_alpha*min(1.,max(0.,(progress-.30)/.55)))
                if progress>.30:
                    self._draw_text_detail(painter,key,final.adjusted(DETAIL_PAD,DETAIL_PAD,-DETAIL_PAD,-DETAIL_PAD),
                                           ink,muted,visible.adjusted(1,1,-1,-1))
                painter.setOpacity(detail_alpha)
            else:
                self._draw_thumbnail(painter,visible,image,radius)
        else:
            visible=thumb
            if image.isNull():
                self._rounded(painter,thumb,palette['subcard'],line,RADIUS_CONTROL_PX)
                glyph={'text':'text','link':'link','image':'image','file':'file'}.get(row['kind'],'text')
                self._glyph(painter,glyph,thumb.adjusted(10,10,-10,-10),muted.name())
            else:
                self._draw_thumbnail(painter,thumb,image,RADIUS_CONTROL_PX)
        pin=QRectF(rect.right()-55,rect.y()+(ROW-23)/2,23,23)
        remove=QRectF(rect.right()-29,pin.y(),23,23)
        title=QRectF(title_x,rect.y()+(ROW-34)/2,pin.left()-title_x-4,18)
        meta=QRectF(title.x(),title.bottom(),title.width(),16)
        self._caption(painter,row['title'],title,ink,size=13,bold=True)
        label={'text':'文字','link':'链接','image':'图片','file':'文件引用'}[row['kind']]
        stamp=time.strftime('%m-%d %H:%M',time.localtime(row['updated']))
        self._caption(painter,label+' · '+stamp,meta,muted,size=10)
        for part,box,glyph in (('pin',pin,'pin-filled' if row['pinned'] else 'pin'),('remove',remove,'close')):
            if self._hovered==(part,key):
                self._rounded(painter,box,palette['control_hover'],None,7)
            tint=palette['action'] if part=='pin' and row['pinned'] else muted
            self._glyph(painter,glyph,box.adjusted(5,5,-5,-5),tint.name())
        painter.setOpacity(alpha)
        if interactive:
            self._hits.extend([('thumb',key,visible),('pin',key,pin),('remove',key,remove),('card',key,rect)])

    def _layout_rows(self,*,windowed=False):
        y=HEADER-self.scroll+(0. if self._pull_key else self._pull_offset)
        width=self._content_width()-INSET*2
        progress=self._spread()
        rebound=self._hover_rebound()
        extras=self._detail_extras()
        pitch=ROW+GAP
        first,last=0,len(self.entries)
        if windowed:
            if not progress:last=min(last,3)
            elif progress==1. and not self._entry_offsets:
                # Account for the at most two expanded details before the
                # viewport while bounding the rows visited during a scroll.
                first=max(0,int((HEADER-y-sum(extras.values()))//pitch))
                last=min(last,max(first,math.ceil((self._painted.bottom()-INSET-y)/pitch)+1))
                y+=first*pitch+sum(extra for index,extra in extras.items() if index<first)
        for index in range(first,last):
            row=self.entries[index]
            extra=extras.get(index,0.)
            inset=min(index,2)*5*(1-progress)
            top=(HEADER+min(index,2)*PEEK)*(1-progress)+y*progress+min(index,2)*rebound/2
            yield index,row,QRectF(INSET+inset,top,width-inset*2,ROW+extra),extra
            y+=pitch+extra

    def _rows_at_current_frame(self,*,windowed=False):
        remaining=1-self._entry_progress
        for index,row,rect,extra in self._layout_rows(windowed=windowed):
            offset,opacity=self._entry_offsets.get(row['id'],(0.,1.))
            yield index,row,rect.translated(0,offset*remaining),extra,1-(1-opacity)*remaining

    def _scroll_rect(self):
        width,height=self._painted.width(),self._painted.height()
        return QRectF(3,HEADER,max(0.,width-6),max(0.,height-HEADER-INSET+1))

    def _update_scroll_body(self,*,on_display_frame=False):
        rect=self._scroll_rect()
        scale=self._panel_scale
        origin=self._paint_origin()
        dirty=QRectF(origin.x()+rect.x()*scale,origin.y()+rect.y()*scale,rect.width()*scale,rect.height()*scale).toAlignedRect()
        clock=self._wheel_frames.clock
        if on_display_frame and clock is not None and clock.display_period():
            # Paint the viewport in the native cadence callback so a
            # background tool window's queued update cannot halve the rate.
            self.repaint(dirty)
        else:
            self.update(dirty)

    def paintEvent(self,event):
        painter=QPainter(self);painter.setRenderHints(QPainter.Antialiasing|QPainter.SmoothPixmapTransform)
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        painter.fillRect(event.rect(),Qt.transparent)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        painter.translate(self._paint_origin())
        painter.scale(self._panel_scale,self._panel_scale)
        self._hits=[]
        self._detail_hits={}
        palette=self._colors()
        ink,muted=palette['text'],palette['secondary']
        scroll_rect=self._scroll_rect()
        if self._panel_scale==1. and scroll_rect.translated(self._paint_origin()).toAlignedRect().contains(event.rect()):
            painter.fillRect(scroll_rect,palette['window_backdrop'])
        else:
            paint_surface_shadow(painter,self._painted,self._shadow_tiles,SHELF_SHADOW_BLUR,
                                 SHELF_SHADOW_OFFSET,self._shadow_color,radius=RADIUS_WINDOW_PX)
            self._rounded(painter,self._painted,palette['window_backdrop'],palette['glass_border'],RADIUS_WINDOW_PX)
        width=self._content_width()
        title=QRectF(INSET,(HEADER-28)/2,160,28)
        message='再次点击清空历史' if self._confirm_clear.isActive() else self._feedback or '剪贴板历史'
        self._caption(painter,message,title,ink,size=13,bold=True)
        clear=QRectF(width-INSET-28,title.y(),28,28)
        if message=='剪贴板历史':
            count=f'{self.total} 项'
            heading_metrics=QFontMetricsF(self._font(13,True))
            count_font=self._font(11,True)
            count_metrics=QFontMetricsF(count_font)
            start=title.left()+heading_metrics.horizontalAdvance(message)+8
            advance=count_metrics.horizontalAdvance(count)
            badge=QRectF(start,title.center().y()-10,advance+16,20)
            self._rounded(painter,badge,palette['subcard'] if self.dark else palette['card'],palette['input_border'],10)
            baseline=title.center().y()+(heading_metrics.ascent()-heading_metrics.descent())/2
            painter.setFont(count_font);painter.setPen(ink)
            painter.drawText(QPointF(start+8,baseline),count)
        if self.entries:
            if self._hovered==('clear','') or self._confirm_clear.isActive():
                self._rounded(painter,clear,palette['control_hover'],None,9)
            self._glyph(painter,'trash',clear.adjusted(7,6,-7,-6),'#d33138' if self._confirm_clear.isActive() else muted.name())
            self._hits.append(('clear','',clear))
        painter.save()
        painter.setClipRect(QRectF(1,HEADER,width-2,max(0.,self._painted.height()-HEADER-INSET+1)))
        if not self.entries:
            painter.setOpacity(self._entry_progress)
            self._caption(painter,'暂无剪贴板历史',QRectF(14,HEADER,width-28,38),muted,align=Qt.AlignHCenter)
        progress=self._entry_progress
        spread=self._spread()
        for row,rect,extra,opacity,index in self._leaving:
            painter.setOpacity(opacity*(1-progress))
            departure=rect.translated(0,-5*progress).adjusted(2*progress,0,-2*progress,0)
            self._card(painter,row,departure,extra,palette,content=1. if index==0 else spread,interactive=False)
        for index,row,rect,extra,opacity in reversed(list(self._rows_at_current_frame(windowed=True))):
            if rect.bottom()<HEADER or rect.top()>=self._painted.bottom()-INSET:continue
            if index>2:opacity*=min(1.,spread*2.5)
            if not opacity:continue
            painter.setOpacity(opacity)
            detail=1. if index==0 else min(1.,max(0.,(spread-.35)/.65))
            self._card(painter,row,rect,extra,palette,content=detail,interactive=index==0 or spread>=.99)
        painter.setOpacity(1.)
        available=self._painted.height()-HEADER-INSET
        total=self._body_height()
        if self.can_load_more and self._stack_progress>.95:
            more=QRectF(INSET,HEADER+total-25-self.scroll,width-2*INSET,25)
            self._caption(painter,'加载更多',more,muted,size=11,align=Qt.AlignHCenter)
            self._hits.append(('more','',more))
        if self._stack_progress>.95 and total>available+1 and available>20:
            height=max(16.,available*available/total)
            y=HEADER+(available-height)*self.scroll/(total-available)
            self._rounded(painter,QRectF(width-7,y,3,height),muted,None,1.5)
        painter.restore();painter.end()

    def _hit(self,position):
        if self._companion_closing or self._companion_amount<.92:return None
        position=self._body_position(position)
        if not self._painted.contains(position) or position.y()>self._painted.bottom()-INSET:return None
        return next(((part,key) for part,key,rect in self._hits if rect.contains(position)),None)

    def mouseMoveEvent(self,event):
        hit=self._hit(event.position())
        if self._press is not None and self._press[0][0] in ('card','thumb'):
            (_,key),anchor=self._press
            if (event.position()-anchor).manhattanLength()>8:
                self._press=None
                self.dragged.emit(key)
        if hit!=self._hovered:
            self._hovered=hit
            self.setCursor(Qt.PointingHandCursor if hit else Qt.ArrowCursor)
            part,key=hit if hit else ('','')
            tip={'clear':'清空历史','thumb':'预览内容','remove':'删除此条','pin':'切换置顶'}.get(part,'')
            self.setToolTip(tip)
            self.update()

    def enterEvent(self,event):
        if self.entries and not self.expanded and self._companion_amount>=.92 and not self._companion_closing:
            self.show_all()
        super().enterEvent(event)

    def leaveEvent(self,event):
        self._hovered=None
        if self.expanded and not self._companion_closing:self.show_all(False)
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self,event):
        hit=self._hit(event.position())
        self._press=(hit,event.position()) if hit and event.button()==Qt.LeftButton else None
        event.accept()

    def mouseReleaseEvent(self,event):
        press,self._press=self._press,None
        hit=self._hit(event.position())
        if not press or hit!=press[0]:return
        part,key=hit
        if part=='clear':
            if self._confirm_clear.isActive():
                self._confirm_clear.stop()
                self.clear_requested.emit()
            else:self._confirm_clear.start(3000)
            self.update()
        elif part=='thumb':
            if not self.expanded:self.show_all()
            self._open('' if key==self.selected else key)
            if key==self.selected:
                row=next((row for row in self.entries if row['id']==key),None)
                if row and row['kind']!='image':self.set_detail(key,row.get('excerpt',''))
                self.preview_requested.emit(key)
        elif part=='pin':self.pinned.emit(key)
        elif part=='remove':self.removed.emit(key)
        elif part=='more':self.more_requested.emit()
        else:self.activated.emit(key)

    def _set_pull(self,value):
        self._pull_offset=float(value)
        if not self._wheel_frames.isActive():self._update_scroll_body()

    def _reset_pull(self):
        self._pull_idle.stop()
        self._pull_return.stop()
        self._pull_offset=0.
        self._pull_key=''
        self._pull_lock=0

    def _release_pull(self):
        self._pull_idle.stop()
        if not self._pull_offset or self._pull_return.state()==DisplayAnimation.Running:
            return
        self._pull_return.setStartValue(self._pull_offset)
        self._pull_return.start()

    def _scroll_motion(self,key,current,limit,delta,phase):
        if key!=self._pull_key:
            self._reset_pull()
            self._pull_key=key
        if phase==Qt.ScrollBegin:self._pull_lock=0
        if phase in (Qt.ScrollEnd,Qt.ScrollMomentum):
            self._release_pull()
            if phase==Qt.ScrollEnd:self._pull_lock=0
        if not delta:return current
        if self._pull_offset*delta<0 and self._pull_return.state()!=DisplayAnimation.Running:
            offset=scroll_pull_offset(self._pull_offset,delta)
            self._set_pull(offset if offset*self._pull_offset>0 else 0.)
            if not self._pull_offset:self._pull_lock=0
            else:self._pull_idle.start(SCROLL_BOUNCE_IDLE_MS)
            return current
        landed=max(0.,min(limit,current-delta))
        excess=delta+landed-current
        direction=1 if delta>0 else -1
        if not excess or reduced_motion() or phase in (Qt.ScrollEnd,Qt.ScrollMomentum):
            if landed!=current and self._pull_offset:self._reset_pull()
            return landed
        if self._pull_lock==direction:return landed
        self._pull_return.stop()
        precision=sys.platform=='darwin' and phase!=Qt.NoScrollPhase
        threshold=MAC_TRACKPAD_PULL_PX if precision else 24.
        self._set_pull(scroll_pull_offset(self._pull_offset,excess,threshold))
        if precision and abs(self._pull_offset)>=threshold*.85:
            self._pull_lock=direction
            self._release_pull()
        else:
            self._pull_idle.start(SCROLL_BOUNCE_IDLE_MS)
        return landed

    def _scroll_delta(self,key,delta,phase):
        if key:
            detail=self._details.get(key)
            viewport=self._detail_hits.get(key)
            if detail is not None and viewport is not None:
                detail[1]=self._scroll_motion(key,detail[1],viewport[1],delta,phase)
        else:
            maximum=max(0.,self._body_height()-max(0.,self._painted.height()-HEADER-INSET))
            if maximum:self.scroll=self._scroll_motion('',self.scroll,maximum,delta,phase)

    def _scroll_frame(self):
        now=time.perf_counter()
        while self._wheel_events:
            key,delta,phase,notch=self._wheel_events.popleft()
            if notch and not reduced_motion():
                if self._wheel_remaining and key!=self._wheel_key:
                    self._scroll_delta(self._wheel_key,self._wheel_remaining,Qt.NoScrollPhase)
                    self._wheel_remaining=0.
                self._wheel_key=key
                self._wheel_remaining+=delta
            else:
                self._scroll_delta(key,delta,phase)
        if self._wheel_remaining:
            step=self._wheel_remaining*(1-math.exp(-min(.05,now-self._wheel_at)/WHEEL_GLIDE_SECONDS))
            if abs(self._wheel_remaining)<.85:step=self._wheel_remaining
            self._wheel_remaining-=step
            self._scroll_delta(self._wheel_key,step,Qt.NoScrollPhase)
        self._wheel_at=now
        self._update_scroll_body(on_display_frame=True)
        if not self._wheel_remaining:self._wheel_frames.stop()

    def wheelEvent(self,event):
        if self._companion_closing or self._companion_amount<.92:
            event.ignore();return
        if not self.expanded:self.show_all()
        pixels=event.pixelDelta().y()
        delta=pixels or event.angleDelta().y()*.37
        phase=event.phase()
        position=self._body_position(event.position())
        key=next((key for key,(viewport,limit) in self._detail_hits.items()
                  if limit and viewport.contains(position) and key in self._details),'')
        notch=not pixels and phase==Qt.NoScrollPhase
        if self._wheel_events and self._wheel_events[-1][0]==key and self._wheel_events[-1][2:]==(phase,notch):
            _,prior,_,_=self._wheel_events.pop()
            delta+=prior
        self._wheel_events.append((key,delta,phase,notch))
        if not self._wheel_frames.isActive():
            clock=getattr(self.window(),'_display_frames',None)
            period=clock.display_period() if clock is not None else None
            self._wheel_at=time.perf_counter()-(period or 1/60)
            self._wheel_frames.start()
        event.accept()
