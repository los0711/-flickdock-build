"""Explicit semantic G2 paint for known Qt controls, not a QSS emulator.

Owners supply their existing colors/states. Original style metrics, fonts and
private editor/event behavior stay with Qt. Only this opted-in widget's old
background/border ink is made transparent, once per style/theme change.
"""
from dataclasses import dataclass,replace
from functools import lru_cache
import re

from PySide6.QtCore import QRectF,Qt,QObject,QEvent
from PySide6.QtGui import QColor,QPainter,QRegion
from PySide6.QtWidgets import (QFrame,QLabel,QLineEdit,QPushButton,QToolButton,QWidget,
    QComboBox,QSpinBox,QKeySequenceEdit,QPlainTextEdit,QScrollArea,QScrollBar,QSlider,QMenu,QStackedWidget,
    QStylePainter,QStyleOptionButton,QStyleOptionToolButton,QStyleOptionSlider,QStyle)
from continuous_corners import paint_continuous_panel,continuous_rect_path


@lru_cache(maxsize=256)
def _rgba(value):
    match=re.fullmatch(r'rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([.\d]+)\s*\)',value)
    if match:
        r,g,b=map(int,match.group(1,2,3));raw=match.group(4)
        a=round(float(raw)*255) if '.' in raw and float(raw)<=1 else round(float(raw))
        return r,g,b,max(0,min(255,a))
    return QColor(value).getRgb()


def ink(value):
    return QColor(*_rgba(value)) if isinstance(value,str) else QColor(value)


@dataclass(frozen=True)
class PanelStyle:
    radius: object=8.
    background: object=None
    border: object=None
    border_width: float=1.
    hover_background: object=None
    hover_border: object=None
    pressed_background: object=None
    checked_background: object=None
    disabled_background: object=None
    disabled_border: object=None
    focus_border: object=None
    error_border: object=None
    corner_radii: object=None
    top_border: object=None


def error_state(widget):
    for name in ('error','invalid','validationError','validationState'):
        value=widget.property(name)
        if value is True or isinstance(value,str) and value.lower() in ('true','error','invalid'):return True
    # Keep legacy inline error semantics before neutralizing its ink. No input
    # text/password is inspected and no CSS layout/color model is inferred.
    source=getattr(widget,'_g2_raw_style','')
    return bool(re.search(r'border(?:-color)?\s*:[^;}]*?(?:\bred\b|#ff453a\b|#ff3b30\b|#ef4444\b|#dc2626\b)',source,re.I))


def _resolved(widget,style):
    style=style(widget) if callable(style) else style
    if not isinstance(style,PanelStyle):raise TypeError('G2 recipe must produce PanelStyle')
    fill,border=style.background,style.border
    if not widget.isEnabled():
        if style.disabled_background is not None:fill=style.disabled_background
        if style.disabled_border is not None:border=style.disabled_border
    else:
        if widget.underMouse():
            if style.hover_background is not None:fill=style.hover_background
            if style.hover_border is not None:border=style.hover_border
        if hasattr(widget,'isChecked') and widget.isChecked() and style.checked_background is not None:
            fill=style.checked_background
        if hasattr(widget,'isDown') and widget.isDown() and style.pressed_background is not None:
            fill=style.pressed_background
        if style.focus_border is not None and widget.hasFocus() and (widget.property('keyboardFocus') or getattr(widget,'_g2_keyboard_focus',False)):
            border=style.focus_border
            style=replace(style,border_width=max(1.,style.border_width))
    if style.error_border is not None and error_state(widget):border=style.error_border
    return style,fill,border


def paint_panel(widget,painter,*,background=True,border=True):
    recipe=getattr(widget,'_g2_recipe',None)
    if recipe is None:return None
    style,fill,edge=_resolved(widget,recipe)
    path=paint_continuous_panel(painter,QRectF(widget.rect()),style.radius,
        fill=ink(fill) if background and fill is not None else None,
        border=ink(edge) if border and edge is not None else None,
        border_width=style.border_width,corner_radii=style.corner_radii)
    if border and style.top_border is not None:
        from PySide6.QtGui import QPen
        painter.save();painter.setPen(QPen(ink(style.top_border),style.border_width))
        painter.drawLine(0,round(style.border_width/2),widget.width(),round(style.border_width/2));painter.restore()
    return path


class _ContinuousInk:
    def __init__(self,*args,**kwargs):
        self._g2_recipe=None;self._g2_raw_style='';self._g2_effective_style=None;self._g2_keyboard_focus=False
        super().__init__(*args,**kwargs)

    def _neutral_style(self):
        # An owner-local attribute selector: descendants do not inherit this
        # ink override unless they explicitly have their own recipe too.
        name=self.objectName()
        selector='QWidget'+('#'+name if re.fullmatch(r'[A-Za-z_][\w-]*',name or '') else '')+'[g2Ink="true"]'
        selectors=','.join(selector+state for state in ('',':hover',':pressed',':checked',':disabled',':focus'))
        source=self._g2_raw_style
        # QWidget also accepts a declaration-only sheet. Adding selector rules
        # to that shorthand without an explicit wrapper changes Qt's parser
        # route and can restore an opaque square palette backing.
        if source.strip() and '{' not in source:source='* {'+source+'}'
        return (source+'\n/* continuous-background-ink */\n'+selectors+
                ' {background:transparent;border-color:transparent;border-radius:0;}\n'+
                getattr(self,'_g2_subcontrol_ink','')+'\n/* end-continuous-background-ink */')

    def _refresh_ink(self):
        value=self._neutral_style() if self._g2_recipe is not None else self._g2_raw_style
        if value!=self._g2_effective_style:
            self._g2_effective_style=value
            super().setStyleSheet(value)

    def setStyleSheet(self,value):
        # Ignore only our own suffix on the common "append a repolish comment"
        # path; preserve the original error declarations and all metrics.
        self._g2_raw_style=re.sub(r'\n/\* continuous-background-ink \*/.*?/\* end-continuous-background-ink \*/',
                                  '',str(value),flags=re.S)
        self._refresh_ink()

    def enterEvent(self,event):super().enterEvent(event);self.update()
    def leaveEvent(self,event):super().leaveEvent(event);self.update()
    def focusInEvent(self,event):
        self._g2_keyboard_focus=event.reason() in (Qt.TabFocusReason,Qt.BacktabFocusReason,Qt.ShortcutFocusReason)
        super().focusInEvent(event);self.update()
    def focusOutEvent(self,event):
        self._g2_keyboard_focus=False;super().focusOutEvent(event);self.update()
    def mousePressEvent(self,event):
        self._g2_keyboard_focus=False;super().mousePressEvent(event)


def set_g2_style(widget,style):
    if not isinstance(widget,_ContinuousInk):
        raise TypeError('Use an explicit continuous Qt subclass; no live class/effect replacement')
    if style is not None and not callable(style) and not isinstance(style,PanelStyle):
        raise TypeError('Expected PanelStyle or a semantic recipe callable')
    widget._g2_recipe=style
    if widget.property('g2Ink')!=(style is not None):widget.setProperty('g2Ink',style is not None)
    widget.setAttribute(Qt.WA_Hover,style is not None)
    widget._refresh_ink();widget.update()
    if hasattr(widget,'_g2_plate'):
        from PySide6.QtGui import QPalette
        palette=widget.viewport().palette();palette.setColor(QPalette.Base,Qt.transparent)
        widget.viewport().setPalette(palette);widget.viewport().setAutoFillBackground(False)
        widget._g2_plate.update()
    return widget


class G2Frame(_ContinuousInk,QFrame):
    def paintEvent(self,event):
        if self._g2_recipe is None:return super().paintEvent(event)
        p=QPainter(self);paint_panel(self,p);p.end()


class G2Widget(_ContinuousInk,QWidget):
    def paintEvent(self,event):
        if self._g2_recipe is None:return super().paintEvent(event)
        p=QPainter(self);paint_panel(self,p);p.end()


class G2Label(_ContinuousInk,QLabel):
    def paintEvent(self,event):
        if self._g2_recipe is not None:
            p=QPainter(self);paint_panel(self,p);p.end()
        super().paintEvent(event)


class G2LineEdit(_ContinuousInk,QLineEdit):
    def paintEvent(self,event):
        if self._g2_recipe is None:return super().paintEvent(event)
        p=QPainter(self);paint_panel(self,p,border=False);p.end()
        super().paintEvent(event)
        p=QPainter(self);paint_panel(self,p,background=False);p.end()


class G2PushButton(_ContinuousInk,QPushButton):
    def paintEvent(self,event):
        if self._g2_recipe is None:return super().paintEvent(event)
        p=QPainter(self);paint_panel(self,p,border=False);p.end()
        # CE_Label alone bypasses QSS's original content rect/padding and menu
        # indicator stage. Keep the entire private Qt paint after neutralizing
        # only its frame ink, then paint our exact border above it.
        super().paintEvent(event)
        p=QPainter(self);paint_panel(self,p,background=False);p.end()


class G2ToolButton(_ContinuousInk,QToolButton):
    def paintEvent(self,event):
        if self._g2_recipe is None:return super().paintEvent(event)
        p=QPainter(self);paint_panel(self,p,border=False);p.end()
        super().paintEvent(event)
        p=QPainter(self);paint_panel(self,p,background=False);p.end()


class _PrivateControlPanel(_ContinuousInk):
    def paintEvent(self,event):
        if self._g2_recipe is None:return super().paintEvent(event)
        p=QPainter(self);paint_panel(self,p,border=False);p.end()
        super().paintEvent(event)
        p=QPainter(self);paint_panel(self,p,background=False);p.end()


class G2ComboBox(_PrivateControlPanel,QComboBox):
    _g2_subcontrol_ink='QComboBox[g2Ink="true"]::drop-down{background:transparent;border-color:transparent;border-radius:0;}'


class G2SpinBox(_PrivateControlPanel,QSpinBox):
    _g2_subcontrol_ink=('QSpinBox[g2Ink="true"]::up-button,QSpinBox[g2Ink="true"]::down-button'
        '{background:transparent;border-color:transparent;border-radius:0;}'
        'QSpinBox[g2Ink="true"] QLineEdit{background:transparent;border-color:transparent;border-radius:0;}')


class G2KeySequenceEdit(_PrivateControlPanel,QKeySequenceEdit):
    _g2_subcontrol_ink='QKeySequenceEdit[g2Ink="true"] QLineEdit{background:transparent;border-color:transparent;border-radius:0;}'


class G2StackedWidget(_PrivateControlPanel,QStackedWidget):
    """The native stack owns pages/currentIndex/signals; only its frame is G2."""
    pass


class G2Menu(_PrivateControlPanel,QMenu):
    def set_g2_item_style(self,style,*,insets=(0,0,0,0)):
        self._g2_item_style=style;self._g2_item_insets=insets
        self._g2_subcontrol_ink='QMenu[g2Ink="true"]::item:selected{background:transparent;border-color:transparent;border-radius:0;}'
        self._refresh_ink();self.update()
    def paintEvent(self,event):
        if self._g2_recipe is None:return super().paintEvent(event)
        p=QPainter(self);paint_panel(self,p,border=False)
        active=self.activeAction();style=getattr(self,'_g2_item_style',None)
        if style is not None and active is not None and active.isEnabled() and not active.isSeparator():
            style=style(self) if callable(style) else style
            left,top,right,bottom=self._g2_item_insets
            rect=QRectF(self.actionGeometry(active)).adjusted(left,top,-right,-bottom)
            paint_continuous_panel(p,rect,style.radius,fill=ink(style.background) if style.background else None,
                border=ink(style.border) if style.border else None,border_width=style.border_width)
        p.end();QMenu.paintEvent(self,event)
        p=QPainter(self);paint_panel(self,p,background=False);p.end()


class _NativeSurfaceBacking(QWidget):
    """One passive background below the native child/editor or popup content."""
    def __init__(self,owner):
        super().__init__(owner);self.owner=owner
        self.setAttribute(Qt.WA_TransparentForMouseEvents);self.setAttribute(Qt.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.NoFocus)
    def paintEvent(self,event):
        p=QPainter(self);paint_panel(self.owner,p);p.end()


class _NativeSurface(QObject):
    """Explicit adapter only for Qt-owned popup frames and trailing toolbuttons.

    No private class replacement, paint interception, graphics effect, timer or
    parent capture. Qt keeps painting its text, IME and handling its own events.
    """
    def __init__(self,owner,*,popup):
        super().__init__(owner);self.owner=owner;self.popup=popup
        self.backing=_NativeSurfaceBacking(owner);owner.installEventFilter(self)
    def sync(self):
        owner=self.owner;self.backing.setGeometry(owner.rect());self.backing.lower()
        # Explicit G2 subclasses already paint their own one background.
        self.backing.setVisible(owner.isVisible() and not isinstance(owner,_ContinuousInk))
        if self.popup:
            style,_,_=_resolved(owner,owner._g2_recipe)
            path=continuous_rect_path(QRectF(owner.rect()),style.radius,corner_radii=style.corner_radii)
            owner.setMask(QRegion(path.toFillPolygon().toPolygon()))
        self.backing.update()
    def eventFilter(self,watched,event):
        if event.type() in (QEvent.Resize,QEvent.Show,QEvent.Enter,QEvent.Leave,
                            QEvent.MouseButtonPress,QEvent.MouseButtonRelease,QEvent.EnabledChange):
            self.sync()
        elif event.type()==QEvent.Hide:self.backing.hide()
        return False


def install_native_surface(widget,style,*,popup=False):
    """Opt in a known native Qt popup frame or QLineEdit trailing toolbutton.

    The explicit owner recipe must match the previous owner colors. This is
    deliberately not a generic QSS-to-painter conversion API.
    """
    if not isinstance(style,PanelStyle) and not callable(style):raise TypeError('Expected an explicit PanelStyle')
    if popup:
        if not isinstance(widget,(QFrame,QMenu)):raise TypeError('Expected a Qt popup frame/menu')
    elif not isinstance(widget,QToolButton):raise TypeError('Only native trailing QToolButton is supported')
    source=re.sub(r'\n/\* native-continuous-ink \*/.*?/\* end-native-continuous-ink \*/',
                  '',widget.styleSheet(),flags=re.S)
    widget._g2_raw_style=source;widget._g2_recipe=style
    widget.setProperty('nativeG2Ink',True);widget.setAttribute(Qt.WA_Hover)
    if popup:
        widget.setAttribute(Qt.WA_TranslucentBackground)
        widget.setWindowFlag(Qt.NoDropShadowWindowHint,True);widget.setAutoFillBackground(False)
    if source.strip() and '{' not in source:source='* {'+source+'}'
    selector=('QMenu' if isinstance(widget,QMenu) else 'QFrame' if popup else 'QToolButton')+'[nativeG2Ink="true"]'
    selectors=','.join(selector+s for s in ('',':hover',':pressed',':checked',':disabled',':focus'))
    widget.setStyleSheet(source+'\n/* native-continuous-ink */\n'+selectors+
        '{background:transparent;border-color:transparent;border-radius:0;}\n/* end-native-continuous-ink */')
    controller=getattr(widget,'_native_g2_surface',None)
    if controller is None:
        controller=widget._native_g2_surface=_NativeSurface(widget,popup=popup)
    elif controller.popup!=popup:raise ValueError('Native surface type cannot change')
    controller.sync();widget.update();return controller


class G2ScrollBar(_ContinuousInk,QScrollBar):
    _g2_subcontrol_ink='QScrollBar[g2Ink="true"]::handle{background:transparent;border-color:transparent;border-radius:0;}'
    def paintEvent(self,event):
        if self._g2_recipe is None:return super().paintEvent(event)
        option=QStyleOptionSlider();self.initStyleOption(option)
        handle=self.style().subControlRect(QStyle.CC_ScrollBar,option,QStyle.SC_ScrollBarSlider,self)
        option.subControls=QStyle.SC_All&~QStyle.SC_ScrollBarSlider
        p=QStylePainter(self);p.drawComplexControl(QStyle.CC_ScrollBar,option)
        style,fill,border=_resolved(self,self._g2_recipe)
        paint_continuous_panel(p,QRectF(handle),style.radius,fill=ink(fill) if fill else None,
            border=ink(border) if border else None,border_width=style.border_width);p.end()


class G2ScrollArea(QScrollArea):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.setVerticalScrollBar(G2ScrollBar(Qt.Vertical,self))
        self.setHorizontalScrollBar(G2ScrollBar(Qt.Horizontal,self))


class _EditorBackplate(QWidget):
    def __init__(self,owner):
        super().__init__(owner);self.owner=owner
        self.setAttribute(Qt.WA_TransparentForMouseEvents);self.setAttribute(Qt.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.NoFocus);self.lower()
    def paintEvent(self,event):
        p=QPainter(self);paint_panel(self.owner,p);p.end()


class G2PlainTextEdit(_ContinuousInk,QPlainTextEdit):
    """Native document/IME renderer over one passive frame backing, no effect."""
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.setVerticalScrollBar(G2ScrollBar(Qt.Vertical,self));self.setHorizontalScrollBar(G2ScrollBar(Qt.Horizontal,self))
        self._g2_plate=_EditorBackplate(self);self._g2_plate.setGeometry(self.rect())
    def resizeEvent(self,event):
        super().resizeEvent(event)
        self._g2_plate.setGeometry(self.rect());self._g2_plate.lower()


class G2Slider(_ContinuousInk,QSlider):
    _g2_subcontrol_ink=('QSlider[g2Ink="true"]::groove,QSlider[g2Ink="true"]::sub-page'
                       '{background:transparent;border-color:transparent;border-radius:0;}')
    def set_g2_parts(self,groove,fill,handle=None):
        self._g2_fill_style=fill;self._g2_handle_style=handle
        self._g2_subcontrol_ink=type(self)._g2_subcontrol_ink+(
            'QSlider[g2Ink="true"]::handle{background:transparent;border-color:transparent;border-radius:0;}'
            if handle is not None else '')
        set_g2_style(self,groove)
    def paintEvent(self,event):
        if self._g2_recipe is None:return super().paintEvent(event)
        option=QStyleOptionSlider();self.initStyleOption(option)
        groove=self.style().subControlRect(QStyle.CC_Slider,option,QStyle.SC_SliderGroove,self)
        handle=self.style().subControlRect(QStyle.CC_Slider,option,QStyle.SC_SliderHandle,self)
        p=QStylePainter(self);style,fill,border=_resolved(self,self._g2_recipe)
        paint_continuous_panel(p,QRectF(groove),style.radius,fill=ink(fill) if fill else None,
            border=ink(border) if border else None,border_width=style.border_width)
        filled=QRectF(groove)
        if self.orientation()==Qt.Horizontal:
            filled.setLeft(handle.center().x()) if option.upsideDown else filled.setRight(handle.center().x())
        else:filled.setTop(handle.center().y()) if option.upsideDown else filled.setBottom(handle.center().y())
        active=self._g2_fill_style
        paint_continuous_panel(p,filled,active.radius,fill=ink(active.background),border_width=0)
        handle_recipe=getattr(self,'_g2_handle_style',None)
        if handle_recipe is not None:
            thumb,fill,border=_resolved(self,handle_recipe)
            rect=QRectF(handle)
            # Existing exact circular knobs are already G-infinity. Keep that
            # artwork; a non-square CSS capsule/rounded rectangle needs G2.
            circle=abs(rect.width()-rect.height())<.01 and isinstance(thumb.radius,(float,int)) and thumb.radius>=rect.width()/2
            if circle:
                from PySide6.QtGui import QPen
                p.setPen(QPen(ink(border),thumb.border_width) if border and thumb.border_width else Qt.NoPen)
                p.setBrush(ink(fill) if fill else Qt.NoBrush)
                inset=thumb.border_width/2 if border else 0
                p.drawEllipse(rect.adjusted(inset,inset,-inset,-inset))
            else:paint_continuous_panel(p,rect,thumb.radius,fill=ink(fill) if fill else None,
                border=ink(border) if border else None,border_width=thumb.border_width)
        option.subControls=QStyle.SC_SliderTickmarks if handle_recipe is not None else QStyle.SC_SliderHandle|QStyle.SC_SliderTickmarks
        p.drawComplexControl(QStyle.CC_Slider,option);p.end()
