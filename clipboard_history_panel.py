"""Passive clipboard-history panel attached to the compact shelf reveal."""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QEasingCurve, QPointF, QRect, QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import (QAbstractTextDocumentLayout,QColor,QFont,QFontMetrics,QPainter,
    QPainterPath,QPalette,QPen,QPixmap,QTextDocument,QTextOption)
from PySide6.QtWidgets import QWidget

from flickdock_icons import glyph_pixmap
from flickdock_motion import Spring
from native_ui_common import RADIUS_WINDOW_PX, reduced_motion
from platform_native import keep_tool_visible_when_inactive, remove_host_window_shadow


ROW = 70
GAP = 7
HEADER = 48
INSET = 13
PEEK = 12
CARD_PADDING = 11
DETAIL_HEIGHT = 151


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
        self.dark = False
        self.expanded = False
        self.selected = ''
        self.scroll = 0.
        self._stack_progress = 0.
        self._detail_ids={'opening':'','closing':''}
        self._detail_amounts={'opening':0.,'closing':0.}
        self._hovered = None
        self._press = None
        self._hits = []
        self._pixmaps = {}
        self._fonts = {}
        self._icons = {}
        self._above = False
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
        self._stack = QVariantAnimation(self)
        self._stack.setDuration(300)
        easing = QEasingCurve(QEasingCurve.Type.BezierSpline)
        easing.addCubicBezierSegment(QPointF(.23,1),QPointF(.32,1),QPointF(1,1))
        self._stack.setEasingCurve(easing)
        self._stack.valueChanged.connect(self._stack_frame)
        self._preview = Spring(self,self._detail_amounts,self._preview_frame)
        self._painted = QRectF()

    def set_entries(self,entries, *, has_more=False):
        self.entries = list(entries)
        self.can_load_more=has_more
        retained={row['id'] for row in self.entries}
        self._pixmaps={key:image for key,image in self._pixmaps.items() if key in retained}
        self._details={key:detail for key,detail in self._details.items() if key in retained}
        if self.selected and self.selected not in retained:self._open('')
        self.update()

    def set_dark(self,dark):
        if self.dark!=bool(dark):
            self.dark=bool(dark)
            self.update()

    def place(self,anchor,area):
        width=min(anchor.width(),area.width()-12)
        below=area.bottom()-anchor.bottom()-8
        above=anchor.top()-area.top()-8
        use_below=below>=155 or below>=above
        height=max(0,below if use_below else above)
        if height<HEADER+ROW+INSET:
            self.hide()
            return
        x=max(area.left()+6,min(anchor.left(),area.right()-width-5))
        y=anchor.bottom()+7 if use_below else anchor.top()-height-7
        geometry=QRect(x,y,width,height)
        above_anchor=not use_below
        changed_side=self._above!=above_anchor
        self._above=above_anchor
        if self.geometry()!=geometry:self.setGeometry(geometry)
        elif changed_side:self._update_mask()
        if not self.isVisible():
            self.show()
            remove_host_window_shadow(self)
            self._update_mask()

    def contains_point(self,point):
        position=QPointF(self.mapFromGlobal(point))-QPointF(0,self._content_top())
        return self.isVisible() and self._painted.contains(position)

    def _content_top(self):
        return self.height()-self._surface_height() if self._above else 0.

    def _stack_frame(self,value):
        self._stack_progress=float(value)
        self._update_mask()

    def _preview_frame(self,values):
        self._detail_amounts=values
        self._update_mask()

    @property
    def _preview_progress(self):
        return self._detail_amounts['opening']

    def _preview_extra(self,row):
        amount=next((self._detail_amounts[channel] for channel,key in self._detail_ids.items() if key==row['id']),0.)
        return min(1.,max(0.,amount))*(110 if row['kind']=='image' else DETAIL_HEIGHT)

    def _body_height(self):
        return sum(ROW+GAP+self._preview_extra(row) for row in self.entries)+(ROW//2 if self.can_load_more else 0)

    def _surface_height(self):
        folded=HEADER+(ROW+PEEK*2 if self.entries else 53)+INSET
        full=self.height() if self.entries else HEADER+53+INSET
        return min(self.height(),folded+(full-folded)*self._stack_progress)

    def _update_mask(self):
        from PySide6.QtGui import QRegion
        self._painted=QRectF(.5,.5,self.width()-1,self._surface_height()-1)
        path=QPainterPath()
        path.addRoundedRect(self._painted.translated(0,self._content_top()),RADIUS_WINDOW_PX,RADIUS_WINDOW_PX)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))
        self.update()

    def resizeEvent(self,event):
        self._update_mask()
        super().resizeEvent(event)

    def hideEvent(self,event):
        self._stack.stop()
        self._stack_progress=0.
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
        self.expanded=expanded
        if not expanded:
            self.scroll=0.
            self._open('')
        target=1. if expanded else 0.
        self._stack.stop()
        if reduced_motion():
            self._stack_frame(target)
        else:
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
        else:self._preview.animate_physical(target,240,21)

    def _colors(self):
        if self.dark:
            return ('#101113','#1c1d21','#2f3137','#f1f2f5','#a8aeba','#292b31','#eab64f','#242530')
        return ('#e3e4e8','#f7f7f9','#cacdd5','#15171d','#565d69','#e6e7eb','#a86b0a','#e6eaf1')

    @staticmethod
    def _rounded(painter,rect,fill,outline,radius):
        painter.setBrush(QColor(fill))
        painter.setPen(QPen(QColor(outline),1))
        painter.drawRoundedRect(rect,radius,radius)

    def _font(self, size, bold=False):
        key=(size,bold)
        if key not in self._fonts:
            font=QFont(self.font());font.setPixelSize(size)
            font.setWeight(QFont.Weight.DemiBold if bold else QFont.Weight.Normal)
            self._fonts[key]=font
        return self._fonts[key]

    def _caption(self,painter,text,rect,color,*,size=12,bold=False,align=Qt.AlignLeft):
        font=self._font(size,bold);painter.setFont(font);painter.setPen(QColor(color))
        value=QFontMetrics(font).elidedText(text,Qt.ElideRight,max(0,math.floor(rect.width())))
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
        if key not in self._pixmaps:self._pixmaps[key]=QPixmap()
        pixmap=self._pixmaps[key]
        if pixmap.isNull() and row.get('thumbnail'):
            pixmap.loadFromData(row['thumbnail'])
        return pixmap

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
        self.update()

    def _draw_text_detail(self,painter,row,details,ink,muted,tile,line):
        self._rounded(painter,details,tile,line,10)
        viewport=details.adjusted(10,8,-11,-8)
        if viewport.isEmpty():return
        key=row['id']
        if key not in self._details:return
        document,scroll=self._details[key]
        if document.textWidth()!=viewport.width():document.setTextWidth(viewport.width())
        total=document.size().height()
        maximum=max(0.,total-viewport.height())
        scroll=max(0.,min(maximum,scroll));self._details[key][1]=scroll
        self._detail_hits[key]=(viewport,maximum)
        context=QAbstractTextDocumentLayout.PaintContext()
        context.palette.setColor(QPalette.Text,QColor(ink))
        painter.save();painter.setClipRect(viewport)
        painter.translate(viewport.x(),viewport.y()-scroll)
        document.documentLayout().draw(painter,context)
        painter.restore()
        if maximum>0:
            height=max(10.,viewport.height()*viewport.height()/total)
            y=viewport.y()+(viewport.height()-height)*scroll/maximum
            self._rounded(painter,QRectF(details.right()-6,y,2,height),muted,muted,1)

    def feedback(self,message):
        self._feedback=message
        self._feedback_timer.start(2200)
        self.update()

    def _reset_feedback(self):
        self._feedback=''
        self.update()

    def _card(self,painter,row,rect,preview,folded=False):
        surface,card,line,ink,muted,hover,gold,tile=self._colors()
        key=row['id']
        self._rounded(painter,rect,card,line,16)
        if folded:return
        thumb=QRectF(rect.x()+CARD_PADDING,rect.y()+CARD_PADDING,37,37)
        image=self._entry_image(row)
        title_x=thumb.right()+9
        if not image.isNull():
            progress=min(max(preview/110,0.),1.)
            if progress:
                width=min(rect.width()-2*CARD_PADDING,102*image.width()/image.height())
                height=width*image.height()/image.width()
                thumb=QRectF(thumb.x()+(rect.center().x()-width/2-thumb.x())*progress,
                            thumb.y()+44*progress,37+(width-37)*progress,37+(height-37)*progress)
                title_x+=(rect.x()+CARD_PADDING-title_x)*progress
            clip=QPainterPath();clip.addRoundedRect(thumb,7,7)
            painter.save();painter.setClipPath(clip);painter.drawPixmap(thumb,image,QRectF(image.rect()));painter.restore()
        else:
            self._rounded(painter,thumb,tile,line,9)
            glyph={'text':'text','link':'link','image':'image','file':'file'}.get(row['kind'],'text')
            self._glyph(painter,glyph,thumb.adjusted(10,10,-10,-10),muted)
        pin=QRectF(rect.right()-53,rect.top()+7,23,23)
        remove=QRectF(rect.right()-27,rect.top()+7,23,23)
        title=QRectF(title_x,rect.y()+9,pin.left()-title_x-5,22)
        meta=QRectF(title.x(),rect.y()+32,rect.right()-title.x()-8,17)
        self._caption(painter,row['title'],title,ink,size=12,bold=True)
        label={'text':'文字','link':'链接','image':'图片','file':'文件引用'}[row['kind']]
        stamp=time.strftime('%m-%d %H:%M',time.localtime(row['updated']))
        self._caption(painter, label+' · '+stamp,meta,muted,size=10)
        for part,box,glyph in (('pin',pin,'pin-filled' if row['pinned'] else 'pin'),('remove',remove,'close')):
            if self._hovered==(part,key):self._rounded(painter,box,hover,hover,7)
            self._glyph(painter,glyph,box.adjusted(5,5,-5,-5),gold if part=='pin' and row['pinned'] else muted)
        self._hits.extend([('thumb',key,thumb),('pin',key,pin),('remove',key,remove),('card',key,rect)])
        if preview>16 and image.isNull():
            details=QRectF(rect.x()+CARD_PADDING,rect.y()+57,rect.width()-2*CARD_PADDING,
                           max(10.,rect.height()-57-CARD_PADDING))
            self._draw_text_detail(painter,row,details,ink,muted,tile,line)

    def paintEvent(self,event):
        painter=QPainter(self);painter.setRenderHints(QPainter.Antialiasing|QPainter.SmoothPixmapTransform)
        painter.translate(0,self._content_top())
        self._hits=[]
        self._detail_hits={}
        surface,card,line,ink,muted,hover,gold,tile=self._colors()
        self._rounded(painter,self._painted,surface,line,RADIUS_WINDOW_PX)
        title=QRectF(30,8,152,29)
        self._glyph(painter,'copy',QRectF(13,16,13,13),muted)
        message='再次点击清空历史' if self._confirm_clear.isActive() else self._feedback or '剪贴板历史'
        self._caption(painter,message,title,ink,size=13,bold=True)
        clear=QRectF(self.width()-44,9,30,28)
        if self.entries:
            if self._hovered==('clear','') or self._confirm_clear.isActive():
                self._rounded(painter,clear,hover,line,9)
            self._glyph(painter,'trash',clear.adjusted(7,6,-7,-6),'#d33138' if self._confirm_clear.isActive() else muted)
            self._hits.append(('clear','',clear))
        painter.save()
        painter.setClipRect(QRectF(1,HEADER,self.width()-2,max(0.,self._painted.height()-HEADER-INSET+2)))
        if not self.entries:
            self._caption(painter,'暂无剪贴板历史',QRectF(14,HEADER,self.width()-28,40),muted,align=Qt.AlignHCenter)
        positions=[]
        y=HEADER-self.scroll
        for i,row in enumerate(self.entries):
            extra=self._preview_extra(row)
            width=self.width()-INSET*2
            inset=min(i,2)*5*(1-self._stack_progress)
            top=(HEADER+min(i,2)*PEEK)*(1-self._stack_progress)+y*self._stack_progress
            positions.append((row,QRectF(INSET+inset,top,width-inset*2,ROW+extra),extra,i))
            y+=ROW+GAP+extra
        for row,rect,extra,i in (reversed(positions) if self._stack_progress<.32 else positions):
            if i>2 and self._stack_progress<.12:continue
            if rect.bottom()<HEADER or rect.top()>=self._painted.bottom():continue
            self._card(painter,row,rect,extra,folded=i>0 and self._stack_progress<.32)
        if self.can_load_more and self._stack_progress>.95:
            more=QRectF(INSET,y,self.width()-2*INSET,25)
            self._caption(painter,'加载更多',more,muted,size=11,align=Qt.AlignHCenter)
            self._hits.append(('more','',more))
        available=self._painted.height()-HEADER-INSET
        total=self._body_height()
        if self._stack_progress>.95 and total>available and available>20:
            height=max(16.,available*available/total)
            y=HEADER+(available-height)*self.scroll/(total-available)
            bar=QRectF(self.width()-7,y,3,height)
            self._rounded(painter,bar,muted,muted,1.5)
        painter.restore();painter.end()

    def _hit(self,position):
        position=position-QPointF(0,self._content_top())
        if not self._painted.contains(position):return None
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
        if self.entries and not self.expanded:self.show_all()
        super().enterEvent(event)

    def leaveEvent(self,event):
        self._hovered=None
        if self.expanded:self.show_all(False)
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

    def wheelEvent(self,event):
        if not self.expanded:self.show_all()
        delta=event.pixelDelta().y() or event.angleDelta().y()*.37
        position=event.position()-QPointF(0,self._content_top())
        for key,(viewport,limit) in self._detail_hits.items():
            if viewport.contains(position) and key in self._details:
                detail=self._details[key]
                offset=max(0.,min(limit,detail[1]-delta))
                if offset!=detail[1]:
                    detail[1]=offset;self.update();event.accept();return
        maximum=max(0.,self._body_height()-max(0.,self._surface_height()-HEADER-INSET))
        self.scroll=max(0.,min(maximum,self.scroll-delta))
        self.update();event.accept()
