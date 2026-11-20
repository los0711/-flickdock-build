"""Utility-window transitions, separate from the native shelf compositor."""
import math
from PySide6.QtCore import QCoreApplication,QEvent,QObject,QPoint,QPointF,QRectF,Qt,QTimer,QEasingCurve,Signal,Slot
from PySide6.QtGui import QGuiApplication,QPainter,QPixmap
from PySide6.QtWidgets import QWidget,QFrame,QGraphicsEffect
from shiboken6 import isValid
from display_frames import DisplayAnimation

UTILITY_TRANSITION_MS=180
COPY_NOTICE_MORPH_MS=420
COPY_NOTICE_FADE_MS=160
COPY_NOTICE_OVERSHOOT=1.15
NOTICE_SPLIT_MS=420
NOTICE_SPLIT_BRIDGE_END=.58
DROP_HOVER_TRANSITION_MS=180
WINDOW_MIN_SCALE=.96
WINDOW_OPEN_OPACITY=.78
SHELF_REVEAL_RESPONSE_SECONDS=.36
SHELF_REVEAL_DAMPING=.52
SHELF_REVEAL_SCALE=(.92,.875)
OVERVIEW_MORPH_DURATION_MS=360
OVERVIEW_MORPH_REBOUND_PX=12.
CARD_REBOUND_SPRING=(240,20)
SURFACE_SOURCE_FADE_END=.45
SURFACE_TARGET_FADE_END=.72
STACK_SPREAD_RESPONSE_SECONDS=.72
STACK_SPREAD_DAMPING=.88
STACK_LANDING_DURATION_MS=440
STACK_PULSE_TIMEOUT_MS=950
SHARED_ELEMENT_MAX_TRAVEL_PX=600
EMPTY_STATE_ENTRY_MS=180
EMPTY_STATE_ENTRY_OFFSET_PX=6


def spring_progress(elapsed,stiffness,damping):
    decay=damping/2
    frequency=math.sqrt(stiffness-decay**2)
    angle=frequency*elapsed
    return 1-math.exp(-decay*elapsed)*(math.cos(angle)+decay/frequency*math.sin(angle))


def motion_allowed():
    from native_ui_common import reduced_motion
    return not reduced_motion()


def exit_curve():
    curve=QEasingCurve(QEasingCurve.BezierSpline)
    curve.addCubicBezierSegment(QPointF(.23,1),QPointF(.32,1),QPointF(1,1))
    return curve


class SelectionSurface(QFrame):
    """A shared selection surface that can be retargeted while moving.

    Buttons keep ownership of input and state; only the background moves. The
    horizontal spring retains velocity across interruptions, while vertical
    navigation uses a short ease-out. Reduced motion snaps to the target.
    """
    selection_settled=Signal()

    def __init__(self,parent=None,*,vertical=False,color_role='card',border_role='border',radius=6,motion=True):
        super().__init__(parent)
        self._selection_buttons=[];self._selection_button=None;self._selection_rect=QRectF()
        self._selection_target=QRectF();self._selection_theme=None
        self._surface_background=None
        self._selection_opacity=1.;self._selection_start_opacity=1.;self._selection_warp=False
        self._vertical_spring=None
        self._selection_vertical=vertical;self._selection_color_role=color_role
        self._selection_border_role=border_role;self._selection_radius=radius;self._selection_motion=motion
        if vertical:
            self._selection_animation=DisplayAnimation(self);self._selection_animation.setDuration(190)
            self._selection_animation.setEasingCurve(exit_curve());self._selection_animation.valueChanged.connect(self._animation_frame)
            self._selection_animation.finished.connect(self.selection_settled)
        else:
            from flickdock_motion import Spring
            self._selection_animation=Spring(self,{'x':0,'y':0,'w':0,'h':0},self._spring_frame)
            self._selection_animation.settled.connect(self.selection_settled)

    def selection_is_moving(self):
        if self._selection_vertical:
            return self._selection_animation.state()==self._selection_animation.Running
        return self._selection_animation._timer.isActive()

    def set_selection_theme(self,theme):
        self._selection_theme=theme;self.update()

    def configure_vertical_spring(self,stiffness,damping):
        self._vertical_spring=(math.sqrt(stiffness-damping*damping/4),damping/2)
        self._selection_animation.setDuration(450)

    def set_surface_background(self,dark,light,radius,*,stroke_role=None):
        self._surface_background=(dark,light,float(radius),stroke_role);self.update()

    def add_selection_button(self,button):
        if button in self._selection_buttons:return
        self._selection_buttons.append(button);button.installEventFilter(self)
        button.toggled.connect(lambda checked,b=button:self._choose(b) if checked else None)
        if button.isChecked():self._choose(button)

    def clear_selection(self):
        """Clear both the selected button and its painted moving surface."""
        self._selection_animation.stop()
        self._selection_button=None
        self._selection_rect=QRectF();self._selection_target=QRectF()
        self._selection_opacity=1.;self._selection_start_opacity=1.;self._selection_warp=False
        self.update()

    def _enabled_motion(self):
        enabled=self._selection_motion() if callable(self._selection_motion) else self._selection_motion
        return bool(enabled and motion_allowed() and self.isVisible())

    def _choose(self,button):
        self._selection_button=button
        target=QRectF(button.geometry())
        if target.isEmpty():return
        self._move_selection(target)

    def _move_selection(self,target,*,layout=False):
        target=QRectF(target)
        if target.isEmpty():return
        if target==self._selection_target and not layout:return
        old_target=self._selection_target
        self._selection_target=target
        if (layout and self._vertical_spring and old_target.y()==target.y() and
                self._selection_animation.state()==self._selection_animation.Running):
            self._selection_rect=QRectF(target.x(),self._selection_rect.y(),target.width(),target.height())
            self.update()
            return
        if self._selection_rect.isEmpty() or layout or not self._enabled_motion():
            was_moving=self.selection_is_moving()
            if self._selection_vertical:self._selection_animation.stop()
            else:self._selection_animation.jump(dict(x=target.x(),y=target.y(),w=target.width(),h=target.height()))
            self._selection_rect=target;self._selection_opacity=1.;self.update()
            if was_moving:self.selection_settled.emit()
            return
        if self._selection_vertical:
            self._selection_animation.stop();self._selection_animation.setStartValue(QRectF(self._selection_rect))
            self._selection_start_opacity=self._selection_opacity
            self._selection_warp=not self._vertical_spring and abs(target.center().y()-self._selection_rect.center().y())>max(72.,target.height()*2.2)
            curve=QEasingCurve.Linear if self._vertical_spring else QEasingCurve.InOutCubic if self._selection_warp else exit_curve()
            self._selection_animation.setEasingCurve(curve)
            self._selection_animation.setEndValue(target);self._selection_animation.start()
        else:
            self._selection_animation.animate(dict(x=target.x(),y=target.y(),w=target.width(),h=target.height()),response=.24,damping=.72)

    def _spring_frame(self,value):
        self._selection_rect=QRectF(value['x'],value['y'],value['w'],value['h']);self.update()

    def _animation_frame(self,value):
        if isinstance(value,QRectF):
            self._selection_rect=QRectF(value)
            t=min(1.,max(0.,self._selection_animation.currentTime()/max(1,self._selection_animation.duration())))
            if self._vertical_spring:
                frequency,decay=self._vertical_spring
                elapsed=self._selection_animation.currentTime()/1000
                angle=frequency*elapsed
                at=1. if t>=1 else 1-math.exp(-decay*elapsed)*(math.cos(angle)+decay/frequency*math.sin(angle))
                start=self._selection_animation.startValue().top()
                self._selection_rect=QRectF(self._selection_target)
                self._selection_rect.moveTop(start+(self._selection_rect.top()-start)*at)
                self._selection_opacity=1.
                self.update()
                return
            smooth=lambda x:x*x*(3-2*x)
            if self._selection_warp:
                self._selection_opacity=(self._selection_start_opacity*(1-smooth(t*2)) if t<.5 else smooth((t-.5)*2))
            else:self._selection_opacity=self._selection_start_opacity+(1-self._selection_start_opacity)*smooth(t)
            self.update()

    def _layout_selection(self):
        if self._selection_button is not None:
            target=QRectF(self._selection_button.geometry())
            if target!=self._selection_target:self._move_selection(target,layout=True)

    def eventFilter(self,watched,event):
        if watched in self._selection_buttons:
            if watched is self._selection_button and event.type() in (QEvent.Move,QEvent.Resize,QEvent.Show):
                QTimer.singleShot(0,self,self._layout_selection)
            if watched is self._selection_button and event.type() in (QEvent.Enter,QEvent.Leave,QEvent.MouseButtonPress,QEvent.MouseButtonRelease):self.update()
        return super().eventFilter(watched,event)

    def showEvent(self,event):
        super().showEvent(event);QTimer.singleShot(0,self._layout_selection)

    def hideEvent(self,event):
        self._selection_animation.stop()
        if not self._selection_target.isEmpty():self._selection_rect=QRectF(self._selection_target)
        self._selection_opacity=1.
        super().hideEvent(event)

    def paintEvent(self,event):
        super().paintEvent(event)
        import ui_theme
        from native_ui_common import colors,paint_rounded_surface,qss_color
        theme=self._selection_theme or ui_theme.current_theme();c=colors(theme)
        if self._surface_background is not None:
            dark,light,radius,stroke_role=self._surface_background;fill=dark if theme=='dark' else light
            p=QPainter(self);paint_rounded_surface(p,self.rect(),radius,qss_color(c.get(fill,fill)),
                                                    qss_color(c[stroke_role]) if stroke_role else None);p.end()
        if self._selection_rect.isEmpty() or self._selection_button is None:return
        role=self._selection_color_role
        border_role=self._selection_border_role
        button=self._selection_button
        draw_radius=self._selection_radius
        if role=='card':
            role='segment_selected'
            border_role=None
            draw_radius=min(float(self._selection_radius),self._selection_rect.height()/2.0)
            if not button.isEnabled():role='control'
        elif role=='rail_active':
            border_role=None
            draw_radius=self._selection_rect.height()/2.0
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing);p.setOpacity(self._selection_opacity)
        paint_rounded_surface(p,self._selection_rect,draw_radius,qss_color(c[role]),
                              qss_color(c[border_role]) if border_role else None)
        p.end()


class TabPillSurface(SelectionSurface):
    """One elastic indicator whose edges keep their velocity on a new click."""

    # DesignBookmark's tab demo tunes the shared lead/trail presets for its tabs.
    LEADING=(620,40)
    TRAILING=(170,19)

    def __init__(self,parent,*,motion=True):
        super().__init__(parent,color_role='selected',border_role=None,radius=13,motion=motion)
        from flickdock_motion import Spring
        self._selection_animation._on_update=self._edge_changed
        self._right_edge=Spring(self,{'x':0},self._edge_changed)

    def _move_selection(self,target,*,layout=False):
        if target.isEmpty() or target==self._selection_target:return
        left,right=self._selection_animation,self._right_edge
        old=self._selection_target
        self._selection_target=QRectF(target)
        if old.isEmpty() or layout or not self._enabled_motion():
            left.jump({'x':target.left()})
            right.jump({'x':target.right()})
            return
        goes_right=target.center().x()>old.center().x()
        left.animate_physical({'x':target.left()},*(self.TRAILING if goes_right else self.LEADING))
        right.animate_physical({'x':target.right()},*(self.LEADING if goes_right else self.TRAILING))

    def _edge_changed(self,_):
        left=self._selection_animation.values['x']
        right=self._right_edge.values['x']
        self._selection_rect=QRectF(left,self._selection_target.y(),max(0.,right-left),self._selection_target.height())
        self.update()

    def clear_selection(self):
        self._right_edge.stop()
        super().clear_selection()

    def hideEvent(self,event):
        target=self._selection_target
        self._selection_animation.jump({'x':target.left()})
        self._right_edge.jump({'x':target.right()})
        super().hideEvent(event)


class PreviewWidthTransition(QObject):
    """Ease the overview's preview width so the adjacent search field follows."""
    def __init__(self,panel,motion=True):
        super().__init__(panel);self.panel=panel;self.motion_enabled=motion
        self.active=False;self.target_visible=False;self.target_width=0
        self.animation=DisplayAnimation(self);self.animation.setDuration(180);self.animation.setEasingCurve(exit_curve())
        self.animation.valueChanged.connect(self._frame);self.animation.finished.connect(self._finish)

    def stop(self):
        self.animation.stop();self.active=False

    def request(self,visible,width,*,animate):
        visible=bool(visible);width=max(0,int(width))
        if self.active and self.target_visible==visible and self.target_width==width:return
        enabled=self.motion_enabled() if callable(self.motion_enabled) else self.motion_enabled
        can_animate=bool(animate and enabled and motion_allowed() and self.panel.parentWidget().isVisible())
        if not self.active and visible==(not self.panel.isHidden()):
            if visible and self.panel.width()!=width:self.panel.setFixedWidth(width)
            self.target_visible=visible;self.target_width=width;return
        start=self.panel.width() if not self.panel.isHidden() else 0
        self.animation.stop();self.target_visible=visible;self.target_width=width
        if not can_animate:
            self.active=False;self.panel.setVisible(visible);self.panel.setFixedWidth(width);return
        self.panel.setFixedWidth(start);self.panel.show();self.active=True
        self.animation.setStartValue(float(start));self.animation.setEndValue(float(width if visible else 0))
        self.animation.start()

    def _frame(self,value):
        if self.active:self.panel.setFixedWidth(max(0,round(value)))

    def _finish(self):
        self.active=False
        if not self.target_visible:self.panel.hide()
        self.panel.setFixedWidth(self.target_width)


class _EmptyStateEffect(QGraphicsEffect):
    """Animate painted pixels without changing the layout-owned position."""
    def __init__(self,parent):
        super().__init__(parent);self.progress=1.

    def set_progress(self,value):
        self.progress=max(0.,min(1.,float(value)));self.update()

    def boundingRectFor(self,rect):return rect.adjusted(0,0,0,EMPTY_STATE_ENTRY_OFFSET_PX)

    def draw(self,painter):
        offset=QPoint();source=self.sourcePixmap(Qt.LogicalCoordinates,offset,QGraphicsEffect.PixmapPadMode.NoPad)
        painter.save();painter.setOpacity(self.progress)
        painter.drawPixmap(QPointF(offset)+QPointF(0,EMPTY_STATE_ENTRY_OFFSET_PX*(1-self.progress)),source)
        painter.restore()


class EmptyStateEntrance(QObject):
    """One reusable, interruptible entrance for a small native empty target."""
    settled=Signal()
    def __init__(self,widget,motion):
        super().__init__(widget);self.widget=widget;self.motion=motion
        assert widget.graphicsEffect() is None
        self.effect=_EmptyStateEffect(widget);self.effect.setEnabled(False);widget.setGraphicsEffect(self.effect)
        self.animation=DisplayAnimation(self);self.animation.setDuration(EMPTY_STATE_ENTRY_MS)
        self.animation.setEasingCurve(exit_curve());self.animation.valueChanged.connect(self._frame)
        self.animation.finished.connect(self.finish);self.active=False
        widget.installEventFilter(self)

    def begin(self):
        self.finish()
        if not self.motion() or not motion_allowed() or not self.widget.isVisible():return
        self.active=True;self.effect.setEnabled(True)
        self.animation.setStartValue(0.);self.animation.setEndValue(1.);self._frame(0.)
        self.animation.start()

    def _frame(self,value):
        if not self.active:return
        self.effect.set_progress(value)

    def finish(self):
        if not self.active:return
        self.active=False;self.animation.stop();self.effect.set_progress(1.);self.effect.setEnabled(False)
        self.widget.update()
        self.settled.emit()

    def eventFilter(self,watched,event):
        if event.type() in (QEvent.Hide,QEvent.Resize):self.finish()
        return False


class DwellHint(QObject):
    """A single two-pixel glyph lift after deliberate hover; never an idle loop."""
    def __init__(self,owner):
        super().__init__(owner);self.owner=owner;self.value=0.
        self.wait=QTimer(self);self.wait.setSingleShot(True);self.wait.setInterval(1400)
        self.wait.timeout.connect(self._start)
        self.animation=DisplayAnimation(self);self.animation.setDuration(240)
        self.animation.setEasingCurve(exit_curve())
        self.animation.setKeyValueAt(0.,0.);self.animation.setKeyValueAt(.45,1.);self.animation.setKeyValueAt(1.,0.)
        self.animation.valueChanged.connect(self._frame);owner.installEventFilter(self)

    def enter(self):
        self.stop()
        if motion_allowed() and self.owner.isVisible():self.wait.start()

    def _start(self):
        if self.owner.isVisible() and self.owner.isEnabled() and motion_allowed():self.animation.start()

    def _frame(self,value):
        self.value=float(value)
        if isValid(self.owner):self.owner.update()

    def stop(self):
        self.wait.stop();self.animation.stop();self._frame(0.)

    def eventFilter(self,watched,event):
        if QCoreApplication.closingDown() or not isValid(self.owner) or not isValid(self.animation):return False
        if event.type() in (QEvent.Hide,QEvent.Leave,QEvent.MouseButtonPress,QEvent.DragEnter):self.stop()
        elif event.type()==QEvent.EnabledChange and not self.owner.isEnabled():self.stop()
        return False


class ToolbarEntrance(QObject):
    """One finite clock for all compact header glyphs, with fixed hit regions."""
    def __init__(self,owner,controls):
        super().__init__(owner);self.owner=owner;self.controls=controls;self.progress=1.
        self.animation=DisplayAnimation(self);self.animation.setDuration(240)
        self.animation.valueChanged.connect(self._frame);self.curve=exit_curve()
        owner.installEventFilter(self)

    def reveal(self,*,reset=False):
        if reset:self._frame(0.)
        self._target(1.)

    def conceal(self):self._target(0.)

    def _target(self,target):
        self.animation.stop()
        if not motion_allowed():self._frame(1.);return
        current=self.progress
        self.animation.setDuration(240 if target else 160)
        self.animation.setStartValue(current);self.animation.setEndValue(target);self.animation.start()

    def _frame(self,value):
        self.progress=float(value);controls=self.controls()
        for index,control in enumerate(controls):
            local=max(0.,min(1.,(self.progress-index*.055)/(1-max(0,len(controls)-1)*.055)))
            local=self.curve.valueForProgress(local)
            control._entry_alpha=local;control._entry_dx=6*(1-local);control.update()

    def eventFilter(self,watched,event):
        if event.type()==QEvent.Hide:
            self.animation.stop();self._frame(1.)
        return False


def fade_hide(window):
    """Hide without emitting dialog rejection or changing business state."""
    fade=getattr(window,'_window_fade',None)
    if fade is None or not fade.defer_done(None,lambda _:window.hide()):window.hide()


WINDOW_FADE_ANIMATION = False


class _WindowZoomOverlay(QWidget):
    """Scale one captured surface over the window without an effect pipeline.

    A QGraphicsEffect forces every frame through an offscreen window render.
    Painting the cached snapshot in a transparent child keeps the frame cost
    to one scaled blit, which is what makes large dialogs open smoothly.
    """
    def __init__(self,window):
        super().__init__(window)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAutoFillBackground(False)
        self.snapshot=QPixmap();self.scale=1.;self.opacity=1.;self.hide()

    def clear(self):
        self.snapshot=QPixmap();self.scale=1.;self.opacity=1.;self.hide()

    def capture(self,window):
        self.setGeometry(window.rect())
        pixmap=window.grab()
        if pixmap.isNull():return False
        pixmap.setDevicePixelRatio(1.0)
        self.snapshot=pixmap
        return True

    def paintEvent(self,event):
        if self.snapshot.isNull():return
        painter=QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform,abs(self.scale-1.)>0.004)
        painter.setOpacity(max(0.,min(1.,self.opacity)))
        width=self.width()*self.scale;height=self.height()*self.scale
        target=QRectF((self.width()-width)/2,(self.height()-height)/2,width,height)
        painter.drawPixmap(target,self.snapshot,QRectF(self.snapshot.rect()))


class WindowFade(QObject):
    """Keep native focus, shadow and hit testing; never animate geometry."""
    def __init__(self,window,*,motion=True):
        super().__init__(window);self.window=window;self.pending=None;self.completing=False
        self.motion=bool(motion)
        self.scale=1.;self.opacity=1.;self.zoom=None;self._awaiting_capture=False
        self.animation=DisplayAnimation(self);self.animation.setDuration(UTILITY_TRANSITION_MS)
        self.animation.setEasingCurve(exit_curve())
        self.animation.valueChanged.connect(self._frame)
        self.animation.finished.connect(self._finished);window.installEventFilter(self)
        self._capture_deadline=QTimer(self);self._capture_deadline.setSingleShot(True)
        self._capture_deadline.timeout.connect(self._capture_expired)

    def enabled(self):
        # Dialogs open and close instantly: the snapshot entrance added a
        # capture step before the first visible frame, which reads as a stall.
        return (WINDOW_FADE_ANIMATION and self.motion
                and QGuiApplication.platformName() not in ('offscreen','minimal')
                and motion_allowed())

    def _frame(self,value):
        # Blend and scale the same captured surface. Changing native window
        # alpha on every frame adds a second platform composition/update path.
        self.opacity=value.x();self.scale=value.y()
        if self.zoom is not None:
            self.zoom.scale=self.scale;self.zoom.opacity=self.opacity;self.zoom.update()

    def _reset_zoom(self):
        self._capture_deadline.stop()
        self._awaiting_capture=False
        self.scale=1.;self.opacity=1.
        if self.zoom is not None:self.zoom.clear()
        if isValid(self.window):self.window.setWindowOpacity(1.0)

    def eventFilter(self,watched,event):
        if QCoreApplication.closingDown() or not isValid(self.window):return False
        if event.type()==QEvent.Show and not self.window.isMinimized():
            self.pending=None;self.animation.stop()
            if self.enabled() and not event.spontaneous():
                # The snapshot arrives one event-loop turn later. Hide the live
                # window for that turn so it cannot flash at full size first.
                self.window.setWindowOpacity(0.0)
                self.scale=WINDOW_MIN_SCALE;self.opacity=WINDOW_OPEN_OPACITY;self._start(1.0)
            else:self._reset_zoom();self.window.setWindowOpacity(1.0)
        elif event.type()==QEvent.Hide:
            self.animation.stop();self.pending=None;self._reset_zoom();self.window.setWindowOpacity(1.0)
        elif event.type()==QEvent.Resize and self.zoom is not None and self.zoom.isVisible():
            self.zoom.capture(self.window);self.zoom.update()
        elif event.type()==QEvent.WindowStateChange:
            self.animation.stop();self.pending=None;self._reset_zoom();self.window.setWindowOpacity(1.0)
        return super().eventFilter(watched,event)

    def _start(self,target):
        self.animation.stop()
        if self.zoom is None:self.zoom=_WindowZoomOverlay(self.window)
        if target==0 and self.zoom.snapshot.isNull():self.zoom.capture(self.window)
        self.zoom.setGeometry(self.window.rect())
        self.zoom.scale=self.scale;self.zoom.opacity=self.opacity
        self.animation.setStartValue(QPointF(self.opacity,1. if not target else self.scale))
        self.animation.setEndValue(QPointF(target,1.))
        self._awaiting_capture=False
        if self.zoom.snapshot.isNull() and not self.zoom.capture(self.window):
            self._capture_expired();return
        self.zoom.scale=self.scale;self.zoom.opacity=self.opacity
        self.zoom.show();self.zoom.raise_();self.zoom.update()
        self.animation.start()

    def _capture_expired(self):
        # An occluded/restored native window may never paint the requested
        # snapshot. Opening must still finish with live, fully opaque input.
        if self._awaiting_capture:
            self.animation.stop();self._reset_zoom();self.window.setWindowOpacity(1.0)

    @Slot()
    def _captured(self):
        if (self._awaiting_capture and isValid(self.window) and self.window.isVisible()
                and not self.zoom.snapshot.isNull()):
            self._awaiting_capture=False;self.animation.start()
            self._capture_deadline.stop()

    def defer_done(self,result,callback):
        if self.completing or not self.window.isVisible() or not self.enabled():return False
        if self.pending is None:
            self.pending=lambda:callback(result);self._start(0.0)
        return True

    def reopen(self):
        # QWidget.show() on an already visible fading window emits no Show.
        # Explicit open intent must reverse the pending close from this frame.
        if self.pending is not None:
            self.pending=None;self.animation.stop()
            if self.enabled():self._start(1.0)
            else:self._reset_zoom();self.window.setWindowOpacity(1.0)

    def _finished(self):
        callback,self.pending=self.pending,None
        if callback:
            self.completing=True
            try:callback()
            finally:self.completing=False
            if not isValid(self.window) or not self.window.isVisible():self._reset_zoom()
        else:self._reset_zoom()


class SnapshotFade(QWidget):
    """Fade the old page over immediately usable new contents, with no effects replacement."""
    def __init__(self,parent):
        super().__init__(parent);self.snapshot=None;self.alpha=0.0
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground);self.hide()
        self.animation=DisplayAnimation(self);self.animation.setDuration(180)
        self.animation.setEasingCurve(exit_curve());self.animation.valueChanged.connect(self._frame)
        self.animation.finished.connect(self.finish)
        parent.installEventFilter(self)

    def eventFilter(self,watched,event):
        if QCoreApplication.closingDown() or not isValid(self.animation):return False
        if event.type() in (QEvent.Hide,QEvent.Resize):self.finish()
        return super().eventFilter(watched,event)

    def change(self,mutation):
        owner=self.parentWidget()
        if not owner.isVisible() or not motion_allowed():
            self.finish();mutation();return
        # grab includes any existing partial transition, so quick reversals
        # continue from the visible frame rather than a stale original page.
        snapshot=owner.grab()
        self.animation.stop();mutation()
        self.snapshot=snapshot;self.alpha=1.0
        self.setGeometry(owner.rect());self.show();self.raise_()
        self.animation.setStartValue(1.0);self.animation.setEndValue(0.0);self.animation.start()

    def _frame(self,value):self.alpha=float(value);self.update()

    def finish(self):
        self.animation.stop();self.hide();self.snapshot=None;self.alpha=0.0

    def paintEvent(self,event):
        if self.snapshot is None:return
        painter=QPainter(self);painter.setOpacity(self.alpha)
        painter.drawPixmap(QPoint(),self.snapshot);painter.end()
