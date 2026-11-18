"""Native translator controls using FlickDock surfaces and display clock."""
from collections import OrderedDict
from functools import cache
from pathlib import Path
import math
import time
import weakref
from shiboken6 import isValid

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QSignalBlocker, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QIcon, QLinearGradient, QPainter, QPalette, QPen, QPixmap, QRegion
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QToolButton, QAbstractButton, QApplication, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QStyledItemDelegate, QStyle, QVBoxLayout, QWidget)

from native_ui_common import (PopupChevronMotion, RoundedScrollBar, RoundedSurfaceFrame, colors, icon, paint_rounded_surface,
                              qss_color, reduced_motion, reference_font, rounded_popup, screen_for_widget)
from display_frames import DisplayAnimation, FramePulse
from reference_widgets import IconTextButton, ToolbarButton, button, css, label
from surface_morph import paint_surface_shadow
from translation_languages import TARGETS
import ui_theme

ACTION_SIZE=28


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


def translator_action(text,glyph,parent,*,variant='secondary'):
    control=button(text,glyph,variant,parent,control_type=TranslatorActionButton)
    control.setFixedHeight(ACTION_SIZE)
    control.setIconSize(QSize(14,14))
    control.setProperty('iconTextGap',4)
    control.setProperty('iconTextReservedWidth',16)
    styles=[]
    for theme in ('dark','light'):
        c=colors(theme);ink='@fd_on_action@' if variant=='primary' else c['text']
        styles.append(f"QPushButton{{background:transparent;color:{ink};border:0;padding:2px 7px;font:500 11px 'FlickDock UI';}}"
                      f"QPushButton:disabled{{color:{c['muted']};}}")
    css(control,*styles)
    return control


def picker_list(parent):
    view=QListWidget(parent)
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
    def __init__(self,parent=None,*,role='card',radius=12,border=True):
        super().__init__(parent,content_inset=0);self.role=role;self.radius=radius;self.border=border
    def surface_style(self):
        c=colors(ui_theme.current_theme());return c[self.role],c['border'] if self.border else None,self.radius


class TranslatorSplitAction(TranslatorSurface):
    def __init__(self,text,glyph,parent):
        super().__init__(parent,role='control',radius=8)
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
        super().__init__(parent,role='input')
        self._active=False;self._started_at=0.
        self._rim=QRegion()
        self._frames=FramePulse(self);self._frames.timeout.connect(self._tick)

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
        self.surface=TranslatorSurface(self,role='popup_surface');outer.addWidget(self.surface)
        self.body=QVBoxLayout(self.surface);self.body.setContentsMargins(12,12,12,12);self.body.setSpacing(10)
        self._anchor=None
    def paintEvent(self,event):
        p=QPainter(self);p.setCompositionMode(QPainter.CompositionMode_Source);p.fillRect(self.rect(),Qt.transparent);p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        paint_surface_shadow(p,QRectF(self.surface.geometry()),self._shadow,10,QPoint(0,2),QColor(0,0,0,60),radius=12);p.end()
    def hideEvent(self,event):
        QApplication.instance().removeEventFilter(self)
        self.closed_at=time.monotonic();super().hideEvent(event)

    def eventFilter(self,watched,event):
        if self.isVisible() and event.type()==QEvent.MouseButtonPress and self._anchor is not None:
            anchor=self._anchor()
            if anchor is not None and isValid(anchor) and anchor.isVisible() and anchor.rect().contains(anchor.mapFromGlobal(event.globalPosition().toPoint())):
                self.setAttribute(Qt.WA_NoMouseReplay,True);self.hide();return True
        return False

    def popup_below(self,anchor):
        self._anchor=weakref.ref(anchor);QApplication.instance().installEventFilter(self)
        self.setAttribute(Qt.WA_NoMouseReplay,True)
        self.adjustSize();point=anchor.mapToGlobal(QPoint(0,anchor.height()+4))-QPoint(8,0)
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
    def set_language(self, code, detected=''):
        name,region=TARGETS.get(code,('自动检测',''))
        if code=='auto' and detected:
            name='识别为 '+detected
        if self.text()==name and self._region==region:
            return
        self._region=region
        self.setText(name);self.updateGeometry();self.update()
    def sizeHint(self):return QSize(self.fontMetrics().horizontalAdvance(self.text())+42+(24 if self._region else 0),32)
    def paintEvent(self,event):
        c=colors(ui_theme.current_theme());p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        if self.isDown():
            center=QRectF(self.rect()).center();p.translate(center);p.scale(.97,.97);p.translate(-center)
        if not self.compact or self.underMouse() or self.isDown():
            paint_rounded_surface(p,self.rect(),8,qss_color(c['control_pressed'] if self.isDown() else c['control_hover'] if self.underMouse() else c['control']),None if self.compact else qss_color(c['input_border']))
        p.setFont(self.font());p.setPen(QColor(c['secondary'] if self.compact else c['text']))
        left=5 if self.compact else 10
        flag=language_flag(self._region)
        if not flag.isNull():
            flag.paint(p,left,(self.height()-14)//2,18,14)
            left+=24
        available=max(1,self.width()-left-26)
        text=p.fontMetrics().elidedText(self.text(),Qt.ElideRight,available)
        p.drawText(QRectF(left,0,available,self.height()),Qt.AlignVCenter|Qt.AlignLeft,text)
        p.translate(self.width()-(10 if self.compact else 17),self.height()/2);p.rotate(self.chevron.angle);p.setPen(QPen(QColor(c['secondary']),1.3,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin))
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
        if selected or hover==point or pressed==point:
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
                    self._hovered=hit;self.list.viewport().update()
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


class _TipWindow(QWidget):
    def __init__(self):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.text = ''
        self._tip_font = reference_font()
        self._tip_font.setPixelSize(12)

    def sizeHint(self):
        metrics = QFontMetrics(self._tip_font)
        return QSize(metrics.horizontalAdvance(self.text) + 22, metrics.height() + 16)

    def paintEvent(self, event):
        c = colors(ui_theme.current_theme())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_rounded_surface(painter, self.rect(), 8, qss_color(c['popup_surface']), qss_color(c['border']))
        painter.setFont(self._tip_font)
        painter.setPen(QColor(c['text']))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text)
        painter.end()


class HoverTip(QObject):
    """Reliable hover hints: one small rounded window instead of QToolTip."""
    def __init__(self, scope):
        super().__init__(scope)
        self.scope = scope
        self._target = None
        self._window = _TipWindow()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(380)
        self._timer.timeout.connect(self._show)
        app = QApplication.instance()
        if app is not None: app.installEventFilter(self)

    def _inside(self, widget):
        try:
            return widget is self.scope or self.scope.isAncestorOf(widget)
        except RuntimeError:
            return False

    def eventFilter(self, watched, event):
        if not isinstance(watched, QWidget): return False
        kind = event.type()
        if kind==QEvent.Resize and watched is self.scope:
            if self._target is not None:self._hide()
            return False
        if kind==QEvent.ToolTip and self._inside(watched):
            event.accept();return True
        if kind in (QEvent.Type.Enter, QEvent.Type.HoverEnter):
            if self._inside(watched) and ui_theme.allows_hover_hint(watched) and watched.isVisible() and watched.toolTip().strip():
                self._target = watched
                self._timer.start()
            elif self._target is not None:
                self._hide()
        elif kind in (QEvent.Type.Leave, QEvent.Type.Hide, QEvent.Type.MouseButtonPress):
            if watched is self._target: self._hide()
        return False

    def _show(self):
        widget = self._target
        if widget is None or not isValid(widget) or not ui_theme.allows_hover_hint(widget): return
        try:
            if not widget.isVisible() or not widget.underMouse(): return
            text = widget.toolTip().strip()
        except RuntimeError:
            return
        if not text: return
        self._window.text = text
        self._window.resize(self._window.sizeHint())
        anchor = widget.mapToGlobal(QPoint(0, widget.height() + 6))
        x = anchor.x() + widget.width() // 2 - self._window.width() // 2
        y = anchor.y()
        screen = screen_for_widget(widget)
        if screen is not None:
            area = screen.availableGeometry()
            if y + self._window.height() > area.bottom() + 1:
                y = widget.mapToGlobal(QPoint(0, 0)).y() - self._window.height() - 6
            x = max(area.left() + 4, min(x, area.right() - self._window.width() - 4))
            y = max(area.top() + 4, y)
        self._window.move(x, y)
        self._window.show()
        self._window.raise_()

    def _hide(self):
        self._timer.stop(); self._target = None
        if self._window.isVisible(): self._window.hide()

    def close(self):
        self._hide(); self._window.deleteLater()


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
        fill=c['action'] if self.isChecked() else c['hover'] if self.underMouse() else 'transparent'
        paint_rounded_surface(p,self.rect(),14,qss_color(fill),None if self.isChecked() else qss_color(c['input_border']))
        p.setFont(self.font());p.setPen(qss_color(c['on_action'] if self.isChecked() else c['secondary']));p.drawText(self.rect(),Qt.AlignCenter,self.text());p.end()
