"""Native Qt primitives transcribed from the supplied 最终版.html.

The source's CSS tokens and SVG paths are retained here; no browser runtime is
used. Windows and macOS keep the separate radius, font and titlebar treatments
specified by that document.
"""
from __future__ import annotations
import re
import math

import sys
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QByteArray, QEasingCurve, QEvent, QObject, QPointF, QRectF, QSize, Qt, QVariantAnimation, QTranslator
from PySide6.QtGui import QBrush, QColor, QFont, QFontDatabase, QIcon, QIconEngine, QPainter, QPainterPath, QPen, QPixmap, QGuiApplication, QRadialGradient, QRegion, QTransform
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QAbstractButton, QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QScrollBar, QWidget


FONT_WEIGHT_AXIS=QFont.Tag('wght')
FONT_TABULAR_NUMBERS=QFont.Tag('tnum')
TECHNICAL_FONT_FAMILY='JetBrains Mono'
BUNDLED_FONT_FILES=('JetBrainsMono-Regular.ttf','JetBrainsMono-Medium.ttf')
RADIUS_SMALL_PX = 6
RADIUS_SUBCONTROL_PX = 8
RADIUS_CONTROL_PX = 12
RADIUS_CARD_PX = 20
RADIUS_PANEL_PX = 24
RADIUS_WINDOW_PX = 28
SEGMENT_INSET_PX = 3
SHELF_HEADER_BUTTON_SIZE_PX = 32
SHELF_HEADER_BUTTON_GAP_PX = 6
SHELF_HEADER_RIGHT_MARGIN_PX = 16
SHELF_COMPACT_HEADER_HEIGHT_PX = 48
SHELF_OVERVIEW_HEADER_HEIGHT_PX = 53
DROP_DOT_SPACING_PX = 9


class DropBorderMotion(QObject):
    """Shared dotted drop surface with pointer tracking and a short landing pulse."""

    LANDING_MS=240

    def __init__(self,widget):
        super().__init__(widget)
        from display_frames import DisplayAnimation
        from flickdock_motion import Spring
        from ui_transitions import DROP_HOVER_TRANSITION_MS
        self.widget=widget
        self.active=False
        self.dragging=False
        self.progress=0.
        self.landing_progress=0.
        self._has_pointer=False
        self._dots_key=None
        self._dot_field=QPixmap()
        self._columns=self._rows=()
        self.animation=DisplayAnimation(self)
        self.animation.setDuration(DROP_HOVER_TRANSITION_MS)
        self.animation.setEasingCurve(QEasingCurve.OutCubic)
        self.animation.valueChanged.connect(self._changed)
        self.pointer=Spring(self,{'x':0.,'y':0.},self._update)
        self.landing=DisplayAnimation(self)
        self.landing.setDuration(self.LANDING_MS)
        self.landing.setStartValue(1.);self.landing.setEndValue(0.)
        self.landing.setEasingCurve(QEasingCurve.OutCubic)
        self.landing.valueChanged.connect(self._landing_frame)
        widget.installEventFilter(self)

    def _update(self,_=None):
        self.widget.update()

    def set_active(self,active):
        active=bool(active)
        if active==self.active:return
        self.active=active;self.animation.stop();target=float(active)
        if not self.widget.isVisible() or reduced_motion():self._changed(target);return
        self.animation.setStartValue(self.progress);self.animation.setEndValue(target);self.animation.start()

    def move_drag(self,point):
        first=not self.dragging
        self.dragging=True
        if first:
            self.landing.stop()
            self.landing_progress=0.
        target={'x':float(point.x()),'y':float(point.y())}
        if not self._has_pointer or reduced_motion():self.pointer.jump(target)
        elif target!=self.pointer._targets:self.pointer.animate_physical(target,560,44)
        self._has_pointer=True
        self.set_active(True)

    def end_drag(self,*,accepted=False,hovered=False):
        was_dragging=self.dragging
        self.dragging=False
        self._has_pointer=False
        self.pointer.stop()
        self.set_active(hovered)
        if accepted and was_dragging and self.widget.isVisible() and not reduced_motion():
            self._landing_frame(1.)
            self.landing.start()

    def reset(self):
        self.pointer.stop();self.landing.stop();self.animation.stop()
        self._has_pointer=False;self.dragging=False;self.active=False
        self.progress=self.landing_progress=0.
        self._update()

    def eventFilter(self,watched,event):
        if event.type()==QEvent.Hide:self.reset()
        elif event.type()==QEvent.Show:
            self.animation.stop();self._changed(float(self.active))
        return False

    def _changed(self,value):
        if reduced_motion() and self.animation.state()==self.animation.Running:
            self.animation.stop();value=float(self.active)
        self.progress=float(value)
        self._update()

    def _landing_frame(self,value):
        self.landing_progress=float(value)
        self._update()

    def color(self,first,last):
        return QColor.fromRgbF(*(a+(b-a)*self.progress for a,b in zip(QColor(first).getRgbF(),QColor(last).getRgbF())))

    @staticmethod
    def _axis(start,extent):
        step=DROP_DOT_SPACING_PX
        count=max(0,math.floor((extent-12)/step)+1)
        first=start+(extent-max(0,count-1)*step)/2
        return tuple(first+index*step for index in range(count))

    def _field(self,rect,dpr,ink,quiet):
        key=(rect,dpr,ink.rgba(),quiet)
        if self._dots_key==key:return self._dot_field
        self._dots_key=key
        self._columns=self._axis(rect.left(),rect.width())
        self._rows=self._axis(rect.top(),rect.height())
        field=QPixmap(math.ceil(rect.width()*dpr),math.ceil(rect.height()*dpr))
        field.setDevicePixelRatio(dpr);field.fill(Qt.transparent)

        # Reuse one antialiased dot across the grid. Only redo this bitmap when
        # geometry, density or color changes, never on a pointer/landing frame.
        side=max(3,math.ceil(3*dpr))
        stamp=QPixmap(side,side)
        stamp.setDevicePixelRatio(dpr);stamp.fill(Qt.transparent)
        half=side/(2*dpr)
        painter=QPainter(stamp);painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen);painter.setBrush(ink)
        painter.drawEllipse(QPointF(half,half),.68,.68)
        painter.end()

        painter=QPainter(field)
        for y in self._rows:
            for x in self._columns:
                painter.drawPixmap(QPointF(x-rect.left()-half,y-rect.top()-half),stamp)
        if quiet:
            centre=QPointF(rect.width()/2,rect.height()/2)
            mist=QRadialGradient(centre,min(98.,max(29.,rect.width()*.30)))
            mist.setColorAt(0.,QColor(0,0,0,56));mist.setColorAt(.5,QColor(0,0,0,56))
            mist.setColorAt(1.,QColor(0,0,0,255))
            painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
            painter.fillRect(QRectF(0,0,rect.width(),rect.height()),mist)
        painter.end()
        self._dot_field=field
        return field

    def _paint_pointer_dots(self,painter,point,ink,centre,quiet):
        gain=self.progress if self.dragging else self.landing_progress
        if gain<=0.:return
        radius=76. if self.dragging else 46.+52.*(1.-gain)
        levels=[QPainterPath() for _ in range(4)]
        px,py=point.x(),point.y()
        for y in self._rows:
            dy=y-py
            if abs(dy)>=radius:continue
            for x in self._columns:
                distance=math.hypot(x-px,dy)
                if distance>=radius:continue
                strength=(1.-distance/radius)**1.6
                if quiet:
                    text_distance=math.hypot((x-centre.x())*.65,y-centre.y())
                    strength*=min(1.,.18+text_distance/40.)
                level=min(3,int(strength*4))
                dot=.75+strength*1.5*gain
                levels[level].addEllipse(QPointF(x,y),dot,dot)
        for level,path in enumerate(levels):
            shade=QColor(ink);shade.setAlpha(round(gain*(28+level*42)))
            painter.fillPath(path,shade)

    def paint(self,painter,path,*,dark,radius,width=1.,idle_fill=None,quiet=True):
        bounds=path.boundingRect()
        if bounds.isEmpty():return
        theme=colors('dark' if dark else 'light')
        ink=qss_color(theme['text'])
        accent=qss_color(theme['action'])
        fill=qss_color(theme['card']) if idle_fill is None else QColor(idle_fill)
        outline=QColor(ink);outline.setAlpha(38 if dark else 44)
        active_edge=QColor(accent);active_edge.setAlpha(148 if dark else 168)
        hover_edge=QColor(ink);hover_edge.setAlpha(82 if dark else 83)
        point=QPointF(self.pointer.values['x'],self.pointer.values['y'])
        centre=bounds.center()
        gain=self.progress if self.dragging else self.landing_progress
        scale=abs(painter.deviceTransform().m11()) or 1.
        rect,width=pixel_aligned_border(painter,bounds,width/scale)
        radius=min(round(radius*scale)/scale,rect.width()/2,rect.height()/2)
        path=QPainterPath();path.addRoundedRect(rect,radius,radius)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        if gain>0. and not reduced_motion():
            # Keep the moving appearance inside the fixed native hit area.
            transform=QTransform()
            dx=max(-1.,min(1.,(point.x()-centre.x())/max(1.,bounds.width()/2)))
            dy=max(-1.,min(1.,(point.y()-centre.y())/max(1.,bounds.height()/2)))
            transform.translate(centre.x(),centre.y())
            transform.scale(1.-.028*gain,1.-.04*gain)
            transform.shear(-.027*dy*gain,-.019*dx*gain)
            transform.translate(-centre.x(),-centre.y())
            painter.setWorldTransform(transform,True)
        painter.save()
        painter.setClipPath(path,Qt.IntersectClip)
        painter.fillPath(path,fill)
        dot=QColor(ink);dot.setAlpha(53 if dark else 51)
        dpr=painter.device().devicePixelRatioF()
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.drawPixmap(bounds.topLeft(),self._field(bounds,dpr,dot,quiet))
        self._paint_pointer_dots(painter,point,accent,centre,quiet)
        painter.restore()
        pen=QPen(self.color(outline,active_edge if self.dragging else hover_edge),width)
        painter.setPen(pen);painter.setBrush(Qt.NoBrush);painter.drawPath(path)
        painter.restore()


class PopupChevronMotion(QObject):
    """Interruptible, display-synchronised rotation for popup indicators."""
    def __init__(self,widget):
        super().__init__(widget);self.widget=widget;self.angle=0.;self.popup=None
        from flickdock_motion import Spring
        self.animation=Spring(self,{'angle':0.},lambda values:self._changed(values['angle']))

    def attach(self,popup):
        if self.popup is popup:return
        if self.popup is not None:
            try:self.popup.removeEventFilter(self)
            except RuntimeError:pass
        self.popup=popup
        if popup is not None:
            popup._dropdown_anchor=self.widget
            popup.installEventFilter(self)
        else:self._transition(False)

    def _changed(self,value):self.angle=float(value);self.widget.update()

    def _transition(self,opened):
        target=180. if opened else 0.
        if not self.widget.isVisible() or reduced_motion():self.animation.jump({'angle':target});return
        self.animation.animate_physical({'angle':target},300,18)

    def eventFilter(self,watched,event):
        if watched is getattr(self,'popup',None):
            if event.type()==QEvent.Show:self._transition(True)
            elif event.type()==QEvent.Hide:self._transition(False)
        return False


class _TranslucentPopup(QObject):
    """Clip native item-view containers even when their alpha backing is opaque."""
    def sync_mask(self, popup):
        radius=getattr(popup,'_popup_clip_radius',None)
        if radius is None:
            if not popup.mask().isEmpty():popup.clearMask()
            return
        path=QPainterPath()
        path.addRoundedRect(QRectF(popup.rect()),radius,radius)
        region=QRegion(path.toFillPolygon().toPolygon())
        if region!=popup.mask():popup.setMask(region)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.sync_mask(watched)
        return False


def rounded_popup(popup, *, clip_radius=None):
    """Keep native popup behavior while its surface paints antialiased alpha."""
    if not hasattr(popup, '_translucent_popup'):
        popup.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground,clip_radius is None)
        popup.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
        popup.setWindowFlag(Qt.WindowType.FramelessWindowHint,True)
        popup.setAutoFillBackground(False)
        popup._translucent_popup=_TranslucentPopup(popup)
        popup.installEventFilter(popup._translucent_popup)
        from dropdown_motion import install_dropdown_motion
        install_dropdown_motion(popup)
    if clip_radius is not None:popup._popup_clip_radius=clip_radius
    popup._translucent_popup.sync_mask(popup)


def screen_for_widget(widget):
    """Resolve a widget's display without transferring its wrapper to a window.

    PySide 6.12 gives QWidget.screen()/QWindow.screen() a wrapper parent of
    the calling window. Closing a transient window then invalidates the same
    QScreen wrapper held by other windows, although Qt's screen is still live.
    The application-owned screen lookup avoids that lifetime coupling.
    """
    geometry=widget.window().frameGeometry()
    screen=QGuiApplication.screenAt(geometry.center())
    if screen is not None:return screen
    screens=QGuiApplication.screens()
    if not screens:return None
    def overlap(candidate):
        intersection=geometry.intersected(candidate.geometry())
        return max(0,intersection.width())*max(0,intersection.height())
    nearest=max(screens,key=overlap)
    return nearest if overlap(nearest)>0 else QGuiApplication.primaryScreen() or screens[0]


def toggle_window_zoom(window):
    """Zoom a frameless utility window in its current work area, then restore."""
    previous=getattr(window,'_flickdock_zoom_restore',None)
    if previous is not None:
        window.setGeometry(previous);window._flickdock_zoom_restore=None
    elif (screen:=screen_for_widget(window)) is not None:
        window._flickdock_zoom_restore=window.geometry()
        window.setGeometry(screen.availableGeometry())

def readable_size(value):
    value=int(value)
    return value+(1 if value>=12 else 2) if sys.platform=='win32' else value

def flat_qss(source):
    def block(match):
        selector,body=match.groups()
        if not re.search(r':focus|keyboardFocus|::indicator|::handle',selector):
            def outline(declaration):
                value=declaration[1].strip()
                if value in ('0','none','0px'):return declaration[0]
                width=re.match(r'([\d.]+px)\s+',value)
                return 'border:'+(width[1] if width else '1px')+' solid transparent'
            body=re.sub(r'(?<![\w-])border\s*:\s*([^;{}]+)',outline,body)
            body=re.sub(r'(?<![\w-])border-color\s*:\s*[^;{}]+','border-color:transparent',body)
        return selector+'{'+body+'}'
    return re.sub(r'([^{}]+)\{([^{}]*)\}',block,source)


_RE_FONT_WEIGHT=re.compile(r'(font-weight\s*:\s*)([1-9]00|bold|normal)\b',re.I)
_RE_FONT_SHORT_W=re.compile(r'(font\s*:\s*)([1-9]00)(?=\s+\d+px)',re.I)
_RE_FONT_SIZE=re.compile(r'(font-size\s*:\s*)(\d+)(px)')
_RE_FONT_SHORT_S=re.compile(r'(font\s*:\s*(?:[1-9]00\s+)?)(\d+)(px)')


def readable_qss(source):
    """Apply the shared type scale and reduce emphasis once per stylesheet."""
    if 'flickdock-readable-type' in source:return source
    def weight(match):
        value=match[2].lower()
        return match[1]+str(_ui_weight(700 if value=='bold' else 400 if value=='normal' else int(value)))
    source=_RE_FONT_WEIGHT.sub(weight,source)
    source=_RE_FONT_SHORT_W.sub(weight,source)
    if sys.platform == 'win32':
        source=_RE_FONT_SIZE.sub(lambda m:m[1]+str(readable_size(m[2]))+m[3],source)
        source=_RE_FONT_SHORT_S.sub(lambda m:m[1]+str(readable_size(m[2]))+m[3],source)
    return source+'\n/* flickdock-readable-type */'


_REDUCED_MOTION_CACHE=(0.0,False)


def reduced_motion():
    """Read the platform preference with a short TTL cache to avoid per-frame OS syscalls."""
    global _REDUCED_MOTION_CACHE
    if any(os.environ.get(key,'').lower() in ('1','true','yes','on')
           for key in ('FLICKDOCK_REDUCE_MOTION','FLICKDOCK_LOW_PERFORMANCE')):return True
    now=time.monotonic()
    if now-_REDUCED_MOTION_CACHE[0]<3.0:
        return _REDUCED_MOTION_CACHE[1]
    result=False
    if sys.platform == 'darwin':
        try:
            from AppKit import NSWorkspace
            result=bool(NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())
        except Exception:
            result=False
    elif sys.platform == 'win32':
        try:
            import ctypes
            value = ctypes.c_int(1)
            ok = ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(value), 0)
            result=bool(ok and not value.value)
        except (AttributeError, OSError):
            result=False
    _REDUCED_MOTION_CACHE=(now,result)
    return result


def keyboard_focus_visible(widget):
    return bool(widget.hasFocus() and widget.property('keyboardFocus'))


def qss_color(value):
    """Convert the palette's Qt CSS colors into QColor without losing alpha."""
    if isinstance(value,str) and value.startswith('rgba('):
        values=[int(float(part)) for part in re.findall(r'[\d.]+',value)]
        if len(values)!=4:raise ValueError('Invalid rgba color: '+value)
        result=QColor(*values)
    else:result=QColor(value)
    if not result.isValid():raise ValueError('Invalid color: '+str(value))
    return result


def pixel_aligned_border(painter, rect, width=1.0):
    """Align a rounded-rectangle stroke to whole device pixels at rest.

    Antialiasing remains enabled for curved corners. Non-axis-aligned or
    nonuniform transforms keep the original geometry during animations.
    """
    r=QRectF(rect);t=painter.deviceTransform();sx=t.m11();sy=t.m22()
    if sx<=0 or sy<=0 or abs(t.m12())>1e-6 or abs(t.m21())>1e-6 or abs(sx-sy)>1e-6:
        return r.adjusted(width/2,width/2,-width/2,-width/2),float(width)
    pixels=max(1,round(float(width)*sx));logical=pixels/sx;half=pixels/2
    # A widget whose native origin lies between device pixels is clipped to
    # the inside pixel by Qt's backing store. Rounding outward can place its
    # entire top/left stroke outside that clip, even though grab() on the
    # widget alone looks correct. Snap both edges inward instead.
    epsilon=1e-7
    left=(math.ceil(r.left()*sx+t.dx()-epsilon)+half-t.dx())/sx
    right=(math.floor(r.right()*sx+t.dx()+epsilon)-half-t.dx())/sx
    top=(math.ceil(r.top()*sy+t.dy()-epsilon)+half-t.dy())/sy
    bottom=(math.floor(r.bottom()*sy+t.dy()+epsilon)-half-t.dy())/sy
    if right<=left or bottom<=top:return r.adjusted(logical/2,logical/2,-logical/2,-logical/2),logical
    return QRectF(left,top,right-left,bottom-top),logical


def paint_rounded_surface(painter, rect, radius_logical, fill, stroke=None, stroke_px=1, *, border_widget=None):
    """Paint one shared AA path with its outline aligned to device pixels.

    The public stroke width is in physical pixels; layout and radius remain in
    logical pixels. The painter state belongs to the caller and is restored.
    """
    if stroke is not None and stroke_px<=0:raise ValueError('stroke_px must be positive')
    bounds=QRectF(rect)
    if bounds.isEmpty():return
    transform=painter.deviceTransform()
    scale=abs(transform.m11()) or 1.
    width=0.
    if stroke is not None:bounds,width=pixel_aligned_border(painter,bounds,float(stroke_px)/scale)
    radius=max(0.,round(float(radius_logical)*scale)/scale)
    radius=min(radius,bounds.width()/2,bounds.height()/2)
    path=QPainterPath();path.addRoundedRect(bounds,radius,radius)
    painter.save()
    try:
        painter.setRenderHint(QPainter.Antialiasing,True)
        if fill is not None:painter.fillPath(path,QBrush(fill))
        if stroke is not None:
            painter.setPen(QPen(QBrush(stroke),width));painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)
            reveal=getattr(QApplication.instance(),'_flickdock_border_reveal',None)
            if reveal is not None and QColor(stroke).alpha():reveal.paint_stroke(painter,path,width,widget=border_widget)
    finally:painter.restore()


class RoundedSurfaceFrame(QFrame):
    """A reusable frame whose surface is painted separately from its layout."""
    def __init__(self,parent=None,*,content_inset=1):
        super().__init__(parent);self.setFrameShape(QFrame.NoFrame)
        self.setContentsMargins(content_inset,content_inset,content_inset,content_inset)

    def surface_style(self):
        """Return (fill, stroke, logical radius), or None for a clear frame."""
        raise NotImplementedError

    def paintEvent(self,event):
        style=self.surface_style()
        if style is None:return
        fill,stroke,radius=style;painter=QPainter(self)
        paint_rounded_surface(painter,self.rect(),radius,qss_color(fill),
                              None if stroke is None else qss_color(stroke))
        painter.end()


class RoundedPopupBackdrop(QWidget):
    """Antialiased backing for native popup containers and their child views."""
    def __init__(self,popup,radius=RADIUS_CONTROL_PX):
        super().__init__(popup);self.radius=radius;self.fill='#FFFFFF';self.stroke='#D8D8D8'
        self.setAttribute(Qt.WA_TransparentForMouseEvents);self.setAttribute(Qt.WA_NoSystemBackground)
        popup.installEventFilter(self);self.setGeometry(popup.rect());self.lower()

    def eventFilter(self,watched,event):
        if watched is self.parentWidget() and event.type() in (QEvent.Resize,QEvent.Show):
            self.setGeometry(watched.rect());self.lower()
        return False

    def paintEvent(self,event):
        painter=QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        painter.fillRect(self.rect(),Qt.transparent)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        paint_rounded_surface(painter,self.rect(),self.radius,qss_color(self.fill),
                              None if self.stroke is None else qss_color(self.stroke))
        painter.end()


def prepare_combo_popup(popup, view, colors, *, margin, highlight_insets=None):
    """Paint the native combo backing and the row view with a single palette."""
    from PySide6.QtGui import QPalette
    if QGuiApplication.platformName()=='cocoa':
        # A second native window during the opening spring can interfere with
        # Cocoa's popup tracking. Composite into the existing popup instead.
        popup.setProperty('flickdockInlineDropdown',True)
    rounded_popup(popup)
    fill=colors['popup_surface']
    signature=(view,fill,colors['input_border'],margin,highlight_insets)
    if getattr(popup,'_combo_presentation_style',None)==signature:return
    popup._combo_presentation_style=signature
    popup.setObjectName('flickdockComboPopup')
    popup.setProperty('dropdownTheme','light' if qss_color(fill).lightness()>128 else 'dark')
    view.setProperty('scrollBackdropColor',fill)
    from dropdown_motion import install_item_highlight
    if highlight_insets is None:highlight_insets=(5,2,-5,-2) if margin>1 else (0,1,0,-1)
    install_item_highlight(view,highlight_insets)
    for part in (popup,view,view.viewport()):
        palette=part.palette()
        palette.setColor(QPalette.Window,qss_color(fill))
        palette.setColor(QPalette.Base,qss_color(fill))
        part.setPalette(palette)
        part.setAutoFillBackground(False)
    popup.setStyleSheet(
        'QFrame#flickdockComboPopup {background:transparent;border:0;} '
        'QAbstractItemView{background:transparent;border:0;outline:0;}')
    if popup.layout() is not None:
        popup.layout().setContentsMargins(margin,margin,margin,margin)
    if not hasattr(popup,'_rounded_backdrop'):
        popup._rounded_backdrop=RoundedPopupBackdrop(popup)
    backdrop=popup._rounded_backdrop
    backdrop.fill=fill
    backdrop.stroke=colors['input_border']
    backdrop.setGeometry(popup.rect())
    backdrop.show()
    backdrop.lower()
    backdrop.update()


def show_rounded_combo(combo,show,*,gap=0):
    """Use the shared popup spring in place of the native rectangular roll."""
    animate=QGuiApplication.platformName() in ('windows','cocoa') and QApplication.isEffectEnabled(Qt.UI_AnimateCombo)
    if animate:QApplication.setEffectEnabled(Qt.UI_AnimateCombo,False)
    try:
        show()
        _fit_combo_popup(combo,gap)
    finally:
        if animate:QApplication.setEffectEnabled(Qt.UI_AnimateCombo,True)


def _fit_combo_popup(combo,gap):
    """Fit complete rows and round insets before the first animation frame."""
    view=combo.view();popup=view.window()
    count=min(combo.count(),combo.maxVisibleItems())
    if not count or not popup.isVisible():return
    if popup.layout() is not None:popup.layout().activate()
    view.doItemsLayout()
    rows=sum(max(0,view.sizeHintForRow(index)) for index in range(count))
    viewport_margin=popup.height()-view.viewport().height()
    wanted=max(popup.height(),rows+viewport_margin)
    anchor=combo.mapToGlobal(combo.rect().topLeft())
    screen=QGuiApplication.screenAt(anchor) or screen_for_widget(combo)
    if screen is None:return
    area=screen.availableGeometry()
    top=anchor.y()-gap
    bottom=anchor.y()+combo.height()+gap
    above=max(0,top-area.top())
    below=max(0,area.bottom()+1-bottom)
    prefer_above=popup.geometry().bottom()<top
    if wanted>min(above,below):
        if wanted<=above:prefer_above=True
        elif wanted<=below:prefer_above=False
        else:prefer_above=above>below
    height=min(wanted,above if prefer_above else below)
    y=top-height if prefer_above else bottom
    if height<=0:return
    x=max(area.left(),min(popup.x(),area.right()-popup.width()+1))
    popup.setGeometry(x,y,popup.width(),height)


class RoundedScrollBar(QScrollBar):
    """Use Qt's native scroll behavior with a device-aligned AA thumb."""
    def __init__(self,orientation,parent=None,*,thumb_color=None):
        super().__init__(orientation,parent);self._thumb_color=thumb_color

    def thumb_color(self):
        if self._thumb_color is not None:
            return self._thumb_color() if callable(self._thumb_color) else self._thumb_color
        import ui_theme
        return colors(ui_theme.current_theme())['input_border']

    def paintEvent(self,event):
        from PySide6.QtWidgets import QStyle,QStyleOptionSlider
        option=QStyleOptionSlider();self.initStyleOption(option)
        thumb=self.style().subControlRect(QStyle.CC_ScrollBar,option,QStyle.SC_ScrollBarSlider,self)
        if thumb.isEmpty():return
        painter=QPainter(self);radius=min(RADIUS_SMALL_PX,min(thumb.width(),thumb.height())/2)
        paint_rounded_surface(painter,thumb,radius,qss_color(self.thumb_color()))
        painter.end()


class RoundedProgressBar(QProgressBar):
    """AA track with a native-frame-clock busy state and no idle timer."""
    def __init__(self,parent=None):
        super().__init__(parent)
        from display_frames import FramePulse
        self._frames=FramePulse(self,follow_refresh_rate=True);self._frames.timeout.connect(self.update)
        self.setTextVisible(False)

    def surface_colors(self):
        import ui_theme
        return colors(ui_theme.current_theme())

    def _sync_animation(self):
        if self.isVisible() and self.minimum()==self.maximum()==0 and not reduced_motion():
            if not self._frames.isActive():self._frames.start()
        else:self._frames.stop()
        self.update()

    def setRange(self,minimum,maximum):
        super().setRange(minimum,maximum)
        if hasattr(self,'_frames'):self._sync_animation()

    def showEvent(self,event):super().showEvent(event);self._sync_animation()
    def hideEvent(self,event):self._frames.stop();super().hideEvent(event)

    def paintEvent(self,event):
        c=self.surface_colors();painter=QPainter(self);rect=QRectF(self.rect())
        radius=rect.height()/2.0
        path=QPainterPath();path.addRoundedRect(rect,radius,radius)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillPath(path,qss_color(c['subcard']))
        painter.save();painter.setClipPath(path)
        painter.setPen(QPen(qss_color(c['hatch']),1.2))
        step=7.0;h=rect.height()+2.0;x=rect.left()-h
        while x<rect.right()+h:
            painter.drawLine(QPointF(x,rect.bottom()+1),QPointF(x+h,rect.top()-1))
            x+=step
        minimum,maximum=self.minimum(),self.maximum()
        if minimum==maximum==0:
            phase=.5 if reduced_motion() else (time.monotonic()%1.4)/1.4
            length=rect.width()*.25;left=(rect.width()+length)*phase-length
            chunk=QRectF(left,0,length,rect.height())
        else:
            fraction=max(0.,min(1.,(self.value()-minimum)/max(1,maximum-minimum)))
            chunk=QRectF(0,0,rect.width()*fraction,rect.height())
        if not chunk.isEmpty():
            paint_rounded_surface(painter,chunk,radius,qss_color(c['brand_accent']))
        painter.restore()
        painter.end()


def button_style_option(button):
    """Make hover available to styles used by manually painted buttons."""
    from PySide6.QtWidgets import QStyle, QStyleOptionButton
    option=QStyleOptionButton();button.initStyleOption(option)
    if button.isEnabled() and button.underMouse():option.state |= QStyle.State_MouseOver
    return option


class _KeyboardFocusVisibility(QObject):
    """Expose keyboard focus without adding a blue ring after pointer clicks."""
    def eventFilter(self, watched, event):
        if not isinstance(watched, QAbstractButton):
            return False
        kind = event.type()
        if kind == QEvent.Type.FocusIn:
            visible = event.reason() in (Qt.FocusReason.TabFocusReason,
                Qt.FocusReason.BacktabFocusReason, Qt.FocusReason.ShortcutFocusReason)
        elif kind == QEvent.Type.FocusOut:
            visible = False
        elif kind == QEvent.Type.MouseButtonPress:
            visible = False
        elif kind == QEvent.Type.KeyPress:
            visible = True
        else:
            return False
        if bool(watched.property('keyboardFocus')) != visible:
            watched.setProperty('keyboardFocus', visible)
            watched.style().unpolish(watched)
            watched.style().polish(watched)
            watched.update()
        return False


def reference_font():
    """Return the application font chosen for this platform."""
    return QFont(QApplication.font()) if QApplication.instance() else QFont()


def _ui_weight(weight):
    value=int(weight)
    return 600 if value>=550 else 500 if value>=450 else 400


def set_ui_weight(font,weight):
    """Request a regular, medium or semibold UI weight."""
    value=_ui_weight(weight)
    font.setWeight(QFont.Weight(value));font.setVariableAxis(FONT_WEIGHT_AXIS,float(value))
    return font


class _FontWeightNormalizer(QObject):
    def __init__(self,parent):
        super().__init__(parent);self._updating=False

    def eventFilter(self,watched,event):
        if self._updating or event.type() not in (QEvent.FontChange,QEvent.Polish,QEvent.StyleChange) or not isinstance(watched,QWidget):
            return False
        font=watched.font();special=watched.property('flickdockFontWeight')
        value=int(special) if special is not None else _ui_weight(font.weight())
        if int(font.weight())!=value or font.variableAxisValue(FONT_WEIGHT_AXIS)!=value:
            self._updating=True
            try:
                if special is None:set_ui_weight(font,value)
                else:font.setWeight(QFont.Weight(value));font.setVariableAxis(FONT_WEIGHT_AXIS,float(value))
                watched.setFont(font)
            finally:self._updating=False
        return False


def apply_widget_theme_style(widget,style):
    """Restyle a window once, then normalize each resulting font once.

    Qt emits many nested style/font events while repolishing a large dialog.
    Rechecking every intermediate font and every frameless-window mouse filter
    repeatedly makes theme changes block the UI. The synchronous transaction
    receives no pointer events; normal input handling resumes immediately.
    """
    app=QApplication.instance();normalizer=getattr(app,'_flickdock_font_weights',None) if app else None
    chrome=getattr(widget,'chrome',None);prior=normalizer._updating if normalizer is not None else False
    if normalizer is not None:normalizer._updating=True
    if app is not None and chrome is not None:app.removeEventFilter(chrome)
    try:widget.setStyleSheet(style)
    finally:
        if app is not None and chrome is not None:app.installEventFilter(chrome)
        if normalizer is not None:normalizer._updating=prior
    if normalizer is not None and not prior:
        event=QEvent(QEvent.FontChange)
        for child in [widget,*widget.findChildren(QWidget)]:normalizer.eventFilter(child,event)


_FONT_STACK_CACHE={}


def font_css_stack(*,technical=False):
    """Keep the script fallback when a stylesheet specifies a font (cached)."""
    app=QApplication.instance()
    raw=tuple(QApplication.font().families()) if app else ()
    key=(bool(technical),raw)
    cached=_FONT_STACK_CACHE.get(key)
    if cached is not None:return cached
    families=([TECHNICAL_FONT_FAMILY]+list(raw)) if technical else list(raw)
    families=list(dict.fromkeys(families))
    value=', '.join("'"+name.replace("'",'')+"'" for name in families) or "'sans-serif'"
    _FONT_STACK_CACHE[key]=value
    return value


def technical_font(base=None):
    """Use monospaced Latin metadata and the platform's Chinese fallback."""
    font=QFont(base) if base is not None else reference_font()
    families=QApplication.font().families() if QApplication.instance() else []
    font.setFamilies(list(dict.fromkeys([TECHNICAL_FONT_FAMILY]+families)))
    font.setFeature(FONT_TABULAR_NUMBERS,1)
    return font


def configure_reference_font(app):
    """Use platform UI fonts while retaining the bundled technical face."""
    # Our fully styled controls use the same layout metrics on both backends.
    # Native file dialogs still come from the platform dialog helper.
    # Replacing the application style while existing proxy styles and windows
    # still reference it can invalidate native Qt objects. Install it once.
    if not getattr(app,'_flickdock_fusion_installed',False):
        app.setStyle('Fusion');app._flickdock_fusion_installed=True
    from border_reveal import install_border_reveal
    install_border_reveal(app)
    root=Path(getattr(sys,'_MEIPASS',Path(__file__).parent))
    if not getattr(app,'_flickdock_fonts_loaded',False):
        for name in BUNDLED_FONT_FILES:
            path=root/'assets'/'fonts'/name
            if not path.is_file() or QFontDatabase.addApplicationFont(str(path))<0:
                raise RuntimeError('Unable to load required bundled font: '+str(path))
        app._flickdock_fonts_loaded=True
    translation=root/'assets/ui/qtbase_zh_CN.qm'
    if not getattr(app,'_flickdock_translation',None) and translation.is_file():
        translator=QTranslator(app)
        if translator.load(str(translation)):
            app.installTranslator(translator);app._flickdock_translation=translator
    # The Windows offscreen Qt backend has no native font enumerator. Register
    # the installed OS fonts only when necessary; never copy them to the app.
    if sys.platform=='win32' and 'Microsoft YaHei' not in QFontDatabase.families():
        font_dir=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'Fonts'
        for name in ('msyh.ttc','msyhbd.ttc'):
            path=font_dir/name
            if path.is_file():QFontDatabase.addApplicationFont(str(path))
    font=QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    families=font.families()
    if sys.platform=='win32':
        families=['Microsoft YaHei','Microsoft YaHei UI',*families]
    elif sys.platform=='darwin':
        families=[*families,'PingFang SC']
    font.setFamilies(list(dict.fromkeys(families)));set_ui_weight(font,400)
    font.setStyleHint(QFont.StyleHint.SansSerif)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias|QFont.StyleStrategy.PreferQuality)
    font.setHintingPreference(QFont.HintingPreference.PreferVerticalHinting)
    font.setFeature(FONT_TABULAR_NUMBERS,1)
    font.setPointSize(10 if sys.platform=='win32' else 11);app.setFont(font)
    if not getattr(app,'_flickdock_font_weights',None):
        app._flickdock_font_weights=_FontWeightNormalizer(app)
        app.installEventFilter(app._flickdock_font_weights)
    from settings_navigation import install_smooth_scrolling
    install_smooth_scrolling(app)
    if not getattr(app, '_flickdock_focus_visibility', None):
        app._flickdock_focus_visibility = _KeyboardFocusVisibility(app)
        app.installEventFilter(app._flickdock_focus_visibility)
    return font


_DARK = {
    'background': '#121317', 'surface': '#121317',
    'dialog_surface': '#121317', 'window_backdrop': '#0E0F11', 'popup_surface': '#1D1F23',
    'sidebar': '#050506', 'rail': '#050506', 'preview': '#0E0F11',
    'rail_item': '#1B1C20', 'rail_item_hover': '#272930', 'rail_item_ink': '#A1A5B2',
    'rail_active': '#FAFAFC', 'rail_active_ink': '#0A0A0B',
    'brand_lime': '#A8EF54', 'brand_lime_ink': '#0A0A0B', 'hatch': '#363942',
    'glass_border': 'rgba(255,255,255,22)', 'divider': 'rgba(255,255,255,18)',
    'highlight': 'rgba(255,255,255,30)', 'card': '#17181B', 'subcard': '#1F2126',
    'segment_selected': '#F5F6F8', 'segment_selected_ink': '#0E0F11', 'segment_border': '#F5F6F8',
    'file_card': '#1F2126', 'file_card_hover': '#262930', 'file_card_selected': '#2B2E35',
    'file_card_selected_focus': '#323640',
    'grid_selected': '#2B2E35', 'grid_selected_text': '#F5F6F8',
    'grid_selected_secondary': '#A1A5B2', 'grid_selected_border': 'rgba(255,255,255,56)',
    'grid_unchecked_border': '#4A4E59', 'grid_unchecked_hover_border': '#8A8F9E',
    'stack_cards': ('#17181B','#1F2126','#24262B','#2C2F36','#353942'),
    'border': 'rgba(255,255,255,18)', 'card_hover': '#262930',
    'hover_border': 'rgba(255,255,255,34)', 'selected': '#2B2E35',
    'selected_border': 'rgba(255,255,255,56)', 'control': '#24262B',
    'hover': '#2A2D34', 'input': '#1E2024', 'transfer_progress_fill': 'rgba(168,239,84,88)', 'transfer_status_text': '#C8CCD6',
    'input_border': 'rgba(255,255,255,33)', 'text': '#F5F6F8',
    'secondary': '#A1A5B2', 'muted': '#969BA8', 'file_description': '#8D92A0', 'icon': '#E2E4EA',
    'shelf_pin': '#E9AF20', 'sort_arrow_idle': '#7C808E', 'sort_arrow_active': '#0E0F11',
    'qt_border': '#2A2D34', 'media_track': '#363942',
    'shadow': 'rgba(0,0,0,191)', 'shadow_blur': 64, 'shadow_y': 28,
}
_LIGHT = {
    'background': '#EAEBEF', 'surface': '#EAEBEF',
    'dialog_surface': '#E3E4E8', 'window_backdrop': '#E3E4E8', 'popup_surface': '#FFFFFF',
    'sidebar': '#0A0A0B', 'rail': '#0A0A0B', 'preview': '#E3E4E8',
    'rail_item': '#26262A', 'rail_item_hover': '#35353B', 'rail_item_ink': '#D4D4D8',
    'rail_active': '#FFFFFF', 'rail_active_ink': '#0A0A0B',
    'brand_lime': '#A8EF54', 'brand_lime_ink': '#0A0A0B', 'hatch': '#C8CBD3',
    'glass_border': 'rgba(255,255,255,244)', 'divider': '#DCDFE6',
    'highlight': '#FFFFFF', 'card': '#FAFAFC', 'subcard': '#F2F3F6',
    'segment_selected': '#111114', 'segment_selected_ink': '#FAFAFC', 'segment_border': '#111114',
    'file_card': '#F2F3F6', 'file_card_hover': '#ECEEF2', 'file_card_selected': '#FFFFFF',
    'file_card_selected_focus': '#FFFFFF',
    'grid_selected': '#FFFFFF', 'grid_selected_text': '#111114',
    'grid_selected_secondary': '#5C5F6A', 'grid_selected_border': '#CBD0DA',
    'grid_unchecked_border': '#C4C7D0', 'grid_unchecked_hover_border': '#868A96',
    'stack_cards': ('#FAFAFC','#F2F3F6','#ECEEF2','#E3E5EB','#D7DAE2'),
    'border': '#E6E7EC', 'card_hover': '#ECEEF2',
    'hover_border': '#CFD2DC', 'selected': '#E8EAEF',
    'selected_border': '#CBD0DA', 'control': '#ECEEF2',
    'hover': '#ECEEF2', 'input': '#FAFAFC', 'transfer_progress_fill': 'rgba(168,239,84,118)', 'transfer_status_text': '#4D505A',
    'input_border': '#C8CCD6', 'text': '#111114',
    'secondary': '#5C5F6A', 'muted': '#636773', 'file_description': '#636773', 'icon': '#2E3038',
    'shelf_pin': '#956200', 'shelf_pin_fill': '#FFAE33', 'sort_arrow_idle': '#8C909C', 'sort_arrow_active': '#FAFAFC',
    'qt_border': '#DCDFE6', 'media_track': '#D6D8E0',
    'shadow': 'rgba(15,23,42,41)', 'shadow_blur': 56, 'shadow_y': 24,
}
_SEMANTIC = {
    'blue': '#0A84FF', 'green': '#30D158', 'orange': '#FF9F0A',
    'red': '#FF453A', 'purple': '#BF5AF2', 'cyan': '#64D2FF', 'steel': '#567DAF',
}


_COLORS_CACHE={}


def colors(theme='dark', platform=None):
    from theme_accent import current as _accent_now
    light = str(theme).lower() == 'light'
    cache_key = (light, _accent_now())
    cached = _COLORS_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)
    result = dict(_LIGHT if light else _DARK)
    result.update(_SEMANTIC)
    light = str(theme).lower() == 'light'
    result.update(action='#111114' if light else '#F5F6F8',
                  action_hover='#27282E' if light else '#E2E4EA',
                  action_pressed='#3A3C44' if light else '#CFD2DC',
                  control_hover='#E3E5EB' if light else '#2E3138',
                  control_pressed='#D7DAE2' if light else '#383C44',
                  on_action='#FAFAFC' if light else '#0E0F11',
                  danger_action='#DC2626' if light else '#F87171',
                  danger_on_action='#FFFFFF' if light else '#111114',
                  link='#27282E' if light else '#D4D7E0')
    if str(theme).lower() == 'light':
        result.update(green='#167D3D', red='#BD2C25', orange='#9D5800')
    is_mac = str(platform or ('mac' if sys.platform == 'darwin' else 'win')).lower() in ('mac', 'macos', 'darwin')
    result.update(radius=RADIUS_WINDOW_PX, card_radius=RADIUS_CARD_PX,
                  radius_small=RADIUS_SMALL_PX, radius_subcontrol=RADIUS_SUBCONTROL_PX, radius_control=RADIUS_CONTROL_PX,
                  radius_card=RADIUS_CARD_PX, radius_panel=RADIUS_PANEL_PX)
    from theme_accent import action_tokens
    result.update(action_tokens('light' if light else 'dark'))
    result['brand_accent']=result['action']
    result['brand_accent_ink']=result['on_action']
    _ac=QColor(result['action'])
    result['transfer_progress_fill']=f'rgba({_ac.red()},{_ac.green()},{_ac.blue()},{42 if light else 54})'
    result.update(primary=result['action'], accent=result['action'], danger=result['red'], success=result['green'])
    _COLORS_CACHE[cache_key] = dict(result)
    return result


# Paths copied verbatim from the source document's toolbar and category SVGs.
_SVG_BODY = {
    'pipette': '<path d="m3 21 3.5-.9L18.6 8l-2.6-2.6L3.9 17.5 3 21Zm11.5-16.5 5 5M18 2.5l3.5 3.5-2 2-3.5-3.5Z"/>',
    'pipette-filled': '<path fill="currentColor" stroke="none" d="m13 8.35 2.65 2.65-9.1 9.1L3 21l.9-3.55 9.1-9.1Z M14.25 6.9l3.86-3.86a1.5 1.5 0 0 1 2.12 0l1.13 1.13a1.5 1.5 0 0 1 0 2.12l-3.86 3.86-3.25-3.25Z"/>',
    'return': '<path d="M20 5v7a4 4 0 0 1-4 4H4m5-5-5 5 5 5"/>',
    'translate': '<path d="M2 5h12M8 2v3M5 5c1 6 4 9 8 11M12 5c-1 6-4 9-8 11M14 21l4-10 4 10M16 17h4"/>',
    'shield': '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/>',
    'tag': '<path d="M3 3h8l10 10-8 8L3 11V3Z"/><circle cx="7" cy="7" r="1"/>',
    'speaker': '<path d="M3 9h4l5-4v14l-5-4H3V9ZM16 8a6 6 0 0 1 0 8M19 5a10 10 0 0 1 0 14"/>',
    'star': '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9L12 3Z"/>',
    'star-filled': '<path fill="currentColor" stroke="none" d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9L12 3Z"/>',
    'star-shelf-light': '<path fill="currentColor" stroke="#956200" stroke-width="1.35" d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9L12 3Z"/>',
    'swap': '<path d="M3 7h18m-4-4 4 4-4 4M21 17H3m4-4-4 4 4 4"/>',
    'undo': '<path d="m8 5-5 5 5 5M3 10h11a6 6 0 0 1 0 12"/>',
    'compare': '<path d="M12 3v18M8 6H3v12h5M16 6h5v12h-5"/>',
    'windows': '<path fill="currentColor" stroke="none" d="M2 2h9.2v9.2H2zM12.8 2H22v9.2h-9.2zM2 12.8h9.2V22H2zM12.8 12.8H22V22h-9.2z"/>',
    # Simple Icons CC0 apple.svg, identifying a discovered Mac.
    'apple': '<path fill="currentColor" stroke="none" d="M12.152 6.896c-.948 0-2.415-1.078-3.96-1.04-2.04.027-3.91 1.183-4.961 3.014-2.117 3.675-.546 9.103 1.519 12.09 1.013 1.454 2.208 3.09 3.792 3.039 1.52-.065 2.09-.987 3.935-.987 1.831 0 2.35.987 3.96.948 1.637-.026 2.676-1.48 3.676-2.948 1.156-1.688 1.636-3.325 1.662-3.415-.039-.013-3.182-1.221-3.22-4.857-.026-3.04 2.48-4.494 2.597-4.559-1.429-2.09-3.623-2.324-4.39-2.376-2-.156-3.675 1.09-4.61 1.09zM15.53 3.83c.843-1.012 1.4-2.427 1.245-3.83-1.207.052-2.662.805-3.532 1.818-.78.896-1.454 2.338-1.273 3.714 1.338.104 2.715-.688 3.559-1.701"/>',
    'cloud': '<path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z"/>',
    'cloud-filled': '<path fill="currentColor" stroke="none" d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z"/>',
    'computer': '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4"/>',
    'link': '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
    'computer-filled': '<path fill="currentColor" stroke="none" d="M5 3a2 2 0 0 0-2 2v11a2 2 0 0 0 2 2h5v2H7v2h10v-2h-3v-2h5a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2Z"/>',
    'cloud-upload': '<path d="M7 18H6a5 5 0 0 1-1-9.9A7 7 0 0 1 18 6a5 5 0 0 1 0 10h-1M12 21V11m-3 3 3-3 3 3"/>',
    'cloud-remove': '<path d="M7 18H6a5 5 0 0 1-1-9.9A7 7 0 0 1 18 6a5 5 0 0 1 0 10h-1M9 15l6 6m0-6-6 6"/>',
    'pin': '<path d="M16 3l5 5-4 1-4 4 1 5-8-8 5 1 4-4 1-4z"/><path d="M9 15l-6 6"/>',
    'expand': '<path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/>',
    'collapse': '<path d="M4 14h6v6M20 10h-6V4M14 10l7-7M3 21l7-7"/>',
    'more': '<path d="M5 12h.01M12 12h.01M19 12h.01"/>',
    'dock': '<path d="M13 5l7 7-7 7M5 5l7 7-7 7"/>',
    'nearby': '<path d="M18 8V6a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h8M10 15v4M7 19h5"/><rect width="6" height="10" x="16" y="12" rx="2"/>',
    'settings': '<circle cx="12" cy="12" r="3"/><path stroke-linecap="round" d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 01-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06a1.65 1.65 0 001.82.33H9a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06a1.65 1.65 0 00-.33 1.82V9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"/>',
    'search': '<path d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/>',
    'list': '<path d="M4 6h16M4 12h16M4 18h16"/>',
    'grid': '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    'chevron': '<path d="M6 9l6 6 6-6"/>',
    'right': '<path d="M9 5l7 7-7 7"/>',
    'all': '<path d="M4 6h16M4 12h16M4 18h16"/>',
    'image': '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/>',
    'video': '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="M10 9l5 3-5 3V9z"/>',
    'audio': '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
    'text': '<path d="M4 7V4h16v3M9 20h6M12 4v16"/>',
    'file': '<path d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/>',
    'web': '<circle cx="12" cy="12" r="9"/><path d="M3.6 9h16.8M3.6 15h16.8M12 3a15 15 0 010 18M12 3a15 15 0 000 18"/>',
    'transferring': '<path d="M7 16V4m0 0L3 8m4-4l4 4m6 0v12m0 0l4-4m-4 4l-4-4"/>',
    'refresh': '<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/>',
    'play': '<path d="M4 3l9 5-9 5V3z"/>',
    'check': '<path d="m5 12 4 4L19 6"/>',
    'circle-check': '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
    'circle-alert': '<circle cx="12" cy="12" r="9"/><path d="M12 7v7M12 16.5h.01"/>',
    'close': '<path d="m6 6 12 12M18 6 6 18"/>',
    'trash': '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>',
    'package': '<path d="m12 3 9 5-9 5-9-5 9-5ZM3 8v10l9 5 9-5V8M12 13v10M7.5 5.5l9 5"/>',
    'open-external': '<path d="M13 5h6v6M19 5l-9 9"/><path d="M18 14v5H5V6h5"/>',
    'rename': '<path d="m4 20 4-1 11-11a2.1 2.1 0 0 0-3-3L5 16z"/><path d="m14.5 6.5 3 3"/>',
    'package-selected': '<path d="m10 2 8 4.5-8 4.5-8-4.5L10 2ZM2 6.5v9L10 20M10 11v9M6 4.2l8 4.5"/><rect x="13" y="13" width="10" height="10" rx="2"/><path d="m15.5 18 1.8 1.8 3.7-4"/>',
    'copy': '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h3"/>',
    'user': '<circle cx="12" cy="8" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/>',
    'eye': '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
    'eye-off': '<path d="m3 3 18 18M10.6 5.1A12 12 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-3.2 4.1M6.1 6.1A20 20 0 0 0 2 12s3.5 7 10 7a12 12 0 0 0 5.9-1.6M10 10a3 3 0 0 0 4 4"/>',
    'pause': '<path d="M8 4v16M16 4v16"/>',
    'status-upload': '<path d="M12 20V4M6 10l6-6 6 6"/>',
    'status-clock': '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l4 2"/>',
    'desktop': '<rect x="3" y="3" width="18" height="13" rx="2"/><path d="M12 16v5M8 21h8"/>',
    'laptop': '<rect x="5" y="3" width="14" height="13" rx="2"/><path d="m5 16-3 5h20l-3-5M9 18h6"/>',
    'sun': '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5"/>',
    'moon': '<path d="M20.5 13A9 9 0 0 1 11 3.5 9 9 0 1 0 20.5 13Z"/>',
    'plus': '<path d="M12 4v16M4 12h16"/>',
    'shelf': '<path d="m3 13 3-9h12l3 9v7H3v-7Z"/><path d="M3 13h5l2 3h4l2-3h5"/>',
    'send': '<path d="m22 2-7 20-4-9-9-4L22 2ZM22 2 11 13"/>',
    'download': '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>',
}
_ALIASES = {'folder': 'file', 'upload': 'transferring',
            'wifi': 'nearby', 'lan': 'nearby', 'chevron-down': 'chevron',
            'chevron-right': 'right', 'cloud-off': 'cloud'}


_svg_renderers=threading.local()
_svg_pixmaps=threading.local()
_source_icons=threading.local()


def cached_svg_renderer(svg):
    # SVG renderers stay on the painting thread. One bounded cache is reused
    # across identical glyphs rather than reparsing XML for every paint frame.
    cache=getattr(_svg_renderers,'cache',None)
    if cache is None:cache=_svg_renderers.cache=OrderedDict()
    renderer=cache.pop(svg,None)
    if renderer is None:renderer=QSvgRenderer(QByteArray(svg.encode('utf8')))
    cache[svg]=renderer
    while len(cache)>128:cache.popitem(last=False)
    return renderer


def _cached_svg_pixmap(svg, width, height, ratio):
    """Rasterize at the actual display scale; reuse only identical glyphs."""
    cache=getattr(_svg_pixmaps,'cache',None)
    if cache is None:cache=_svg_pixmaps.cache=OrderedDict()
    key=(svg,width,height,ratio)
    result=cache.pop(key,None)
    if result is None:
        result=QPixmap(max(1,round(width*ratio)),max(1,round(height*ratio)))
        result.setDevicePixelRatio(ratio);result.fill(Qt.GlobalColor.transparent)
        painter=QPainter(result);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cached_svg_renderer(svg).render(painter,QRectF(0,0,width,height));painter.end()
    cache[key]=result
    while len(cache)>256:cache.popitem(last=False)
    return result


class _SourceIconEngine(QIconEngine):
    def __init__(self, name, color):
        super().__init__()
        self.name, self.color = name, QColor(color)
        self.svg = None
        key = _ALIASES.get(name, name)
        if key in _SVG_BODY:
            body = _SVG_BODY[key]
            if key == 'play':
                attributes = 'viewBox="0 0 16 16" fill="currentColor" stroke="none"'
            else:
                attributes = ('viewBox="0 0 24 24" fill="' + ('currentColor' if key == 'pin' else 'none')
                              + '" stroke="currentColor" stroke-width="' + ('2.2' if key == 'more' else '1.8')
                              + '" stroke-linecap="round" stroke-linejoin="round"')
            if name == 'left':
                body = '<g transform="translate(24 0) scale(-1 1)">' + body + '</g>'
            data = '<svg xmlns="http://www.w3.org/2000/svg" ' + attributes + '>' + body + '</svg>'
            self.svg = data.replace('currentColor', self.color.name())

    def clone(self):
        return _SourceIconEngine(self.name, self.color)

    def paint(self, painter, rect, mode, state):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if mode == QIcon.Mode.Disabled:
            painter.setOpacity(0.4)
        if self.svg:
            transform=painter.worldTransform()
            # A transformed icon may be scaling/rotating during feedback;
            # keep that case vector, so motion never enlarges a cached bitmap.
            unscaled=(abs(transform.m11()-1)<.0001 and abs(transform.m22()-1)<.0001
                      and abs(transform.m12())<.0001 and abs(transform.m21())<.0001)
            device=painter.device()
            ratio=float(device.devicePixelRatioF()) if device is not None else 1.
            if unscaled and rect.width()>0 and rect.height()>0 and ratio>0:
                pixmap=_cached_svg_pixmap(self.svg,rect.width(),rect.height(),ratio)
                painter.drawPixmap(rect.topLeft(),pixmap)
            else:
                cached_svg_renderer(self.svg).render(painter, QRectF(rect))
        painter.restore()

    def pixmap(self, size, mode, state):
        result = QPixmap(size)
        result.fill(Qt.GlobalColor.transparent)
        painter = QPainter(result)
        self.paint(painter, result.rect(), mode, state)
        painter.end()
        return result


def icon(name, color='#A1A1AA', size=16):
    # size is accepted for convenient call-site parity; this vector engine is
    # rendered at the actual button's iconSize and device pixel ratio.
    if name == 'left':
        _ALIASES['left'] = 'right'
    cache=getattr(_source_icons,'cache',None)
    if cache is None:cache=_source_icons.cache=OrderedDict()
    key=(str(name),QColor(color).rgba())
    value=cache.pop(key,None)
    if value is None:value=QIcon(_SourceIconEngine(str(name), color))
    cache[key]=value
    while len(cache)>256:cache.popitem(last=False)
    return QIcon(value)


def _context(widget):
    current = widget
    while current is not None:
        theme = current.property('uiTheme')
        if theme:
            return str(theme), str(current.property('uiPlatform') or 'win')
        current = current.parentWidget()
    return 'dark', 'mac' if sys.platform == 'darwin' else 'win'


class UiLabel(QLabel):
    def __init__(self, text='', size=12, weight=400, color='primary', parent=None):
        super().__init__(text, parent)
        self._ui_size, self._ui_weight, self._ui_color = size, weight, color
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.refresh_style()

    def refresh_style(self):
        palette = colors(*_context(self))
        value = palette.get(self._ui_color, self._ui_color)
        font = self.font()
        font.setPixelSize(readable_size(self._ui_size))
        set_ui_weight(font,self._ui_weight)
        self.setFont(font)
        self.setStyleSheet(f'color:{value}; background:transparent; border:none;')


class UiButton(QPushButton):
    def __init__(self, text='', icon='', variant='secondary', parent=None):
        super().__init__(text, parent)
        self.variant = str(variant)
        self._ui_icon_name = icon if isinstance(icon, str) else ''
        self.setProperty('uiVariant', self.variant)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setIconSize(QSize(16, 16))
        self.setFixedHeight(32 if variant == 'toolbar' else 28)
        if variant == 'toolbar':
            self.setFixedWidth(32)
        if isinstance(icon, QIcon):
            self.setIcon(icon)
        self.refresh_style()

    def refresh_style(self):
        c = colors(*_context(self))
        primary = self.variant in ('primary', 'accent')
        danger = self.variant in ('danger', 'destructive')
        plain = self.variant in ('toolbar', 'ghost', 'text', 'link')
        foreground = c['on_action'] if primary else c['red'] if danger else c['text']
        background = c['action'] if primary else 'rgba(255,69,58,38)' if danger else 'transparent' if plain else c['control']
        border = 'rgba(255,69,58,77)' if danger else 'transparent' if plain or primary else c['glass_border']
        hover = c['action_hover'] if primary else 'rgba(255,69,58,64)' if danger else c['hover'] if plain else c['control_hover']
        padding = '0px' if self.variant == 'toolbar' else '0px 12px'
        checked = '' if primary else f"QPushButton:checked {{ background:{c['selected']}; color:{c['link']}; }}"
        self.setStyleSheet(readable_qss(f'''QPushButton {{ background:{background}; color:{foreground};
            border:1px solid {border}; border-radius:8px; padding:{padding};
            font-size:12px; font-weight:500; }}
            QPushButton:hover {{ background:{hover}; }}
            QPushButton:pressed {{ background:{c['selected'] if plain else c['action_pressed'] if primary else c['control_pressed']}; }}
            {checked}
            QPushButton:disabled {{ color:{c['muted']}; background:{c['control']}; border-color:transparent; }}
            QPushButton[keyboardFocus="true"] {{border:1px solid {c['link']};}}'''))
        if self._ui_icon_name:
            self.setIcon(icon(self._ui_icon_name, foreground))


class UiSwitch(QAbstractButton):
    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(bool(checked))
        self.setFixedSize(44, 32)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._position = 1.0 if checked else 0.0
        from display_frames import DisplayAnimation
        self._motion = DisplayAnimation(self)
        self._motion.setDuration(240)
        curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
        curve.addCubicBezierSegment(QPointF(0.22, 1), QPointF(0.36, 1), QPointF(1, 1))
        self._motion.setEasingCurve(curve)
        self._motion.valueChanged.connect(self._animate)
        self.toggled.connect(self._toggle)

    def _toggle(self, checked):
        self._motion.stop()
        if reduced_motion():
            self._animate(1.0 if checked else 0.0)
            return
        self._motion.setStartValue(self._position)
        self._motion.setEndValue(1.0 if checked else 0.0)
        self._motion.start()

    def _animate(self, value):
        self._position = float(value)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        c = colors(*_context(self))
        start, end = QColor('#404040' if _context(self)[0]=='dark' else '#D4D4D4'), QColor(c['action'])
        p = self._position
        color = QColor(*(round(a + (b-a)*p) for a,b in zip(start.getRgb(), end.getRgb())))
        if self.underMouse() and self.isEnabled():color=color.lighter(110 if _context(self)[0]=='dark' else 94)
        if not self.isEnabled():
            painter.setOpacity(0.4)
        painter.setBrush(color)
        painter.translate(3,5)
        painter.drawRoundedRect(QRectF(0,0,38,22),11,11)
        painter.setBrush(QColor(0,0,0,35))
        painter.drawEllipse(QRectF(2+16*p,3,18,18))
        off_thumb, on_thumb = QColor('#FAFAFA'), QColor(c['on_action'])
        painter.setBrush(QColor(*(round(a+(b-a)*p) for a,b in zip(off_thumb.getRgb(),on_thumb.getRgb()))))
        painter.drawEllipse(QRectF(2+16*p,2,18,18))
        if keyboard_focus_visible(self):
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(c['ring']), 1.5))
            painter.drawRoundedRect(QRectF(-2,-2,42,26),13,13)


class _Titlebar(QWidget):
    def __init__(self, surface, title):
        super().__init__(surface)
        self.surface = surface
        self.setFixedHeight(48)
        self._drag = None
        self.layout_box = QHBoxLayout(self)
        self.layout_box.setContentsMargins(0,0,0,0)
        self.layout_box.setSpacing(12)
        _, platform = _context(surface)
        self.traffic_buttons = []
        if platform in ('mac','macos','darwin'):
            traffic = QWidget(self)
            row = QHBoxLayout(traffic)
            row.setContentsMargins(0,0,0,0)
            row.setSpacing(8)
            close_action = (lambda: surface.navigate('overview')) if title in ('设置','局域网互传') else (lambda: surface.controller.command('dock'))
            callbacks = (close_action, lambda: surface.navigate('mini'),
                         lambda: surface.controller.command('pin'))
            for value, callback in zip(('#FF5F56','#FFBD2E','#27C93F'), callbacks):
                control = QPushButton(traffic)
                control.setFixedSize(12,12)
                control.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                control.setCursor(Qt.CursorShape.PointingHandCursor)
                control.setStyleSheet(f'QPushButton{{background:{value};border:0;border-radius:6px;}} QPushButton:hover{{border:1px solid rgba(255,255,255,64);}}')
                control.clicked.connect(callback)
                row.addWidget(control)
                self.traffic_buttons.append(control)
            if title in ('设置','局域网互传'):
                self.traffic_buttons[-1].setEnabled(False)
            self.layout_box.addWidget(traffic)
        self.title_label = UiLabel(title,15,600,parent=self)
        self.layout_box.addWidget(self.title_label)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            top = self.surface.window()
            handle = top.windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return
            self._drag = event.globalPosition().toPoint() - top.pos()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.surface.window().move(event.globalPosition().toPoint()-self._drag)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag = None
        super().mouseReleaseEvent(event)


def titlebar(surface, title):
    """Return the source header's left section; page owns its right actions."""
    return _Titlebar(surface, title)


def style_color_dialog(dialog,theme):
    """Own the complete chooser surface; ancestor settings QSS cannot leak in."""
    from PySide6.QtGui import QPalette
    c=colors(theme);dialog.setObjectName('flickdockColorPicker')
    palette=QPalette(dialog.palette())
    for role,key in (('Window','popup_surface'),('Base','input'),('Button','control'),
                     ('WindowText','text'),('Text','text'),('ButtonText','text'),
                     ('Highlight','action'),('HighlightedText','on_action')):
        palette.setColor(getattr(QPalette,role),QColor(c[key]))
    dialog.setPalette(palette)
    dialog.setFont(reference_font())
    dialog.setStyleSheet(f'''
        QColorDialog#flickdockColorPicker {{background:{c['popup_surface']};color:{c['text']};}}
        QColorDialog#flickdockColorPicker QLabel {{background:transparent;color:{c['text']};border:0;}}
        QColorDialog#flickdockColorPicker QLineEdit, QColorDialog#flickdockColorPicker QSpinBox {{
            background:{c['input']};color:{c['text']};border:1px solid {c['input_border']};
            border-radius:6px;padding:4px 6px;selection-background-color:{c['action']};selection-color:{c['on_action']};}}
        QColorDialog#flickdockColorPicker QPushButton {{background:{c['control']};color:{c['text']};
            border:1px solid {c['input_border']};border-radius:6px;padding:5px 10px;font-weight:400;}}
        QColorDialog#flickdockColorPicker QPushButton:hover {{background:{c['control_hover']};}}
        QColorDialog#flickdockColorPicker QPushButton:pressed {{background:{c['control_pressed']};}}
        QColorDialog#flickdockColorPicker QPushButton:default {{background:{c['action']};color:{c['on_action']};border-color:{c['action']};}}
        QColorDialog#flickdockColorPicker QPushButton:default:hover {{background:{c['action_hover']};}}
        QColorDialog#flickdockColorPicker QPushButton:default:pressed {{background:{c['action_pressed']};}}
    ''')
    if sys.platform=='win32' and QGuiApplication.platformName().lower() not in ('offscreen','minimal'):
        import ctypes
        value=ctypes.c_int(theme=='dark')
        dwm=ctypes.WinDLL('dwmapi').DwmSetWindowAttribute
        dwm.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_void_p,ctypes.c_uint]
        dwm.restype=ctypes.c_long
        window=int(dialog.winId())
        if dwm(window,20,ctypes.byref(value),ctypes.sizeof(value))<0:
            dwm(window,19,ctypes.byref(value),ctypes.sizeof(value))


def apply_theme(widget, theme='dark', platform=None):
    platform = platform or ('mac' if sys.platform == 'darwin' else 'win')
    c = colors(theme, platform)
    widget.setProperty('uiTheme', str(theme))
    widget.setProperty('uiPlatform', str(platform))
    font = reference_font()
    font.setPixelSize(12)
    widget.setFont(font)
    widget.setStyleSheet(f'''
        QWidget {{ color:{c['text']}; font-size:12px; }}
        QLabel {{ background:transparent; border:0; }}
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ background:{c['input']};
            color:{c['text']}; border:1px solid {c['input_border']}; border-radius:8px;
            padding:5px 10px; min-height:18px; selection-background-color:{c['action']}; selection-color:{c['on_action']}; }}
        QLineEdit:focus, QComboBox:focus {{ border-color:{c['link']}; }}
        QComboBox QAbstractItemView {{ background:{c['input']}; color:{c['text']};
            selection-background-color:{c['action']}; selection-color:{c['on_action']}; padding:4px; border:1px solid {c['input_border']}; }}
        QComboBox QAbstractItemView::item {{ min-height:30px; padding:4px 10px; }}
        QScrollArea {{ background:transparent; border:0; }}
        QScrollBar:vertical {{ background:transparent; width:6px; margin:0; }}
        QScrollBar::handle:vertical {{ background:rgba(142,142,147,71); border-radius:3px; min-height:24px; }}
        QScrollBar::handle:vertical:hover {{ background:rgba(142,142,147,115); }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:transparent; }}
        QMenu {{ background:{c['input']}; color:{c['text']}; border:1px solid {c['glass_border']};
            border-radius:8px; padding:4px 0; }}
        QMenu::item {{ padding:6px 14px; }}
        QMenu::item:selected {{ background:{c['action']}; color:{c['on_action']}; }}
    ''')
    for child in [widget] + widget.findChildren(QWidget):
        if isinstance(child, (UiButton, UiLabel)):
            child.refresh_style()
