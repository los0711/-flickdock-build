"""One scroll document with accessible anchor navigation and scroll tracking."""
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSignalBlocker, QTimer, QEasingCurve, Qt
from PySide6.QtGui import QColor,QPainter
from PySide6.QtWidgets import QApplication,QAbstractItemView,QAbstractScrollArea,QListWidget,QStyledItemDelegate,QStyle,QWidget
from display_frames import DisplayAnimation
from ui_transitions import motion_allowed

ANCHOR_TOP_INSET_PX = 20
TRACKING_INSET_PX = 28
WHEEL_SCROLL_DURATION_MS = 220
WHEEL_IMMEDIATE_STEP = 1
SETTINGS_ANCHOR_DURATION_MS = 220
SCROLL_BOUNCE_DISTANCE_PX = 36
SCROLL_BOUNCE_DURATION_MS = 250
SCROLL_BOUNCE_IDLE_MS = 100


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
        super().__init__(viewport);self.snapshot=None;self.offset=0.;self.background=QColor()
        self.setAttribute(Qt.WA_TransparentForMouseEvents);self.setAttribute(Qt.WA_NoSystemBackground);self.hide()

    def paintEvent(self,event):
        if self.snapshot is None:return
        painter=QPainter(self);painter.setClipRect(self.rect());painter.fillRect(self.rect(),self.background)
        painter.drawPixmap(QPointF(0,self.offset),self.snapshot);painter.end()


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
            # Many app viewports are transparent. Capturing them alone loses
            # the parent surface and can paint black into the bounce snapshot.
            # Capture their composited region from the real Qt window instead.
            if window is not viewport and window.isVisible():
                rect=QRect(viewport.mapTo(window,QPoint()),viewport.size())
                overlay.snapshot=window.grab(rect)
            else:overlay.snapshot=viewport.grab()
            if overlay.snapshot.isNull():return
            color=overlay.snapshot.toImage().pixelColor(0,0)
            if color.alpha()<255:
                import ui_theme
                color=QColor(ui_theme.color('background'))
            overlay.background=color;overlay.setGeometry(self.area.viewport().rect());overlay.show();overlay.raise_()
        # Resist progressively near the limit while retaining direction, so
        # reversing a gesture unwinds the stretch instead of jumping.
        delta=max(-12.,min(12.,-float(distance)*.12))
        if overlay.offset*delta>0:
            delta*=max(.08,1-abs(overlay.offset)/SCROLL_BOUNCE_DISTANCE_PX)
        overlay.offset=max(-SCROLL_BOUNCE_DISTANCE_PX,min(SCROLL_BOUNCE_DISTANCE_PX,overlay.offset+delta))
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
            bounce=getattr(self.area,'_flickdock_boundary_bounce',None)
            if bounce is not None:bounce.release()
            return False
        if event.pixelDelta().y() or event.angleDelta().y():
            before=getattr(self.area,'_flickdock_before_scroll',None)
            if callable(before):before()
        if event.pixelDelta().y() or event.phase()!=Qt.ScrollPhase.NoScrollPhase:
            self.motion.stop()
            distance=-(event.pixelDelta().y() or event.angleDelta().y())
            if distance and _bounce_at_edge(self.area,distance):event.accept();return True
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


class _SmoothScrollRouter(QObject):
    """Attach wheel momentum lazily to each application's scroll viewport."""
    def eventFilter(self,watched,event):
        if event.type()!=QEvent.Wheel or not isinstance(watched,QWidget):return False
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
        owner=self.parent();c=owner._colors();rect=QRectF(option.rect).adjusted(0,0,0,-4)
        painter.save();painter.setRenderHint(QPainter.Antialiasing);painter.setClipRect(option.rect)
        painter.setPen(Qt.NoPen)
        if option.state & QStyle.State_MouseOver:
            painter.setBrush(QColor(c['hover']));painter.drawRoundedRect(rect,8,8)
        if not owner._marker_rect.isEmpty():
            painter.save();painter.setOpacity(owner._marker_opacity);painter.setBrush(QColor(c['action']))
            painter.drawRoundedRect(owner._marker_rect,8,8);painter.restore()
        point=rect.center()
        active=owner._marker_rect.contains(point) and owner._marker_opacity>.55
        painter.setPen(QColor(c['on_action'] if active else c['secondary']));painter.setFont(option.font)
        text=option.fontMetrics.elidedText(str(index.data() or ''),Qt.ElideRight,max(1,round(rect.width()-20)))
        painter.drawText(rect.adjusted(10,0,-10,0),Qt.AlignLeft|Qt.AlignVCenter,text);painter.restore()


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
            bounce=getattr(self.dialog.scroll,'_flickdock_boundary_bounce',None)
            if bounce is not None:bounce.release()
            return False
        pixels=event.pixelDelta().y();angle=event.angleDelta().y()
        if not pixels and not angle:return False
        self.motion.stop();self.anchor=None
        if not pixels and event.phase()!=Qt.ScrollPhase.NoScrollPhase:
            self.wheel_motion.stop()
            if angle and _bounce_at_edge(self.dialog.scroll,-angle):event.accept();return True
            _stop_bounce(self.dialog.scroll);return False
        if pixels:
            # Precision trackpads already supply their own system momentum.
            if _bounce_at_edge(self.dialog.scroll,-pixels):event.accept();return True
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
