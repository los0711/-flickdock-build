"""Native translator controls using FlickDock surfaces and display clock."""
from collections import OrderedDict
from functools import cache
from pathlib import Path
import math
import re
import time
import weakref
from shiboken6 import isValid

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QSignalBlocker, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QFontMetricsF, QIcon, QLinearGradient, QPainter, QPainterPath, QPalette, QPen, QPixmap, QRegion, QTextCharFormat, QTextLayout, QTextLine, QTextOption
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QToolButton, QAbstractButton, QApplication, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QStyledItemDelegate, QStyle, QVBoxLayout, QWidget)

from native_ui_common import (RADIUS_PANEL_PX, PopupChevronMotion, RoundedScrollBar, RoundedSurfaceFrame, colors, icon, paint_rounded_surface,
                              qss_color, reduced_motion, reference_font, rounded_popup, screen_for_widget, set_ui_weight)
from display_frames import DisplayAnimation, FramePulse
from reference_widgets import IconTextButton, PixelScrollTextEdit, ToolbarButton, button, css, label
from surface_morph import paint_surface_shadow
from translation_languages import TARGETS
from flickdock_motion import Spring
from bisect import bisect_right
import ui_theme

ACTION_SIZE=28


class ThinkingText(QLabel):
    """Native AI Text Loading motion, adapted from Kokonut UI (MIT)."""

    PHRASE_INTERVAL=1.5
    WAVE_DURATION=1.6
    WAVE_STEP=.07
    ENTER_DURATION=.28
    ENTER_STEP=.018
    EXIT_DURATION=.15
    GLYPH_PADDING=4

    def __init__(self,parent):
        super().__init__(parent)
        font=set_ui_weight(reference_font(),500)
        font.setPixelSize(11);self.setFont(font)
        self.setFixedHeight(math.ceil(QFontMetricsF(font).height())+10)
        self.setTextFormat(Qt.PlainText)
        self._phrases=();self._started=0.;self._since=0.
        self._ink=qss_color(colors(ui_theme.current_theme())['secondary'])
        self._sprites={}
        from ui_transitions import exit_curve
        self._ease_out=exit_curve()
        self._wave=QEasingCurve(QEasingCurve.BezierSpline)
        self._wave.addCubicBezierSegment(QPointF(.42,0),QPointF(.58,1),QPointF(1,1))
        self._frames=FramePulse(self,follow_refresh_rate=True)
        self._frames.timeout.connect(self._frame)
        self.hide()

    def set_theme(self,theme):
        ink=qss_color(colors(theme)['secondary'])
        if ink!=self._ink:
            self._ink=ink
            self._sprites.clear()
            self.update()

    def set_task(self,caption='',alternate=''):
        phrases=(caption,alternate) if alternate else (caption,) if caption else ()
        if phrases==self._phrases:return
        self._phrases=phrases
        self.setText(caption);self.setAccessibleName(caption)
        self.setVisible(bool(phrases))
        self._restart()

    def _restart(self):
        self._started=time.perf_counter()
        self._since=0.
        if self.isVisible() and self._phrases and not reduced_motion():
            if not self._frames.isActive():self._frames.start()
        else:self._frames.stop()
        self.update()

    def showEvent(self,event):
        super().showEvent(event)
        self._restart()

    def hideEvent(self,event):
        self._frames.stop()
        super().hideEvent(event)

    def _frame(self):
        if reduced_motion():
            self._frames.stop();self._since=0.
        else:self._since=time.perf_counter()-self._started
        self.update()

    def _soft_glyph(self,letter,metrics,ratio):
        key=(letter,ratio)
        if key not in self._sprites:
            pad=self.GLYPH_PADDING
            width=math.ceil((metrics.horizontalAdvance(letter)+2*pad)*ratio)
            height=math.ceil((metrics.height()+2*pad)*ratio)
            image=QPixmap(width,height);image.setDevicePixelRatio(ratio);image.fill(Qt.transparent)
            painter=QPainter(image)
            painter.setFont(self.font());painter.setPen(self._ink)
            painter.drawText(QPointF(pad,pad+metrics.ascent()),letter)
            painter.end()
            softened=image.scaled(max(1,round(width/3)),max(1,round(height/3)),
                                  Qt.IgnoreAspectRatio,Qt.SmoothTransformation)
            softened=softened.scaled(width,height,Qt.IgnoreAspectRatio,Qt.SmoothTransformation)
            softened.setDevicePixelRatio(ratio)
            self._sprites[key]=softened
        return self._sprites[key]

    def _paint_phrase(self,painter,metrics,phrase,at,*,entering=False,leaving=False):
        x=0.
        baseline=(self.height()-metrics.height())/2+metrics.ascent()
        for index,letter in enumerate(phrase):
            wave=((at-index*self.WAVE_STEP)%self.WAVE_DURATION)/self.WAVE_DURATION
            opacity=1.-.7*self._wave.valueForProgress(1.-abs(2.*wave-1.))
            offset=blur=0.
            if entering:
                fraction=max(0.,min(1.,(at-index*self.ENTER_STEP)/self.ENTER_DURATION))
                settled=self._ease_out.valueForProgress(fraction)
                opacity*=settled;offset=8.*(1.-settled);blur=1.-settled
            elif leaving:
                departed=self._ease_out.valueForProgress(at/self.EXIT_DURATION)
                opacity*=1.-departed;offset=-6.*departed;blur=departed*.75
            if opacity>0:
                if blur>0:
                    pad=self.GLYPH_PADDING
                    painter.setOpacity(opacity*blur)
                    painter.drawPixmap(QPointF(x-pad,baseline+offset-metrics.ascent()-pad),
                                       self._soft_glyph(letter,metrics,self.devicePixelRatioF()))
                painter.setOpacity(opacity*(1.-blur))
                painter.drawText(QPointF(x,baseline+offset),letter)
            x+=metrics.horizontalAdvance(letter)

    def paintEvent(self,event):
        if not self._phrases:return
        painter=QPainter(self);painter.setFont(self.font());painter.setPen(self._ink)
        metrics=QFontMetricsF(self.font())
        if not self._frames.isActive():
            painter.drawText(self.rect(),Qt.AlignLeft|Qt.AlignVCenter,self._phrases[0])
        else:
            cycle=int(self._since/self.PHRASE_INTERVAL)
            now=self._since-cycle*self.PHRASE_INTERVAL
            index=cycle%len(self._phrases)
            changing=cycle>0 and len(self._phrases)>1
            if changing and now<self.EXIT_DURATION:
                self._paint_phrase(painter,metrics,self._phrases[index-1],now,leaving=True)
            phrase=self._phrases[index]
            entering=changing and now<self.ENTER_DURATION+(len(phrase)-1)*self.ENTER_STEP
            self._paint_phrase(painter,metrics,phrase,now,entering=entering)
        painter.end()


class StreamingResultEditor(PixelScrollTextEdit):
    """Reveal the complete native document without editing it on paint frames."""

    WORD_INTERVAL=.07
    MAX_STAGGER=2.1
    WORD_STIFFNESS=340
    WORD_DAMPING=26
    WORD_SETTLE=.36
    _UNITS=re.compile(r'[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]|[^\s\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]+')

    def __init__(self,parent):
        super().__init__(parent)
        self._reveal=FramePulse(self,follow_refresh_rate=True);self._reveal.timeout.connect(self._reveal_frame)
        self._words=[]
        self._started_at=0.
        self._delay=self.WORD_INTERVAL
        self.textChanged.connect(self.finish_reveal)
        self.set_viewport_fill(qss_color(colors(ui_theme.current_theme())['card']))
        self.setProperty('scrollBackdropRole','card')

    def start_reveal(self,value):
        self.finish_reveal()
        if not self.isVisible() or reduced_motion() or not value.strip():return
        offsets=[]
        last=position=0
        for match in self._UNITS.finditer(value):
            start=position+len(value[last:match.start()].encode('utf-16-le'))//2
            last=match.end()
            position=start+len(match[0].encode('utf-16-le'))//2
            offsets.append((start,position))
        if not offsets:return
        # Very long documents reveal in larger groups within the same budget.
        if len(offsets)>48:
            stride=math.ceil(len(offsets)/48)
            offsets=[(offsets[i][0],offsets[min(i+stride,len(offsets))-1][1])
                     for i in range(0,len(offsets),stride)]
        self._words=offsets
        self._delay=min(self.WORD_INTERVAL,self.MAX_STAGGER/max(1,len(offsets)-1))
        self._started_at=time.perf_counter();self._reveal.start()
        self.viewport().update()

    def finish_reveal(self):
        if not self._words:return
        self._words=[];self._reveal.stop();self.viewport().update()

    def _reveal_frame(self):
        duration=(len(self._words)-1)*self._delay+self.WORD_SETTLE
        if time.perf_counter()-self._started_at>=duration or reduced_motion():
            self.finish_reveal()
        else:self.viewport().update()

    def hideEvent(self,event):
        self.finish_reveal()
        super().hideEvent(event)

    def viewportEvent(self,event):
        if hasattr(self,'_words') and event.type() in (QEvent.MouseButtonPress,QEvent.Wheel,QEvent.TouchBegin):
            self.finish_reveal()
        return super().viewportEvent(event)

    def keyPressEvent(self,event):
        self.finish_reveal()
        super().keyPressEvent(event)

    def _draw_reveal_block(self,painter,block,origin,elapsed):
        layout=block.layout()
        length=block.length()-1
        start=block.position()
        finish=min(len(self._words),max(0,1+math.floor((elapsed-self.WORD_SETTLE)/self._delay)))
        cutoff=self._words[finish-1][1]-start if finish else 0
        if cutoff>0:
            for run in layout.glyphRuns(0,min(cutoff,length)):
                painter.drawGlyphRun(origin,run)
        if cutoff>=length:return
        damping=self.WORD_DAMPING/2
        frequency=math.sqrt(self.WORD_STIFFNESS-damping*damping)
        for index in range(finish,min(len(self._words),1+math.floor(elapsed/self._delay))):
            left,right=self._words[index]
            left=max(0,left-start);right=min(length,right-start)
            if right<=left:continue
            t=max(0.,elapsed-index*self._delay)
            angle=frequency*t
            spring=1-math.exp(-damping*t)*(math.cos(angle)+damping/frequency*math.sin(angle))
            alpha=max(0.,min(1.,spring))
            shift=QPointF(0,6*(1-spring))
            softness=.8*(1-alpha)
            runs=layout.glyphRuns(left,right-left)
            if softness>.12:
                painter.setOpacity(alpha*.55)
                for run in runs:painter.drawGlyphRun(origin+shift-QPointF(softness,0),run)
                painter.setOpacity(alpha*.65)
                shift+=QPointF(softness,0)
            else:painter.setOpacity(alpha)
            for run in runs:painter.drawGlyphRun(origin+shift,run)
        painter.setOpacity(1.)

    def paintEvent(self,event):
        if not self._words:
            super().paintEvent(event);return
        self.paint_viewport_background(event)
        painter=QPainter(self.viewport())
        painter.setPen(self.palette().color(QPalette.Text))
        painter.setClipRegion(event.region())
        clip=QRectF(event.rect())
        elapsed=time.perf_counter()-self._started_at
        viewport_offset=QPointF(-self.horizontalScrollBar().value(),-self.verticalScrollBar().value())
        document_layout=self.document().documentLayout()
        block=self.cursorForPosition(QPoint(0,max(0,event.rect().top()-6))).block()
        while block.isValid():
            rect=document_layout.blockBoundingRect(block).translated(viewport_offset)
            if rect.top()>clip.bottom()+6:break
            if block.isVisible() and rect.bottom()+6>=clip.top():
                self._draw_reveal_block(painter,block,rect.topLeft(),elapsed)
            block=block.next()
        painter.end()


class ReadAlongCaption(QWidget):
    """Wrap once, then move the word pill and scroll independently on the frame clock."""

    seek_requested=Signal(int)

    def __init__(self,parent):
        super().__init__(parent)
        font=reference_font();font.setPixelSize(13);self.setFont(font)
        self.setMinimumWidth(0)
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName('朗读字幕')
        self._text=''
        self._span=(0,0)
        self._complete=False
        self._layout=None
        self._layout_width=0
        self._offsets=[0]
        self._line_tops=[]
        self._line_starts=[]
        self._formats=[]
        self._word_box=None
        self._left=Spring(self,{'edge':0.},self._frame)
        self._right=Spring(self,{'edge':0.},self._frame)
        self._top=Spring(self,{'y':0.},self._frame)
        self._opacity=Spring(self,{'alpha':0.},self._frame)
        self._scroll=Spring(self,{'y':0.},self._frame)
        self._theme=None
        self.setFixedHeight(0)

    def _frame(self,_):
        self.update()

    def set_caption(self,text,start,end,*,complete=False):
        if (text,(start,end),complete)==(self._text,self._span,self._complete) and self._theme==ui_theme.current_theme():return
        changed=text!=self._text
        self._text=text;self._span=(start,end);self._complete=complete
        if changed:
            self.setAccessibleDescription(text)
            self._offsets=[0]
            for char in text:self._offsets.append(self._offsets[-1]+(2 if ord(char)>0xffff else 1))
            self._wrap()
        self._retarget(snap=changed)

    def finish_caption(self):
        self.set_caption(self._text,*self._span,complete=True)

    def _wrap(self):
        self._line_starts=[];self._line_tops=[]
        self._layout_width=max(1,self.width()-6)
        if not self._text:
            self._layout=None
            self.setFixedHeight(0)
            return
        self._layout=QTextLayout(self._text.replace('\r',' ').replace('\n','\u2028'),self.font())
        self._layout.setCacheEnabled(True)
        options=QTextOption();options.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self._layout.setTextOption(options)
        self._layout.beginLayout()
        y=2.
        while (line:=self._layout.createLine()).isValid():
            line.setLineWidth(self._layout_width)
            line.setPosition(QPointF(3.,y))
            self._line_starts.append(line.textStart());self._line_tops.append(y)
            y+=line.height()+1.
        self._layout.endLayout()
        last=self._layout.lineAt(min(len(self._line_tops),3)-1)
        self.setFixedHeight(math.ceil(last.y()+last.height()+2.))

    @staticmethod
    def _ink_range(start,end,color):
        run=QTextLayout.FormatRange()
        run.start=start;run.length=end-start
        run.format=QTextCharFormat();run.format.setForeground(color)
        return run

    def _retarget(self,*,snap=False):
        self._theme=ui_theme.current_theme()
        c=colors(self._theme)
        self._formats=[]
        if self._layout is None or not self._line_starts:
            self._word_box=None;self._scroll.jump({'y':0.});self._opacity.jump({'alpha':0.})
            return
        start,end=(self._offsets[min(spot,len(self._text))] for spot in self._span)
        read_end=self._offsets[-1] if self._complete else end
        if read_end:self._formats.append(self._ink_range(0,read_end,qss_color(c['text'])))
        snap=snap or reduced_motion() or not self.isVisible()
        line_index=max(0,bisect_right(self._line_starts,start)-1)
        line=self._layout.lineAt(line_index)
        # Leave one leading line above the current word when there is room.
        target_scroll=self._line_tops[max(0,line_index-1)]-2.
        final=self._layout.lineAt(self._layout.lineCount()-1)
        target_scroll=min(target_scroll,max(0.,final.y()+final.height()+2.-self.height()))
        if snap:self._scroll.jump({'y':target_scroll})
        elif self._scroll._targets['y']!=target_scroll:self._scroll.animate_physical({'y':target_scroll},600,48)
        if end<=start:
            self._word_box=None
            self._opacity.jump({'alpha':0.})
            return
        next_line=line.textStart()+line.textLength()
        x1=line.cursorToX(start)[0];x2=line.cursorToX(min(end,next_line))[0]
        pad=QFontMetricsF(self.font()).height()*.16
        left,right=sorted((x1,x2))
        left=max(0.,left-pad);right=min(float(self.width()),right+pad)
        top=line.y();height=line.height()
        prev=self._word_box
        self._word_box=(left,top,right,height)
        if prev==self._word_box and not snap:
            self.update()
            return
        if snap:
            self._left.jump({'edge':left});self._right.jump({'edge':right})
            self._top.jump({'y':top});self._opacity.jump({'alpha':1.})
            return
        forward=prev is None or left>=prev[0]
        if prev is None or prev[1]!=top:
            self._left.jump({'edge':left});self._right.jump({'edge':left});self._top.jump({'y':top})
        self._left.animate_physical({'edge':left},*((210,26) if forward else (520,40)))
        self._right.animate_physical({'edge':right},*((520,40) if forward else (210,26)))
        self._opacity.animate_physical({'alpha':1.},300,30)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if max(1,self.width()-6)!=self._layout_width:
            self._wrap();self._retarget(snap=True)

    def hideEvent(self,event):
        for spring in (self._left,self._right,self._top,self._opacity,self._scroll):spring.stop()
        super().hideEvent(event)

    def showEvent(self,event):
        super().showEvent(event)
        self._retarget(snap=True)

    def paintEvent(self,event):
        if self._layout is None:return
        c=colors(self._theme or ui_theme.current_theme())
        painter=QPainter(self);painter.setRenderHint(QPainter.Antialiasing)
        scroll=self._scroll.values['y']
        origin=QPointF(0,-scroll)
        pill=None
        alpha=min(1.,max(0.,self._opacity.values['alpha']))
        if self._word_box is not None and alpha>0.:
            left=self._left.values['edge'];width=max(0.,self._right.values['edge']-left)
            top=self._top.values['y'];height=self._word_box[3]
            pill=QPainterPath()
            pill.addRoundedRect(QRectF(left,top-scroll,width,height),height*.26,height*.26)
            painter.setOpacity(alpha);painter.fillPath(pill,qss_color(c['action']));painter.setOpacity(1.)
        painter.setPen(qss_color(c['muted']))
        self._layout.draw(painter,origin,self._formats)
        if pill is not None:
            # Keep text legible even while only part of a word is under the pill.
            painter.setClipPath(pill,Qt.IntersectClip)
            painter.setOpacity(alpha);painter.setPen(qss_color(c['on_action']))
            self._layout.draw(painter,origin)
        painter.end()

    def mousePressEvent(self,event):
        if event.button()==Qt.LeftButton and self._layout is not None:
            y=event.position().y()+self._scroll.values['y']
            index=max(0,bisect_right(self._line_tops,y)-1)
            offset=self._layout.lineAt(index).xToCursor(event.position().x(),QTextLine.CursorOnCharacter)
            self.seek_requested.emit(max(0,bisect_right(self._offsets,offset)-1))
            event.accept()
            return
        super().mousePressEvent(event)


class PolishArrival:
    """Fade in the comparison and editors' action controls after a result."""
    def __init__(self,parent):
        self.motion=DisplayAnimation(parent)
        self.motion.setDuration(185);self.motion.setStartValue(0.);self.motion.setEndValue(1.)
        self.motion.setEasingCurve(QEasingCurve.OutCubic)
        self.motion.valueChanged.connect(self._step);self.motion.finished.connect(self.stop)
        self._items=[]

    def start(self,controls):
        self.stop()
        if reduced_motion():return
        for control in controls:
            if control.isVisible() and control.graphicsEffect() is None:
                effect=QGraphicsOpacityEffect(control);effect.setOpacity(0.)
                control.setGraphicsEffect(effect);self._items.append((control,effect))
        if self._items:self.motion.start()

    def _step(self,alpha):
        for control,effect in self._items:
            if isValid(control) and isValid(effect):effect.setOpacity(alpha)

    def stop(self):
        self.motion.stop()
        items,self._items=self._items,[]
        for control,effect in items:
            if isValid(control) and isValid(effect) and control.graphicsEffect() is effect:
                control.setGraphicsEffect(None)


@cache
def language_flag(region):
    if not region:
        return QIcon()
    svg=QSvgRenderer(str(Path(__file__).resolve().parent/'assets'/'flags'/(region+'.svg')))
    if not svg.isValid():
        return QIcon()
    pixmap=QPixmap(64,48);pixmap.fill(Qt.transparent)
    painter=QPainter(pixmap);svg.render(painter);painter.end()
    return QIcon(pixmap)


class TranslatorToolButton(ToolbarButton):
    def __init__(self,glyph,parent=None,*,glyph_size=15):
        super().__init__(glyph,parent,motion=False)
        self.setFixedSize(ACTION_SIZE,ACTION_SIZE)
        self.setProperty('controlFrame',True)
        self.setProperty('glyphSize',glyph_size)


class TranslatorActionButton(IconTextButton):
    def _paint_surface(self,painter):
        parent=self.parentWidget()
        if not isinstance(parent,TranslatorSplitAction):
            super()._paint_surface(painter)
            return
        if not self.isEnabled():return
        palette=colors(ui_theme.current_theme())
        hover=self.underMouse()
        if hover or self.isDown():
            painter.save()
            path=QPainterPath()
            bounds=QRectF(parent.rect()).adjusted(1,1,-1,-1).translated(-self.x(),-self.y())
            radius=bounds.height()/2
            path.addRoundedRect(bounds,radius,radius)
            painter.setClipPath(path,Qt.IntersectClip)
            painter.fillRect(self.rect(),qss_color(palette['control_pressed' if self.isDown() else 'control_hover']))
            painter.restore()
        if self.property('keyboardFocus'):
            paint_rounded_surface(painter,QRectF(self.rect()).adjusted(2,2,-2,-2),9,
                                  QColor(Qt.transparent),qss_color(palette['ring']))

    def sizeHint(self):
        if not self.text() and not self.icon().isNull():return QSize(self.height(),self.height())
        return super().sizeHint()

    def minimumSizeHint(self):
        return self.sizeHint()

    def set_glyph(self,glyph):
        if not glyph:
            if not self.icon().isNull():self._reference_icon=None;self.setIcon(QIcon())
            return
        if getattr(self,'_reference_icon',None)==(glyph,'secondary'):
            return
        self._reference_icon=(glyph,'secondary')
        self.setIcon(icon(glyph,colors(ui_theme.current_theme())['secondary']))


def translator_action(text,glyph,parent,*,variant='secondary',padding=7):
    control=button(text,glyph,variant,parent,control_type=TranslatorActionButton)
    control.setFixedHeight(ACTION_SIZE)
    control.setIconSize(QSize(14,14))
    control.setProperty('iconTextGap',4)
    control.setProperty('iconTextReservedWidth',16)
    styles=[]
    for theme in ('dark','light'):
        c=colors(theme);ink='@fd_on_action@' if variant=='primary' else c['text']
        styles.append(f"QPushButton{{background:transparent;color:{ink};border:0;padding:2px {padding}px;font:500 11px 'FlickDock UI';}}"
                      f"QPushButton:disabled{{color:{c['muted']};}}")
    css(control,*styles)
    return control


def picker_list(parent):
    view=QListWidget(parent)
    view.setProperty('scrollEdgeBlur',False)
    view.setProperty('scrollBackdropRole','popup_surface')
    view.setFrameShape(QFrame.NoFrame)
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    view.setVerticalScrollMode(QListWidget.ScrollPerPixel)
    view.setSpacing(2)
    view.setVerticalScrollBar(RoundedScrollBar(Qt.Vertical,view))
    css(view,*[
        f"QListWidget{{background:transparent;color:{colors(theme)['text']};border:0;outline:0;}}"
        "QListWidget::item{background:transparent;border:0;}"
        "QScrollBar:vertical{background:transparent;width:6px;}"
        "QScrollBar::handle:vertical{background:transparent;min-height:22px;}"
        "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}"
        for theme in ('dark','light')
    ])
    return view


class TranslatorSurface(RoundedSurfaceFrame):
    def __init__(self,parent=None,*,role='card',radius=12,border=True,fill_opacity=1.):
        super().__init__(parent,content_inset=0);self.role=role;self.radius=radius;self.border=border
        self.fill_opacity=fill_opacity
    def surface_style(self):
        palette = colors(ui_theme.current_theme())
        outline = 'glass_border' if self.role == 'window_backdrop' else 'border'
        fill=palette[self.role]
        if self.fill_opacity!=1.:
            fill=qss_color(fill)
            fill.setAlphaF(fill.alphaF()*self.fill_opacity)
        return fill, palette[outline] if self.border else None, self.radius


class TranslatorSplitAction(TranslatorSurface):
    def __init__(self,text,glyph,parent):
        super().__init__(parent,role='control',radius=ACTION_SIZE/2)
        self.setFixedHeight(ACTION_SIZE)
        layout=QHBoxLayout(self);layout.setContentsMargins(1,1,1,1);layout.setSpacing(0)
        self.primary=translator_action(text,glyph,self,variant='quiet')
        self.secondary=translator_action('','settings',self,variant='quiet')
        self.secondary.setIconSize(QSize(12,12))
        self.secondary.setMinimumWidth(26)
        for control in (self.primary,self.secondary):
            control.setFixedHeight(ACTION_SIZE-2)
            layout.addWidget(control)
            if control is self.primary:
                divider=QFrame(self);divider.setFixedSize(1,14)
                divider.setStyleSheet('background:rgba(128,128,128,68);border:0;')
                layout.addWidget(divider,0,Qt.AlignVCenter)


class PolishPane(TranslatorSurface):
    """Breathe the selected pane's whole outline through the current accent."""
    BREATH_SECONDS=3.8

    def __init__(self,parent):
        super().__init__(parent,role='card',radius=RADIUS_PANEL_PX)
        self._active=False;self._started_at=0.
        self._rim=QRegion()
        self._frames=FramePulse(self,follow_refresh_rate=True);self._frames.timeout.connect(self._tick)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        # Include the diagonal section of each rounded corner in the dirty band.
        inset=math.ceil(self.radius*(1-math.sqrt(.5)))+2
        self._rim=QRegion(self.rect()).subtracted(QRegion(self.rect().adjusted(inset,inset,-inset,-inset)))

    def set_polish_active(self,active):
        changed=self._active!=active
        self._active=active
        animate=active and self.isVisible() and not self.window().isMinimized() and not reduced_motion()
        if animate and not self._frames.isActive():
            self._started_at=time.monotonic();self._frames.start()
        elif not animate:self._frames.stop()
        if changed:self.update(self._rim)

    def showEvent(self,event):
        super().showEvent(event);self.set_polish_active(self._active)

    def hideEvent(self,event):
        self._frames.stop();super().hideEvent(event)

    def _tick(self):
        if not self.isVisible() or self.window().isMinimized() or reduced_motion():
            self._frames.stop()
        self.update(self._rim)

    def surface_style(self):
        fill,border,radius=super().surface_style()
        if not self._active:return fill,border,radius
        blend=.6
        if self._frames.isActive():
            phase=(time.monotonic()-self._started_at)%self.BREATH_SECONDS/self.BREATH_SECONDS
            blend=(1-math.cos(math.tau*phase))/2
        resting=qss_color(border).getRgbF()
        accent=QColor(ui_theme.color('accent')).getRgbF()
        stroke=QColor.fromRgbF(*(a+(b-a)*blend for a,b in zip(resting,accent)))
        return fill,stroke,radius


class _ComparisonReveal(QWidget):
    """Reveal both sides of the native viewport with a symmetric fade."""
    def __init__(self,editor):
        super().__init__(editor.window())
        self.editor=editor
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        editor.destroyed.connect(self.deleteLater)
        self.hide()

    def paintEvent(self,event):
        editor=self.editor
        if not isValid(editor):return
        core_height=editor.viewport().height()
        core_top=(self.height()-core_height)//2
        core_bottom=core_top+core_height
        extent=round(core_top*editor.reveal_progress)
        if extent<=0:return
        top,bottom=core_top-extent,core_bottom+extent
        left_edge=extent/(bottom-top)
        right_edge=1-left_edge
        upper=QRect(0,top,self.width(),extent)
        lower=QRect(0,core_bottom,self.width(),extent)
        painter=QPainter(self)
        # Leave the actual preview and its scrollbar untouched in the center.
        painter.setClipRegion(QRegion(upper).united(QRegion(lower)))
        background=QLinearGradient(0,top,0,bottom)
        palette=colors(ui_theme.current_theme())
        fill=qss_color(palette['control'])
        edge=qss_color(palette['input'])
        background.setColorAt(0,edge);background.setColorAt(left_edge,fill)
        background.setColorAt(right_edge,fill);background.setColorAt(1,edge)
        paint_rounded_surface(painter,QRect(0,top,self.width(),bottom-top),8,background)
        ink=editor.palette().color(QPalette.Text)
        shade=QLinearGradient(0,top,0,bottom)
        for position,alpha in ((0,0),(left_edge*.55,.28),(left_edge,.66),
                               (right_edge,.66),(1-left_edge*.55,.28),(1,0)):
            ink.setAlphaF(alpha*editor.reveal_progress);shade.setColorAt(position,ink)
        painter.setPen(QPen(shade,1))
        editor.paint_surrounding_lines(painter,top,bottom)
        painter.end()


class ComparisonEditor(QPlainTextEdit):
    PREVIEW_HEIGHT=58
    EXPANDED_HEIGHT_FACTOR=2.5

    def __init__(self,parent):
        super().__init__(parent)
        self.setReadOnly(True);self.setFixedHeight(self.PREVIEW_HEIGHT)
        self.reveal_progress=0.;self._reveal=None;self._text_column=None
        self._reveal_animation=DisplayAnimation(self)
        self._reveal_animation.setEasingCurve(QEasingCurve.InOutCubic)
        self._reveal_animation.valueChanged.connect(self._reveal_frame)
        self.updateRequest.connect(self._position_reveal)

    def set_reveal_column(self,widget):
        self._text_column=widget

    def enterEvent(self,event):
        super().enterEvent(event);self._expand(True)

    def leaveEvent(self,event):
        self._expand(False);super().leaveEvent(event)

    def hideEvent(self,event):
        self._reveal_animation.stop();self._reveal_frame(0.)
        super().hideEvent(event)

    def _expand(self,expanded):
        if expanded and self.verticalScrollBar().maximum()<=0:return
        target=float(expanded);current=self.reveal_progress
        if expanded and self._reveal is None:self._reveal=_ComparisonReveal(self)
        self._reveal_animation.stop()
        if reduced_motion() or current==target:
            self._reveal_frame(target);return
        self._reveal_animation.setDuration(210 if expanded else 175)
        self._reveal_animation.setStartValue(current);self._reveal_animation.setEndValue(target)
        self._reveal_animation.start()

    def _reveal_frame(self,value):
        self.reveal_progress=float(value)
        self._position_reveal()

    def _position_reveal(self,*_):
        if self._reveal is None:return
        bar=self.verticalScrollBar()
        column=self._text_column
        if (self.reveal_progress<=0 or not self.isVisible() or column is None
                or not isValid(column) or bar.maximum()<=0):
            self._reveal.hide();return
        owner=self.window();viewport=self.viewport()
        start=viewport.mapTo(owner,QPoint())
        column_left=column.mapTo(owner,QPoint()).x()
        height=viewport.height()
        expanded_height=self.parentWidget().height()*self.EXPANDED_HEIGHT_FACTOR
        requested=round((expanded_height-height)/2)
        # Anchor to the text viewport but measure the full comparison frame,
        # including its heading. The input column supplies only the width.
        extent=min(requested,start.y(),owner.height()-start.y()-height)
        if extent<=0:self._reveal.hide();return
        left=min(column_left,start.x())
        right=max(column_left+column.width(),start.x()+viewport.width())
        rect=QRect(left,start.y()-extent,right-left,height+2*extent)
        if self._reveal.geometry()!=rect:self._reveal.setGeometry(rect)
        if self._reveal.isHidden():self._reveal.show();self._reveal.raise_()
        self._reveal.update()

    def paint_surrounding_lines(self,painter,top,bottom):
        block=self.firstVisibleBlock()
        if not block.isValid():return
        viewport=self.viewport()
        origin=(self.blockBoundingGeometry(block).topLeft()+self.contentOffset()
                +QPointF(viewport.mapTo(self.window(),QPoint())-self._reveal.pos()))
        x,y=origin.x(),origin.y()
        while y>top and block.previous().isValid():
            block=block.previous()
            if block.isVisible():y-=self.blockBoundingRect(block).height()
        # Qt may co-locate unlaid offscreen blocks. Advance by each actual
        # laid-out height from the first visible block's known position.
        while block.isValid() and y<bottom:
            if block.isVisible():
                line_height=self.blockBoundingRect(block).height()
                if y+line_height>top:block.layout().draw(painter,QPointF(x,y))
                y+=line_height
            block=block.next()


class TranslatorPopup(QFrame):
    def __init__(self,parent=None,*,width=300):
        super().__init__(parent,Qt.Popup);rounded_popup(self);self._shadow=OrderedDict()
        self.setFixedWidth(width+16);outer=QVBoxLayout(self);outer.setContentsMargins(8,8,8,8)
        self.surface=TranslatorSurface(self,role='popup_surface',radius=RADIUS_PANEL_PX);outer.addWidget(self.surface)
        self.body=QVBoxLayout(self.surface);self.body.setContentsMargins(12,12,12,12);self.body.setSpacing(10)
        self._anchor=None
        self._anchor_screen_rect=None
        self._passive_host=None
        self._passive_frame=QRect()
    def paintEvent(self,event):
        p=QPainter(self);p.setCompositionMode(QPainter.CompositionMode_Source);p.fillRect(self.rect(),Qt.transparent);p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        paint_surface_shadow(p,QRectF(self.surface.geometry()),self._shadow,12,QPoint(0,1),QColor(0,0,0,32),radius=RADIUS_PANEL_PX);p.end()
    def hideEvent(self,event):
        if self._passive_host is not None:self._passive_host.hide()
        QApplication.instance().removeEventFilter(self)
        self.closed_at=time.monotonic();super().hideEvent(event)

    def eventFilter(self,watched,event):
        if self.isVisible() and event.type()==QEvent.MouseButtonPress and self._anchor is not None:
            anchor=self._anchor()
            if anchor is not None and isValid(anchor) and anchor.isVisible():
                position=event.globalPosition().toPoint()
                hit=self._anchor_screen_rect.contains(position) if self._anchor_screen_rect is not None else anchor.rect().contains(anchor.mapFromGlobal(position))
                if hit:self.setAttribute(Qt.WA_NoMouseReplay,True);self.hide();return True
                if self._passive_host is not None and not self._passive_frame.contains(position):
                    self.hide()
        return False

    def popup_below(self,anchor,*,position=None,passive_parent=None,anchor_rect=None):
        self._dropdown_anchor=anchor
        self._anchor_screen_rect=anchor_rect
        self._anchor=weakref.ref(anchor);QApplication.instance().installEventFilter(self)
        self.setAttribute(Qt.WA_NoMouseReplay,True)
        self.adjustSize()
        if position is not None:
            if passive_parent is not None:
                from platform_native import CocoaNonActivatingHost
                if self._passive_host is None:
                    self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
                    self._passive_host=CocoaNonActivatingHost(self,keyboard=True)
                self._passive_frame=QRect(position,self.size())
                anchor_top=anchor_rect.top() if anchor_rect is not None else anchor.mapToGlobal(QPoint()).y()
                self._dropdown_motion.use_embedded_host(upward=self._passive_frame.bottom()<anchor_top)
                self._passive_host.place(self._passive_frame)
                self.show()
                self._passive_host.show(above=passive_parent,keyboard=True)
                self._passive_host.on_external_click(self.hide)
            else:
                self.move(position)
                self.show();self.raise_()
            return
        point=anchor.mapToGlobal(QPoint(0,anchor.height()+4))-QPoint(8,0)
        screen=screen_for_widget(anchor)
        if screen:
            area=screen.availableGeometry();point.setX(max(area.left(),min(point.x(),area.right()-self.width()+1)))
            if point.y()+self.height()>area.bottom()+1:point.setY(anchor.mapToGlobal(QPoint()).y()-self.height()-4)
            point.setY(max(area.top(),point.y()))
        self.move(point);self.show();self.raise_()


class LanguageButton(QAbstractButton):
    def __init__(self,parent=None,*,compact=False):
        super().__init__(parent);self.setFixedHeight(32);self.setMinimumWidth(100);self.setCursor(Qt.PointingHandCursor)
        self.compact=compact;self._region=''
        self.chevron=PopupChevronMotion(self);font=reference_font();font.setPixelSize(12);self.setFont(font)
        from dropdown_motion import DropdownTextRoll
        self._text_roll=DropdownTextRoll(self)
    def set_language(self, code, detected=''):
        name,region=TARGETS.get(code,('自动检测',''))
        if code=='auto' and detected:
            name='识别为 '+detected
        if self.text()==name and self._region==region:
            return
        popup=self.chevron.popup
        if popup is not None and isValid(popup) and time.monotonic()-getattr(popup,'closed_at',0)<.45:
            self._text_roll.begin(self.text(),name)
        self._region=region
        self.setText(name);self.updateGeometry();self.update()
    def sizeHint(self):return QSize(self.fontMetrics().horizontalAdvance(self.text())+(42 if self.compact else 50)+(24 if self._region else 0),32)
    def paintEvent(self,event):
        c=colors(ui_theme.current_theme());p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        if self.isDown():
            center=QRectF(self.rect()).center();p.translate(center);p.scale(.97,.97);p.translate(-center)
        if not self.compact or self.underMouse() or self.isDown():
            paint_rounded_surface(p,self.rect(),12,qss_color(c['control_pressed'] if self.isDown() else c['control_hover'] if self.underMouse() else c['card']),None if self.compact else qss_color(c['input_border']))
        p.setFont(self.font());p.setPen(QColor(c['secondary'] if self.compact else c['text']))
        left=5 if self.compact else 14
        flag=language_flag(self._region)
        if not flag.isNull():
            flag.paint(p,left,(self.height()-14)//2,18,14)
            left+=24
        available=max(1,self.width()-left-(26 if self.compact else 30))
        self._text_roll.paint(p,QRectF(left,0,available,self.height()),self.text())
        p.translate(self.width()-(10 if self.compact else 21),self.height()/2);p.rotate(self.chevron.angle);p.setPen(QPen(QColor(c['secondary']),1.3,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin))
        p.drawLine(QPoint(-4,-2),QPoint(0,2));p.drawLine(QPoint(0,2),QPoint(4,-2));p.end()


class RoundSwapButton(ToolbarButton):
    SIZE=36
    def __init__(self,parent=None):
        super().__init__('swap',parent,motion=False);self.setFixedSize(self.SIZE,self.SIZE)
    def paintEvent(self,event):
        c=colors(ui_theme.current_theme());p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        center=QRectF(self.rect()).center();scale=self.spring.values['scale'];p.translate(center);p.scale(scale,scale);p.translate(-center)
        fill=c['control_pressed'] if self.isDown() else c['control_hover'] if self.underMouse() else c['surface']
        paint_rounded_surface(p,self.rect(),self.SIZE/2,qss_color(fill),qss_color(c['ring'] if self.hasFocus() and self._keyboard_focus else c['input_border']))
        offset=(self.SIZE-16)//2
        icon('swap',c['text']).paint(p,offset,offset,16,16);p.end()


class LanguageOptionDelegate(QStyledItemDelegate):
    """Paint only the visible options, with separate hit areas for stars."""
    def __init__(self,popup):
        super().__init__(popup.list);self.popup=popup
        self.font=reference_font();self.font.setPixelSize(12)

    @staticmethod
    def parts(rect, code):
        row=QRectF(rect).adjusted(0,1,-2,-1)
        if code=='auto':return row,QRectF()
        star=QRectF(row.right()-28,row.center().y()-14,28,28)
        return row.adjusted(0,0,-34,0),star

    def paint(self,painter,option,index):
        popup=self.popup
        code=index.data(Qt.UserRole)
        region=index.data(Qt.UserRole+1)
        choice,star=self.parts(option.rect,code)
        c=colors(ui_theme.current_theme())
        selected=popup.current==code
        hover=popup._hovered
        pressed=popup._pressed
        point=(code,False)
        highlighted=popup._highlight.paint(painter,qss_color(c['control_hover']),8,option.rect)
        if selected or (hover==point and not highlighted) or pressed==point:
            role=('action_pressed' if pressed==point else 'action') if selected else ('control_pressed' if pressed==point else 'control_hover')
            paint_rounded_surface(painter,choice,8,qss_color(c[role]))
        if option.state & QStyle.State_HasFocus and not selected:
            paint_rounded_surface(painter,choice,8,QColor(Qt.transparent),qss_color(c['ring']))
        color=c['on_action'] if selected else c['text']
        flag=language_flag(region)
        if flag.isNull():flag=icon('web',color)
        flag.paint(painter,QRectF(choice.left()+12,choice.center().y()-7,18,14).toAlignedRect())
        painter.save()
        painter.setFont(self.font);painter.setPen(qss_color(color))
        text=choice.adjusted(36,0,-7,0)
        painter.drawText(text,Qt.AlignLeft|Qt.AlignVCenter,
                         painter.fontMetrics().elidedText(index.data(Qt.DisplayRole),Qt.ElideRight,max(1,int(text.width()))))
        painter.restore()
        if not star.isEmpty():
            point=(code,True)
            role='control_pressed' if pressed==point else 'control_hover' if hover==point else 'control'
            paint_rounded_surface(painter,star,8,qss_color(c[role]),qss_color(c['border']))
            favored=code in popup._favorites
            icon('star-filled' if favored else 'star','#E5AA17' if favored else c['secondary']).paint(
                painter,star.adjusted(7,7,-7,-7).toAlignedRect())


class LanguagePopup(TranslatorPopup):
    selected=Signal(str)
    def __init__(self,store,parent=None,*,side='target'):
        super().__init__(parent,width=280);self.store=store;self.current='';self.allow_auto=True
        self.side=side
        self._list_key=None;self._favorites=set();self._hovered=None;self._pressed=None
        self.search=QLineEdit(self.surface);self.search.setPlaceholderText('搜索语言名称或代码');self.search.setClearButtonEnabled(True);self.search.setToolTip('搜索语言');self.body.addWidget(self.search)
        self.list=picker_list(self.surface);self.list.setFixedHeight(290)
        from dropdown_motion import StretchHighlight
        self._highlight=StretchHighlight(self.list.viewport())
        self.list.verticalScrollBar().valueChanged.connect(lambda _:self._highlight.move(QRectF()))
        self.list.setMouseTracking(True)
        self.list.viewport().setMouseTracking(True)
        self.list.setItemDelegate(LanguageOptionDelegate(self))
        self.body.addWidget(self.list)
        clear=self.search.findChild(QToolButton)
        if clear is not None:clear.setToolTip('清除搜索')
        self.search.textChanged.connect(self.refresh);self.list.itemActivated.connect(self._activate)
    def configure(self,current,*,allow_auto=True):
        self.current=current;self.allow_auto=allow_auto
        self._hovered=self._pressed=None
        self._highlight.move(QRectF())
        with QSignalBlocker(self.search):self.search.clear()
        self.refresh()
    def refresh(self):
        query=self.search.text().strip().casefold()
        favorites=tuple(self.store.favorites_for(self.side))
        recent=tuple(self.store.data['recent'])
        self._favorites=set(favorites)
        key=(query,self.allow_auto,favorites,recent)
        if key!=self._list_key:
            self._list_key=key;self.list.clear()
            options=([('auto',('自动检测',''))] if self.allow_auto else [])+list(TARGETS.items())
            options.sort(key=lambda item:0 if item[0]=='auto' else 1 if item[0] in self._favorites else 2 if item[0] in recent else 3)
            for code,(name,region) in options:
                if query and query not in (name+' '+code).casefold():continue
                item=QListWidgetItem(name,self.list)
                item.setData(Qt.UserRole,code);item.setData(Qt.UserRole+1,region)
                item.setSizeHint(QSize(240,34))
        selected=next((self.list.item(i) for i in range(self.list.count())
                       if self.list.item(i).data(Qt.UserRole)==self.current),None)
        if selected is not None:
            self.list.setCurrentItem(selected)
            if not query:self.list.scrollToItem(selected)
        elif self.list.count():
            self.list.setCurrentRow(0)
        self.list.viewport().update()
    def _favorite(self,code):
        scrollbar=self.list.verticalScrollBar();position=scrollbar.value()
        self.store.toggle_language_favorite(self.side,code);self.refresh()
        scrollbar.setValue(position)
    def _hit(self,point):
        item=self.list.itemAt(point)
        if item is None:return None
        code=item.data(Qt.UserRole)
        choice,star=LanguageOptionDelegate.parts(self.list.visualItemRect(item),code)
        if star.contains(point):return code,True
        if choice.contains(point):return code,False
        return None
    def eventFilter(self,watched,event):
        if watched is self.list.viewport() and self.isVisible():
            kind=event.type()
            if kind in (QEvent.MouseMove,QEvent.Leave):
                hit=self._hit(event.position().toPoint()) if kind==QEvent.MouseMove else None
                if hit!=self._hovered:
                    self._hovered=hit
                    item=self.list.itemAt(event.position().toPoint()) if hit and not hit[1] else None
                    self._highlight.move(LanguageOptionDelegate.parts(self.list.visualItemRect(item),hit[0])[0] if item else QRectF())
                    self.list.viewport().update()
            elif kind in (QEvent.MouseButtonPress,QEvent.MouseButtonRelease) and event.button()==Qt.LeftButton:
                hit=self._hit(event.position().toPoint())
                if kind==QEvent.MouseButtonPress:
                    self._hovered=self._pressed=hit
                else:
                    pressed,self._pressed=self._pressed,None
                    if hit is not None and hit==pressed:
                        (self._favorite if hit[1] else self._choose)(hit[0])
                self.list.viewport().update()
                return True
        if event.type()==QEvent.KeyPress and self.isVisible():
            if watched is self.search and event.key()==Qt.Key_Down:
                self.list.setFocus();return True
            if watched is self.list and event.key()==Qt.Key_Space and self.list.currentItem() is not None:
                self._favorite(self.list.currentItem().data(Qt.UserRole));return True
        return super().eventFilter(watched,event)
    def _activate(self,item):self._choose(item.data(Qt.UserRole))
    def _choose(self,code):self.hide();self.selected.emit(code)


class VoiceRow(QFrame):
    """One voice card with hover, pressed and selected surfaces."""
    def __init__(self, parent, name, selected=False, *, playable=True):
        super().__init__(parent)
        self.selected = bool(selected)
        self.hover = False
        self.pressed = False
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 6, 4)
        layout.setSpacing(8)
        self.name = label(name, 12, 500, parent=self)
        self.name.setAlignment(Qt.AlignLeft|Qt.AlignVCenter)
        if self.selected:
            css(self.name,"color:@fd_on_action@;background:transparent;border:0;font:500 12px 'FlickDock UI';")
        layout.addWidget(self.name, 1)
        if playable:
            self.play = TranslatorToolButton('play',self)
            self.play.setToolTip('试听 ' + name)
            self.play.setAccessibleName(self.play.toolTip())
            layout.addWidget(self.play)
            self.loading=label("加载中…",11,400,"secondary",self)
            if self.selected:
                css(self.loading,"color:@fd_on_action@;background:transparent;border:0;font:11px 'FlickDock UI';")
            self.loading.hide();layout.addWidget(self.loading)

    def enterEvent(self, event):
        self.hover = True; self.update(); super().enterEvent(event)

    def leaveEvent(self, event):
        self.hover = False; self.pressed = False; self.update(); super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.pressed = True; self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self.pressed = False; self.update(); super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        c = colors(ui_theme.current_theme())
        if self.selected: fill = c['action_pressed'] if self.pressed else c['action']
        elif self.pressed: fill = c['control_pressed']
        elif self.hover: fill = c['control_hover']
        else: fill = 'transparent'
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_rounded_surface(painter, self.rect().adjusted(0, 0, -1, -1), 9,
                              qss_color(fill), qss_color(c['border']) if self.selected else None)
        painter.end()
        super().paintEvent(event)


class VoiceListPopup(TranslatorPopup):
    """Voice picker: name, a type line, an audition button and a selected row."""
    chosen = Signal(str)
    audition = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent, width=356)
        self.search = None
        self.list = picker_list(self.surface)
        self.body.addWidget(self.list)
        self.rows = []
        self._configuration = None
        self.list.itemClicked.connect(self._choose)
        self.list.itemActivated.connect(self._choose)

    def configure(self, voices, current=''):
        if not any(voice['key']==current for voice in voices):
            current=''
        configuration = (tuple((voice['key'], voice['name']) for voice in voices), current)
        if configuration == self._configuration:
            return
        self._configuration = configuration
        self.list.clear()
        self.rows = []
        current_item=None
        if voices:
            item = QListWidgetItem(self.list)
            item.setData(Qt.UserRole, '')
            item.setSizeHint(QSize(330, 38))
            self.list.setItemWidget(item,VoiceRow(self.list,'自动选择音色',not current,playable=False))
            if not current:current_item=item
        for voice in voices:
            item = QListWidgetItem(self.list)
            item.setData(Qt.UserRole, voice['key'])
            item.setSizeHint(QSize(330, 38))
            row = VoiceRow(self.list, voice['name'], voice['key'] == current)
            self.list.setItemWidget(item, row)
            row.play.clicked.connect(lambda checked=False, key=voice['key']: self.audition.emit(key))
            self.rows.append((item, row))
            if voice['key']==current:current_item=item
        if not voices:
            item = QListWidgetItem('当前语言没有可用的音色')
            item.setSizeHint(QSize(330, 40))
            self.list.addItem(item)
        self.list.setFixedHeight(min(self.list.count(),7)*42)
        if current_item is not None:
            self.list.setCurrentItem(current_item)
            if current:self.list.scrollToItem(current_item)

    def set_playback(self,key,phase):
        for item,row in self.rows:
            active=item.data(Qt.UserRole)==key;busy=active and phase=="generating"
            row.loading.setVisible(busy);row.play.setVisible(not busy)
            row.play.set_glyph("close" if active and phase=="playing" else "play")

    def _choose(self, item):
        key = item.data(Qt.UserRole)
        self.hide()
        if key is not None:
            self.chosen.emit(str(key))


class PresetButton(QAbstractButton):
    def __init__(self,text,parent=None):
        super().__init__(parent);self.setText(text);self.setCheckable(True);self.setCursor(Qt.PointingHandCursor)
        font=reference_font();font.setPixelSize(12);self.setFont(font);self.setFixedHeight(28)
        self.setFixedWidth(self.fontMetrics().horizontalAdvance(text)+22)
    def paintEvent(self,event):
        c=colors(ui_theme.current_theme());p=QPainter(self)
        if self.isDown():
            center=QRectF(self.rect()).center();p.translate(center);p.scale(.97,.97);p.translate(-center)
        fill=c['action'] if self.isChecked() else c['control_hover'] if self.underMouse() else c['card']
        paint_rounded_surface(p,self.rect(),14,qss_color(fill),None if self.isChecked() else qss_color(c['input_border']))
        p.setFont(self.font());p.setPen(qss_color(c['on_action'] if self.isChecked() else c['text']));p.drawText(self.rect(),Qt.AlignCenter,self.text());p.end()
