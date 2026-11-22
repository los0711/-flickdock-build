"""One scroll document with accessible anchor navigation and scroll tracking."""
from collections import Counter
import sys
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSignalBlocker, QTimer, QEasingCurve, Qt
from PySide6.QtGui import QColor,QImage,QLinearGradient,QPainter,QRegion
from PySide6.QtWidgets import QApplication,QAbstractItemView,QAbstractScrollArea,QListWidget,QStyledItemDelegate,QStyle,QWidget
from display_frames import DisplayAnimation
from ui_transitions import motion_allowed
from native_ui_common import paint_rounded_surface,RADIUS_CONTROL_PX

ANCHOR_TOP_INSET_PX = 20
TRACKING_INSET_PX = 28
WHEEL_SCROLL_DURATION_MS = 220
WHEEL_IMMEDIATE_STEP = 1
SETTINGS_ANCHOR_DURATION_MS = 220
SCROLL_BOUNCE_DISTANCE_PX = 36
SCROLL_BOUNCE_DURATION_MS = 250
SCROLL_BOUNCE_IDLE_MS = 100
MAC_TRACKPAD_PULL_PX = 16


def scroll_pull_offset(offset,wheel_delta,limit=SCROLL_BOUNCE_DISTANCE_PX):
    """Resist the wheel more as visual overscroll approaches its limit."""
    step=max(-12.,min(12.,float(wheel_delta)*.12))
    if offset*step>0:
        step*=max(.08,1-abs(offset)/limit)
    return max(-limit,min(limit,offset+step))


def _at_scroll_edge(bar,distance):
    return (distance<0 and bar.value()<=bar.minimum()) or (distance>0 and bar.value()>=bar.maximum())


def _parent_can_scroll(area,distance):
    parent=area.parentWidget()
    while parent is not None:
        if isinstance(parent,QAbstractScrollArea):
            bar=parent.verticalScrollBar()
            if bar.maximum()>bar.minimum() and not _at_scroll_edge(bar,distance):return True
        parent=parent.parentWidget()
    return False


class _BounceOverlay(QWidget):
    """Brief visual overscroll; the real scrollbar never passes its limits."""
    def __init__(self,viewport):
        super().__init__(viewport);self.snapshot=None;self.offset=0.;self.edge_backgrounds=()
        self.setAttribute(Qt.WA_TransparentForMouseEvents);self.setAttribute(Qt.WA_NoSystemBackground);self.hide()

    @property
    def background(self):
        return self.edge_backgrounds[self.offset<0] if self.edge_backgrounds else QColor()

    def paintEvent(self,event):
        if self.snapshot is None:return
        painter=QPainter(self);painter.setClipRect(self.rect());painter.fillRect(self.rect(),self.background)
        painter.drawPixmap(QPointF(0,self.offset),self.snapshot);painter.end()


def _bounce_edge_colors(area,snapshot):
    from native_ui_common import qss_color
    import ui_theme
    color=area.property('scrollBackdropColor')
    if color:return qss_color(color),qss_color(color)
    if area.property('scrollBackdropFromSnapshot'):
        # The overview's left viewport gutter always belongs to its backing.
        # Use the composited color so the stretch follows translucent glass.
        image=snapshot.toImage()
        if not image.isNull():
            corner=image.pixelColor(0,0)
            if corner.alpha():return corner,corner
    role=area.property('scrollBackdropRole')
    fallback=qss_color(ui_theme.color(role or 'window_backdrop'))
    if role:return fallback,fallback
    image=snapshot.toImage()
    width,height=image.width(),image.height()
    if not width or not height:return fallback,fallback
    # Sample both exposed edges. A corner can belong to a border, gutter or
    # scrollbar, while a few text/icon pixels should not tint the whole gap.
    def color_at_edge(top):
        rows=(min(height-1,step) if top else max(0,height-1-step) for step in (1,3,5))
        colors=Counter(image.pixel(x,y) for y in rows for x in range(width//16,width,max(1,width//24))
                       if image.pixelColor(x,y).alpha()==255)
        return QColor.fromRgb(colors.most_common(1)[0][0]) if colors else fallback
    return color_at_edge(True),color_at_edge(False)


class ScrollBoundaryBounce(QObject):
    def __init__(self,area):
        super().__init__(area);self.area=area;self.overlay=_BounceOverlay(area.viewport())
        self.animation=DisplayAnimation(self);self.animation.setDuration(SCROLL_BOUNCE_DURATION_MS)
        self.animation.valueChanged.connect(self._frame);self.animation.finished.connect(self.stop)
        self.ease=QEasingCurve(QEasingCurve.OutCubic);self.start_offset=0.
        self.idle=QTimer(self);self.idle.setSingleShot(True);self.idle.timeout.connect(self.release)
        area.viewport().installEventFilter(self)

    def kick(self,distance):
        if not motion_allowed() or not self.area.isVisible():return
        overlay=self.overlay;self.animation.stop()
        if not overlay.isVisible():
            viewport=self.area.viewport();window=viewport.window()
            fade=getattr(self.area,'_flickdock_edge_fade',None)
            if fade is not None:fade._capturing=True
            # Many app viewports are transparent. Capturing them alone loses
            # the parent surface and can paint black into the bounce snapshot.
            # Capture their composited region from the real Qt window instead.
            try:
                if window is not viewport and window.isVisible():
                    rect=QRect(viewport.mapTo(window,QPoint()),viewport.size())
                    overlay.snapshot=window.grab(rect)
                else:overlay.snapshot=viewport.grab()
            finally:
                if fade is not None:fade._capturing=False
            if overlay.snapshot.isNull():return
            overlay.edge_backgrounds=_bounce_edge_colors(self.area,overlay.snapshot)
            overlay.setGeometry(self.area.viewport().rect());overlay.show();overlay.raise_()
            if fade is not None:fade.set_bounce_source(overlay)
        # Resist progressively near the limit while retaining direction, so
        # reversing a gesture unwinds the stretch instead of jumping.
        overlay.offset=scroll_pull_offset(overlay.offset,-distance)
        overlay.update();self.idle.start(SCROLL_BOUNCE_IDLE_MS)

    def release(self):
        self.idle.stop()
        if not self.overlay.isVisible():return
        self.start_offset=self.overlay.offset
        self.animation.stop();self.animation.setStartValue(0.);self.animation.setEndValue(1.);self.animation.start()

    def _frame(self,value):
        progress=max(0.,min(1.,float(value)))
        self.overlay.offset=self.start_offset*(1-self.ease.valueForProgress(progress))
        self.overlay.update()

    def stop(self):
        self.idle.stop();self.animation.stop();self.overlay.hide();self.overlay.snapshot=None;self.overlay.offset=0.
        fade=getattr(self.area,'_flickdock_edge_fade',None)
        if fade is not None:fade.set_bounce_source(None)

    def eventFilter(self,watched,event):
        if event.type() in (QEvent.Hide,QEvent.Resize,QEvent.MouseButtonPress,QEvent.KeyPress,QEvent.TouchBegin):self.stop()
        return False


def _bounce_at_edge(area,distance):
    bar=area.verticalScrollBar()
    existing=getattr(area,'_flickdock_boundary_bounce',None)
    if existing is not None and existing.overlay.isVisible() and existing.overlay.offset*distance>0:
        existing.kick(distance)
        if existing.overlay.offset*distance<=0:existing.stop()
        return True
    if (not _at_scroll_edge(bar,distance) or _parent_can_scroll(area,distance)
            or (bar.maximum()<=bar.minimum() and not getattr(area,'_flickdock_edge_bounce',False))):return False
    bounce=getattr(area,'_flickdock_boundary_bounce',None)
    if bounce is None:bounce=area._flickdock_boundary_bounce=ScrollBoundaryBounce(area)
    bounce.kick(distance)
    return True


def _stop_bounce(area):
    bounce=getattr(area,'_flickdock_boundary_bounce',None)
    if bounce is not None:bounce.stop()


def _release_precision_bounce(area):
    area._flickdock_trackpad_edge=0
    bounce=getattr(area,'_flickdock_boundary_bounce',None)
    if bounce is not None and bounce.animation.state()!=bounce.animation.Running:bounce.release()


def _precision_bounce(area,distance,phase):
    if sys.platform!='darwin' or phase==Qt.ScrollPhase.NoScrollPhase:
        return _bounce_at_edge(area,distance)
    if phase==Qt.ScrollPhase.ScrollBegin:area._flickdock_trackpad_edge=0
    direction=1 if distance>0 else -1
    locked=getattr(area,'_flickdock_trackpad_edge',0)
    if not _at_scroll_edge(area.verticalScrollBar(),distance):
        area._flickdock_trackpad_edge=0
        if locked:
            _stop_bounce(area)
            return False
    elif phase==Qt.ScrollPhase.ScrollMomentum:
        _release_precision_bounce(area)
        return False
    elif locked==direction:
        return True
    if not _bounce_at_edge(area,distance):return False
    bounce=getattr(area,'_flickdock_boundary_bounce',None)
    if bounce is not None and abs(bounce.overlay.offset)>=MAC_TRACKPAD_PULL_PX:
        bounce.release()
        area._flickdock_trackpad_edge=direction
    return True


class InertialWheelScroll(QObject):
    """Coalesced mouse-wheel glide; native precision scrolling stays native."""
    def __init__(self,area):
        super().__init__(area);self.area=area;self.bar=area.verticalScrollBar();self.target=self.bar.value()
        if isinstance(area,QAbstractItemView):area.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.motion=DisplayAnimation(self);self.motion.setDuration(WHEEL_SCROLL_DURATION_MS)
        self.motion.setEasingCurve(QEasingCurve.OutCubic)
        self.motion.valueChanged.connect(lambda value:self.bar.setValue(round(value)))
        self.bar.sliderPressed.connect(self.motion.stop)
        area.installEventFilter(self);area.viewport().installEventFilter(self);self.bar.installEventFilter(self)

    def eventFilter(self,watched,event):
        if event.type() in (QEvent.Hide,QEvent.MouseButtonPress,QEvent.KeyPress,QEvent.TouchBegin):self.motion.stop()
        return False

    def wheel(self,event):
        if event.modifiers() & (Qt.ControlModifier|Qt.ShiftModifier):return False
        if event.phase()==Qt.ScrollPhase.ScrollEnd:
            _release_precision_bounce(self.area)
            return False
        if event.pixelDelta().y() or event.angleDelta().y():
            before=getattr(self.area,'_flickdock_before_scroll',None)
            if callable(before):before()
        if event.pixelDelta().y() or event.phase()!=Qt.ScrollPhase.NoScrollPhase:
            self.motion.stop()
            distance=-(event.pixelDelta().y() or event.angleDelta().y())
            if distance and _precision_bounce(self.area,distance,event.phase()):event.accept();return True
            if event.phase()==Qt.ScrollPhase.ScrollMomentum and sys.platform=='darwin' and _at_scroll_edge(self.bar,distance):return False
            _stop_bounce(self.area);return False
        angle=event.angleDelta().y()
        if not angle:return False
        distance=-angle/120*max(1,QApplication.wheelScrollLines())*self.bar.singleStep()
        bounce=getattr(self.area,'_flickdock_boundary_bounce',None)
        if bounce is not None and bounce.overlay.isVisible() and bounce.overlay.offset*distance>0:
            _bounce_at_edge(self.area,distance);event.accept();return True
        current=self.bar.value();pending=self.target-current
        base=self.target if self.motion.state()==self.motion.Running and pending*distance>0 else current
        target=max(self.bar.minimum(),min(self.bar.maximum(),round(base+distance)))
        self.motion.stop();self.target=target
        if target==current:
            if _bounce_at_edge(self.area,distance):event.accept();return True
            return False
        _stop_bounce(self.area)
        if motion_allowed() and self.area.isVisible():
            immediate=current+(min(WHEEL_IMMEDIATE_STEP,abs(target-current)) if target>current else -min(WHEEL_IMMEDIATE_STEP,abs(target-current)))
            self.bar.setValue(immediate);self.motion.setStartValue(float(immediate));self.motion.setEndValue(float(target));self.motion.start()
        else:self.bar.setValue(target)
        event.accept();return True


def _blurred_edge(sample, height, *, top, tone):
    """Progressively diffuse the captured pixels, keeping center content sharp."""
    width=sample.width()
    y=0 if top else sample.height()-height
    output=sample.copy(0,y,width,height)
    destination=QRect(0,0,width,height)
    target=QRect(0,-y,width,sample.height())
    start,stop=(0,height) if top else (height,0)
    level=QImage(output.size(),QImage.Format_ARGB32_Premultiplied)
    p=QPainter(output)
    for scale,end in ((3.,1.),(6.,.78),(12.,.52)):
        low_detail=sample.scaled(max(1,round(width/scale)),max(1,round(sample.height()/scale)),
                                 Qt.IgnoreAspectRatio,Qt.SmoothTransformation)
        layer=QPainter(level)
        layer.setCompositionMode(QPainter.CompositionMode_Source)
        layer.setRenderHint(QPainter.SmoothPixmapTransform)
        layer.drawImage(target,low_detail)
        layer.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        blend=QLinearGradient(0,start,0,stop)
        blend.setColorAt(0,Qt.black)
        blend.setColorAt(end*.30,Qt.black)
        blend.setColorAt(end,Qt.transparent)
        layer.fillRect(destination,blend)
        layer.end()
        p.drawImage(0,0,level)
    clear=QColor(tone);clear.setAlpha(0)
    veil=QLinearGradient(0,start,0,stop)
    veil.setColorAt(0,tone)
    veil.setColorAt(.08,tone)
    veil.setColorAt(.58,clear)
    p.fillRect(destination,veil);p.end()
    return output


class _ScrollEdgeFade(QWidget):
    """Composite blur from the current scroll frame within fixed edge bands."""
    def __init__(self,area):
        viewport=area.viewport()
        # Text and item views can scroll their viewport's child widgets with
        # the content. A sibling keeps the optical edge fixed to the frame.
        super().__init__(viewport.parentWidget())
        self.area=area
        self.bar=area.verticalScrollBar()
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._strengths=(0.,0.)
        self._capturing=False
        self._bounce_source=None
        self._capture_image=QImage()
        self.setGeometry(viewport.geometry())
        viewport.installEventFilter(self)
        self.bar.valueChanged.connect(self.sync)
        self.bar.rangeChanged.connect(self.sync)
        self.sync()

    def sync(self,*args):
        ramp=min(32,max(1,self._edge_height()))
        top=self.bar.value()-self.bar.minimum()
        bottom=self.bar.maximum()-self.bar.value()
        self._strengths=tuple(_smoothstep(min(1.,distance/ramp)) for distance in (top,bottom))
        self.setVisible(any(self._strengths))
        self._update_bands()

    def _update_bands(self):
        height=self._edge_height()
        if self.isVisible() and height>0:
            self.update(QRegion(0,0,self.width(),height).united(
                QRegion(0,self.height()-height,self.width(),height)))

    def eventFilter(self,watched,event):
        kind=event.type()
        if kind in (QEvent.Resize,QEvent.Move):
            self.setGeometry(watched.geometry())
            self.raise_()
            self.sync()
        elif kind in (QEvent.PaletteChange,QEvent.StyleChange,QEvent.Show):
            self._update_bands()
        elif kind==QEvent.Hide:
            self._capture_image=QImage()
        return False

    def set_bounce_source(self,overlay):
        if self._bounce_source is overlay:return
        self._bounce_source=overlay
        if overlay is not None:self.raise_()
        self.sync()

    def _edge_height(self):
        compact=self.area.property('scrollBackdropColor') is not None
        return min(18 if compact else 48,self.height()//4)

    def _tone(self):
        from native_ui_common import colors,qss_color
        import ui_theme
        owner=self.area.window()
        theme=getattr(owner,'_applied_settings_theme',None) or owner.property('uiTheme') or ui_theme.current_theme()
        color=self.area.property('scrollBackdropColor')
        role=self.area.property('scrollBackdropRole')
        if color:
            tone=qss_color(color)
        elif role:
            tone=qss_color(colors(theme)[role])
        else:
            tone=self.area.viewport().palette().base().color()
            if tone.alpha()==0:
                tone=qss_color(colors(theme)['window_backdrop'])
        return tone

    def paintEvent(self,event):
        if self._capturing:return
        height=self._edge_height()
        if height<=0 or self.width()<=0:return
        source=self._bounce_source or self.area.viewport()
        tone=self._tone()
        sampled=min(self.height(),height+12)
        dirty=event.region()
        bands=[]
        for top,strength in zip((True,False),self._strengths):
            y=0 if top else self.height()-height
            if strength>0 and dirty.intersects(QRect(0,y,self.width(),height)):
                bands.append((top,strength,y,QRect(0,0 if top else self.height()-sampled,self.width(),sampled)))
        if not bands:return
        painter=QPainter(self)
        self._capturing=True
        try:
            # One traversal of the viewport when both edge bands are dirty.
            # The gap between the bands never gets sampled or filled.
            samples=QRegion()
            for _,_,_,region in bands:samples+=QRegion(region)
            bounds=samples.boundingRect()
            if self._capture_image.size()!=bounds.size():
                self._capture_image=QImage(bounds.size(),QImage.Format_ARGB32_Premultiplied)
            capture=self._capture_image
            p=QPainter(capture)
            p.setCompositionMode(QPainter.CompositionMode_Source)
            for _,_,_,region in bands:p.fillRect(region.translated(-bounds.topLeft()),tone)
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            source.render(p,QPoint(),samples,QWidget.DrawChildren)
            p.end()
            for top,strength,y,region in bands:
                raster=capture.copy(region.translated(-bounds.topLeft())) if len(bands)>1 else capture
                image=_blurred_edge(raster,height,top=top,tone=tone)
                # Blend into the actual live pixels at the inner boundary.
                # The resampled copy never replaces crisp content in the center.
                mask=QPainter(image)
                mask.setCompositionMode(QPainter.CompositionMode_DestinationIn)
                gradient=QLinearGradient(0,0 if top else height,0,height if top else 0)
                gradient.setColorAt(0,Qt.black)
                gradient.setColorAt(.64,Qt.black)
                gradient.setColorAt(1,Qt.transparent)
                mask.fillRect(image.rect(),gradient);mask.end()
                painter.setOpacity(strength);painter.drawImage(0,y,image)
        finally:
            self._capturing=False
            painter.end()


def _smoothstep(t):
    return t*t*(3-2*t)


class _SmoothScrollRouter(QObject):
    """Attach wheel momentum lazily to each application's scroll viewport."""
    def eventFilter(self,watched,event):
        kind=event.type()
        if kind==QEvent.Show and isinstance(watched,QAbstractScrollArea):
            if watched.property('scrollEdgeBlur') is not False and not getattr(watched,'_flickdock_edge_fade',None):
                watched._flickdock_edge_fade=_ScrollEdgeFade(watched)
            return False
        if kind!=QEvent.Wheel or not isinstance(watched,QWidget):return False
        area=watched if isinstance(watched,QAbstractScrollArea) else watched.parentWidget()
        while area is not None and not isinstance(area,QAbstractScrollArea):area=area.parentWidget()
        if area is None or watched not in (area,area.viewport(),area.verticalScrollBar()):return False
        # The settings document has its own anchor-aware wheel controller.
        if area.objectName()=='settingsScroll':return False
        controller=getattr(area,'_flickdock_wheel_scroll',None)
        if controller is None:
            controller=area._flickdock_wheel_scroll=InertialWheelScroll(area)
        return controller.wheel(event)


def install_smooth_scrolling(app):
    if not getattr(app,'_flickdock_scroll_router',None):
        app._flickdock_scroll_router=_SmoothScrollRouter(app)
        app.installEventFilter(app._flickdock_scroll_router)


class _CategoryDelegate(QStyledItemDelegate):
    def paint(self,painter,option,index):
        owner=self.parent();c=owner._colors();rect=QRectF(option.rect).adjusted(2,0,-2,-4)
        pill_r=rect.height()/2.0
        painter.save();painter.setRenderHint(QPainter.Antialiasing);painter.setClipRect(option.rect)
        if option.state & QStyle.State_MouseOver:
            paint_rounded_surface(painter,rect,pill_r,QColor(c['rail_item_hover']))
        if not owner._marker_rect.isEmpty():
            marker=owner._marker_rect.adjusted(2,0,-2,0)
            painter.save();painter.setOpacity(owner._marker_opacity)
            paint_rounded_surface(painter,marker,marker.height()/2.0,QColor(c['rail_active']));painter.restore()
        point=rect.center()
        active=owner._marker_rect.contains(point) and owner._marker_opacity>.55
        painter.setPen(QColor(c['rail_active_ink'] if active else c['rail_item_ink']));painter.setFont(option.font)
        text=option.fontMetrics.elidedText(str(index.data() or ''),Qt.ElideRight,max(1,round(rect.width()-24)))
        painter.drawText(rect.adjusted(14,0,-10,0),Qt.AlignLeft|Qt.AlignVCenter,text);painter.restore()


class AnimatedCategoryList(QListWidget):
    """Settings sidebar marker with the same interruptible navigation motion."""
    def __init__(self,parent=None):
        super().__init__(parent);self._marker_rect=QRectF();self._marker_target=QRectF()
        self._marker_opacity=1.;self._marker_start_opacity=1.;self._marker_warp=False;self._marker_theme='light'
        self._marker_animation=DisplayAnimation(self);self._marker_animation.setDuration(190)
        self._marker_animation.valueChanged.connect(self._marker_frame)
        self.setItemDelegate(_CategoryDelegate(self));self.currentRowChanged.connect(self._move_marker)
        self.viewport().installEventFilter(self);self.verticalScrollBar().valueChanged.connect(lambda *_:self._sync_marker())

    def _colors(self):
        from native_ui_common import colors
        return colors(self._marker_theme)

    def set_selection_theme(self,theme):self._marker_theme=theme;self.viewport().update()

    def setCurrentRow(self,row,*args):
        super().setCurrentRow(row,*args);self._move_marker(row)

    def _target_for(self,row):
        item=self.item(row)
        return QRectF(self.visualItemRect(item)).adjusted(0,0,0,-4) if item is not None else QRectF()

    def _move_marker(self,row):
        target=self._target_for(row)
        if target.isEmpty() or target==self._marker_target:return
        self._marker_target=target;self._marker_animation.stop()
        if self._marker_rect.isEmpty() or not self.isVisible() or not motion_allowed():
            self._marker_rect=target;self._marker_opacity=1.;self.viewport().update();return
        self._marker_start_opacity=self._marker_opacity
        self._marker_warp=abs(target.center().y()-self._marker_rect.center().y())>max(72.,target.height()*2.2)
        self._marker_animation.setEasingCurve(QEasingCurve.InOutCubic if self._marker_warp else QEasingCurve.OutCubic)
        self._marker_animation.setStartValue(QRectF(self._marker_rect));self._marker_animation.setEndValue(target);self._marker_animation.start()

    def _marker_frame(self,value):
        if not isinstance(value,QRectF):return
        self._marker_rect=QRectF(value);t=min(1.,max(0.,self._marker_animation.currentTime()/max(1,self._marker_animation.duration())))
        smooth=lambda x:x*x*(3-2*x)
        if self._marker_warp:self._marker_opacity=self._marker_start_opacity*(1-smooth(t*2)) if t<.5 else smooth((t-.5)*2)
        else:self._marker_opacity=self._marker_start_opacity+(1-self._marker_start_opacity)*smooth(t)
        self.viewport().update()

    def _sync_marker(self):
        target=self._target_for(self.currentRow())
        if target.isEmpty():return
        self._marker_animation.stop();self._marker_rect=self._marker_target=target;self._marker_opacity=1.;self.viewport().update()

    def eventFilter(self,watched,event):
        if watched is self.viewport() and event.type() in (QEvent.Resize,QEvent.Show):QTimer.singleShot(0,self._sync_marker)
        return super().eventFilter(watched,event)

    def hideEvent(self,event):
        self._marker_animation.stop();self._marker_rect=QRectF(self._marker_target);self._marker_opacity=1.
        super().hideEvent(event)


class SettingsNavigation(QObject):
    def __init__(self, dialog):
        super().__init__(dialog)
        self.dialog = dialog
        self.bar = dialog.scroll.verticalScrollBar()
        self.anchor = None
        self.motion = DisplayAnimation(self)
        self.motion.setDuration(SETTINGS_ANCHOR_DURATION_MS)
        self.motion.setEasingCurve(QEasingCurve.OutCubic)
        self.motion.valueChanged.connect(lambda value: self.bar.setValue(round(value)))
        self.motion.finished.connect(self.track)
        self.wheel_motion=DisplayAnimation(self)
        self.wheel_motion.setDuration(WHEEL_SCROLL_DURATION_MS)
        self.wheel_motion.setEasingCurve(QEasingCurve.OutCubic)
        self.wheel_motion.valueChanged.connect(lambda value:self.bar.setValue(round(value)))
        self.wheel_target=0
        self.bar.valueChanged.connect(self.track)
        self.bar.sliderPressed.connect(self._interrupt)
        dialog.scroll.viewport().installEventFilter(self)
        self.bar.installEventFilter(self)
        dialog.installEventFilter(self)

    def go(self, index):
        if not 0 <= index < len(self.dialog.setting_categories):
            return
        self.wheel_motion.stop()
        _stop_bounce(self.dialog.scroll)
        section = self.dialog.setting_categories[index][1][0]
        if self.dialog._body_fit_timer.isActive():
            self.dialog._body_fit_timer.stop();self.dialog._fit_settings_body()
        self.dialog.body_layout.activate()
        target = min(self.bar.maximum(), max(0, section.y() - ANCHOR_TOP_INSET_PX))
        if self.anchor==(index,target) and self.motion.state()==self.motion.Running:return
        self.motion.stop()
        self.anchor = (index,target)
        if motion_allowed() and self.dialog.isVisible():
            self.motion.setStartValue(float(self.bar.value()))
            self.motion.setEndValue(float(target))
            self.motion.start()
        else:
            self.bar.setValue(target)
            self.track()

    def track(self, *_):
        value = self.bar.value()
        if self.anchor is not None:
            index,target=self.anchor
            if self.motion.state()==self.motion.Running or abs(value-target)<=1:
                if self.dialog.category_list.currentRow()!=index:
                    with QSignalBlocker(self.dialog.category_list):self.dialog.category_list.setCurrentRow(index)
                return
            self.anchor=None
        index = 0
        for number, (_, sections) in enumerate(self.dialog.setting_categories):
            if sections[0].y() <= value + TRACKING_INSET_PX:
                index = number
        if self.bar.maximum() and value >= self.bar.maximum():
            index = len(self.dialog.setting_categories) - 1
        if self.dialog.category_list.currentRow()!=index:
            with QSignalBlocker(self.dialog.category_list):self.dialog.category_list.setCurrentRow(index)

    def eventFilter(self, watched, event):
        if event.type()==QEvent.Wheel and watched in (self.dialog.scroll.viewport(),self.bar):
            return self._wheel(event)
        if event.type() in (QEvent.TouchBegin, QEvent.MouseButtonPress, QEvent.KeyPress, QEvent.Hide):
            self._interrupt()
        return False

    def _wheel(self,event):
        if event.modifiers() & (Qt.ControlModifier|Qt.ShiftModifier):return False
        if event.phase()==Qt.ScrollPhase.ScrollEnd:
            _release_precision_bounce(self.dialog.scroll)
            return False
        pixels=event.pixelDelta().y();angle=event.angleDelta().y()
        if not pixels and not angle:return False
        self.motion.stop();self.anchor=None
        if not pixels and event.phase()!=Qt.ScrollPhase.NoScrollPhase:
            self.wheel_motion.stop()
            if angle and _precision_bounce(self.dialog.scroll,-angle,event.phase()):event.accept();return True
            if event.phase()==Qt.ScrollPhase.ScrollMomentum and sys.platform=='darwin' and _at_scroll_edge(self.bar,-angle):return False
            _stop_bounce(self.dialog.scroll);return False
        if pixels:
            # Precision trackpads already supply their own system momentum.
            if _precision_bounce(self.dialog.scroll,-pixels,event.phase()):event.accept();return True
            if not (event.phase()==Qt.ScrollPhase.ScrollMomentum and sys.platform=='darwin' and _at_scroll_edge(self.bar,-pixels)):
                _stop_bounce(self.dialog.scroll)
            self.wheel_motion.stop();self.bar.setValue(self.bar.value()-pixels)
        else:
            distance=-angle/120*max(1,QApplication.wheelScrollLines())*self.bar.singleStep()
            bounce=getattr(self.dialog.scroll,'_flickdock_boundary_bounce',None)
            if bounce is not None and bounce.overlay.isVisible() and bounce.overlay.offset*distance>0:
                _bounce_at_edge(self.dialog.scroll,distance);event.accept();return True
            current=self.bar.value();pending=self.wheel_target-current
            base=self.wheel_target if self.wheel_motion.state()==self.wheel_motion.Running and pending*distance>0 else current
            target=max(self.bar.minimum(),min(self.bar.maximum(),round(base+distance)))
            self.wheel_motion.stop();self.wheel_target=target
            if target==current and _bounce_at_edge(self.dialog.scroll,distance):event.accept();return True
            _stop_bounce(self.dialog.scroll)
            if motion_allowed() and self.dialog.isVisible() and target!=current:
                immediate=current+(min(WHEEL_IMMEDIATE_STEP,abs(target-current)) if target>current else -min(WHEEL_IMMEDIATE_STEP,abs(target-current)))
                self.bar.setValue(immediate)
                self.wheel_motion.setStartValue(float(immediate));self.wheel_motion.setEndValue(float(target));self.wheel_motion.start()
            else:self.bar.setValue(target)
        event.accept();return True

    def _interrupt(self):
        self.motion.stop()
        self.wheel_motion.stop()
        _stop_bounce(self.dialog.scroll)
        self.anchor=None
