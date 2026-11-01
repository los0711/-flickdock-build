"""Finite utility transitions, separate from the native shelf compositor."""
from PySide6.QtCore import QCoreApplication,QEvent,QObject,QPoint,QPointF,QRectF,Qt,QTimer,QVariantAnimation,QEasingCurve
from PySide6.QtGui import QGuiApplication,QPainter
from PySide6.QtWidgets import QWidget,QGraphicsEffect
from shiboken6 import isValid


class SegmentIndicator(QWidget):
    """One painted moving selection, behind immediately usable buttons."""
    def __init__(self,owner,buttons):
        super().__init__(owner);self.buttons=tuple(buttons);self._rect=QRectF();self._pending=False
        self.setAttribute(Qt.WA_TransparentForMouseEvents);self.setAttribute(Qt.WA_NoSystemBackground)
        self.animation=QVariantAnimation(self);self.animation.setDuration(160);self.animation.setEasingCurve(exit_curve())
        self.animation.valueChanged.connect(self._frame)
        owner.installEventFilter(self)
        for button in self.buttons:button.toggled.connect(lambda checked:self.schedule() if checked else None)
        self.lower();self.show();self.schedule()

    def schedule(self):
        if isValid(self) and not self._pending:self._pending=True;QTimer.singleShot(0,self,self._move)

    def _move(self):
        if not isValid(self):return
        self._pending=False
        if not isValid(self.parentWidget()):return
        self.setGeometry(self.parentWidget().rect());self.lower()
        chosen=next((button for button in self.buttons if button.isChecked()),None)
        if chosen is None:self.animation.stop();self._frame(QRectF());return
        target=QRectF(chosen.geometry())
        if self._rect==target:return
        self.animation.stop()
        if self._rect.isEmpty() or not self.isVisible() or not motion_allowed():self._frame(target);return
        self.animation.setStartValue(self._rect);self.animation.setEndValue(target);self.animation.start()

    def _frame(self,rect):
        if self.animation.state()==self.animation.State.Running and not motion_allowed():
            self.animation.stop()
            chosen=next((button for button in self.buttons if button.isChecked()),None)
            rect=QRectF(chosen.geometry()) if chosen is not None else QRectF()
        self._rect=QRectF(rect);self.update()
    def set_theme(self,theme):self._theme=theme;self.update()

    def eventFilter(self,watched,event):
        if event.type()==QEvent.Hide:
            self.animation.stop();self._rect=QRectF()
        elif event.type() in (QEvent.Show,QEvent.Resize,QEvent.LayoutRequest):self.schedule()
        elif event.type() in (QEvent.StyleChange,QEvent.PaletteChange):self.update()
        return False

    def paintEvent(self,event):
        if self._rect.isEmpty():return
        from PySide6.QtGui import QColor
        import ui_theme
        theme=getattr(self,'_theme',ui_theme.current_theme());p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen);p.setBrush(QColor(255,255,255,31) if theme=='dark' else QColor('white'))
        p.drawRoundedRect(self._rect,5,5);p.end()


def motion_allowed():
    from native_ui_common import reduced_motion
    return not reduced_motion()


def exit_curve():
    curve=QEasingCurve(QEasingCurve.BezierSpline)
    curve.addCubicBezierSegment(QPointF(.23,1),QPointF(.32,1),QPointF(1,1))
    return curve


class DwellHint(QObject):
    """A single two-pixel glyph lift after deliberate hover; never an idle loop."""
    def __init__(self,owner):
        super().__init__(owner);self.owner=owner;self.value=0.
        self.wait=QTimer(self);self.wait.setSingleShot(True);self.wait.setInterval(1400)
        self.wait.timeout.connect(self._start)
        self.animation=QVariantAnimation(self);self.animation.setDuration(240)
        self.animation.setEasingCurve(exit_curve())
        self.animation.setKeyValueAt(0.,0.);self.animation.setKeyValueAt(.45,1.);self.animation.setKeyValueAt(1.,0.)
        self.animation.valueChanged.connect(self._frame);owner.installEventFilter(self)

    def enter(self):
        self.stop()
        if motion_allowed() and self.owner.isVisible():self.wait.start()

    def _start(self):
        if self.owner.isVisible() and self.owner.isEnabled() and motion_allowed():self.animation.start()

    def _frame(self,value):
        if self.animation.state()==self.animation.State.Running and not motion_allowed():
            self.wait.stop();self.animation.stop();value=0.
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
        self.animation=QVariantAnimation(self);self.animation.setDuration(240)
        self.animation.valueChanged.connect(self._frame);self.curve=exit_curve()
        owner.installEventFilter(self)

    def reveal(self,*,reset=False):
        if reset:self._frame(0.)
        self._target(1.)

    def conceal(self):self._target(0.)

    def _target(self,target):
        self.animation.stop()
        if not motion_allowed():self._frame(target);return
        current=self.progress
        self.animation.setDuration(240 if target else 160)
        self.animation.setStartValue(current);self.animation.setEndValue(target);self.animation.start()

    def _frame(self,value):
        if self.animation.state()==self.animation.State.Running and not motion_allowed():
            self.animation.stop();value=self.animation.endValue()
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


class WindowFade(QObject):
    """Temporary .97↔1 scale and fade; never resize/reparent a native window.

    The live source stays interactive and up to date. A temporary root effect
    is removed completely at either endpoint, leaving no settings-tree cache.
    Existing effects (including child shadows) are never replaced.
    """
    def __init__(self,window,*,automatic=True):
        super().__init__(window);self.window=window;self.pending=None;self.completing=False
        self.automatic=automatic
        self._intent=0
        self.scale=1.;self.alpha=1.;self._effect=None;self._from=(1.,1.);self._to=(1.,1.)
        self.animation=QVariantAnimation(self);self.animation.setDuration(180)
        self.animation.setEasingCurve(exit_curve())
        self.animation.valueChanged.connect(self._frame)
        self.animation.finished.connect(self._finished);window.installEventFilter(self)

    def enabled(self):
        return QGuiApplication.platformName() not in ('offscreen','minimal') and motion_allowed()

    def eventFilter(self,watched,event):
        if QCoreApplication.closingDown() or not isValid(self.window):return False
        if event.type()==QEvent.Show and self.automatic and not event.spontaneous() and not self.window.isMinimized():
            self.pending=None;self.animation.stop()
            if self.enabled():
                self.alpha=.78;self.scale=.97;self._start(1.0)
            else:self._reset_visual()
        elif event.type()==QEvent.Hide:
            self._intent+=1
            self.animation.stop();self.pending=None;self._reset_visual()
        elif event.type()==QEvent.WindowStateChange and self.window.isMinimized():
            self.animation.stop();self.pending=None;self._reset_visual()
        return super().eventFilter(watched,event)

    def _prepare_effect(self):
        if self._effect is not None and isValid(self._effect):return
        if self.window.graphicsEffect() is not None:return
        self._effect=_PopupScaleEffect(self.window)
        self.window.setGraphicsEffect(self._effect)

    def _frame(self,value):
        if not isValid(self.window):return
        if self.animation.state()==self.animation.State.Running and not motion_allowed():
            self.animation.stop();self._finished();return
        value=float(value)
        self.alpha=self._from[0]+(self._to[0]-self._from[0])*value
        self.scale=self._from[1]+(self._to[1]-self._from[1])*value
        if self.window.isWindow():self.window.setWindowOpacity(self.alpha)
        if self._effect is not None and isValid(self._effect):
            self._effect.scale=self.scale
            self._effect.alpha=1. if self.window.isWindow() else self.alpha
            self._effect.update()

    def present_pose(self,alpha,scale):
        """One external clock (e.g. a sliding toast), never a second fade."""
        self._prepare_effect();self._from=self._to=(float(alpha),float(scale));self._frame(1.)

    def settle_visual(self):self._reset_visual()

    def _reset_visual(self):
        self.alpha=self.scale=1.
        if not isValid(self.window):return
        effect,self._effect=self._effect,None
        if effect is not None and isValid(effect) and self.window.graphicsEffect() is effect:
            self.window.setGraphicsEffect(None)
            # Clear the transient effect's backing store at completion. In
            # particular the settings update stripe must return to live paint.
            self.window.update()
            for child in self.window.findChildren(QWidget):child.update()
        if self.window.isWindow():self.window.setWindowOpacity(1.)

    def _start(self,target):
        self._intent+=1
        self.animation.stop()
        self._prepare_effect();self._from=(self.alpha,self.scale)
        self._to=(target,1. if target else .97)
        self.animation.setStartValue(0.);self.animation.setEndValue(1.);self._frame(0.);self.animation.start()

    def defer_done(self,result,callback):
        if self.completing or not self.window.isVisible() or self.window.isMinimized() or not self.enabled():return False
        if self.pending is None:
            self.pending=lambda:callback(result);self._start(0.0)
        return True

    def reopen(self):
        # QWidget.show() on an already visible fading window emits no Show.
        # Explicit open intent must reverse the pending close from this frame.
        if self.pending is not None:
            self.pending=None;self.animation.stop()
            if self.enabled():self._start(1.0)
            else:self._reset_visual()

    def _finished(self):
        intent=self._intent
        callback,self.pending=self.pending,None
        if callback:
            self.completing=True
            try:callback()
            finally:
                self.completing=False
                if intent==self._intent:self._reset_visual()
        else:self._reset_visual()


class _PopupScaleEffect(QGraphicsEffect):
    """Live-source scale only, installed for at most one short transition."""
    def __init__(self,parent):
        super().__init__(parent);self.scale=1.;self.alpha=1.

    def draw(self,painter):
        if abs(self.scale-1.)<.00001 and self.alpha>=.99999:self.drawSource(painter);return
        offset=QPoint();source=self.sourcePixmap(Qt.LogicalCoordinates,offset,QGraphicsEffect.NoPad)
        if source.isNull():return
        painter.save();painter.setOpacity(painter.opacity()*self.alpha)
        center=self.sourceBoundingRect(Qt.LogicalCoordinates).center()
        painter.translate(center);painter.scale(self.scale,self.scale);painter.translate(-center)
        painter.drawPixmap(offset,source);painter.restore()


class _ClosingSnapshot(QWidget):
    """Visual-only exit after a child confirmation has already committed."""
    def __init__(self,widget):
        parent=widget.parentWidget()
        super().__init__(parent);self.snapshot=widget.grab();self.mix=0.
        self.setGeometry(widget.geometry());self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground);self.setFocusPolicy(Qt.NoFocus)
        self.animation=QVariantAnimation(self);self.animation.setDuration(160);self.animation.setEasingCurve(exit_curve())
        self.animation.valueChanged.connect(self._frame);self.animation.finished.connect(self.finish)
        parent.installEventFilter(self)
        anchor=getattr(widget,'card',None) or getattr(getattr(widget,'_owner',None),'card',None)
        if anchor is not None and anchor is not widget and anchor is not parent:anchor.installEventFilter(self)
        self.show();self.raise_()
        self.animation.setStartValue(0.);self.animation.setEndValue(1.);self.animation.start()

    def _frame(self,value):
        if self.animation.state()==self.animation.State.Running and not motion_allowed():self.finish();return
        self.mix=float(value);self.update()

    def finish(self):
        if not isValid(self):return
        self.animation.stop();self.hide();self.snapshot=None;self.deleteLater()

    def eventFilter(self,watched,event):
        if event.type() in (QEvent.Hide,QEvent.Close,QEvent.Resize,QEvent.Move):self.finish()
        return False

    def paintEvent(self,event):
        if self.snapshot is None:return
        p=QPainter(self);p.setRenderHint(QPainter.SmoothPixmapTransform);p.setOpacity(1-self.mix)
        center=QRectF(self.rect()).center();p.translate(center);p.scale(1-.03*self.mix,1-.03*self.mix);p.translate(-center)
        p.drawPixmap(QPoint(),self.snapshot);p.end()


def finish_closing_snapshot(widget):
    ghost=getattr(widget,'_closing_snapshot',None)
    if ghost is not None and isValid(ghost):ghost.finish()
    widget._closing_snapshot=None


def close_snapshot(widget):
    """Start only a ghost: caller hides and completes business immediately."""
    finish_closing_snapshot(widget)
    parent=widget.parentWidget()
    if (not QCoreApplication.closingDown() and widget.isVisible() and parent is not None
            and isValid(parent) and parent.isVisible() and motion_allowed()):
        widget._closing_snapshot=_ClosingSnapshot(widget)


class SnapshotFade(QWidget):
    """Fade the old page over immediately usable new contents, with no effects replacement."""
    def __init__(self,parent):
        super().__init__(parent);self.snapshot=None;self.alpha=0.0
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground);self.hide()
        self.animation=QVariantAnimation(self);self.animation.setDuration(180)
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

    def _frame(self,value):
        if self.animation.state()==self.animation.State.Running and not motion_allowed():self.finish();return
        self.alpha=float(value);self.update()

    def finish(self):
        self.animation.stop();self.hide();self.snapshot=None;self.alpha=0.0

    def paintEvent(self,event):
        if self.snapshot is None:return
        painter=QPainter(self);painter.setOpacity(self.alpha)
        painter.drawPixmap(QPoint(),self.snapshot);painter.end()
