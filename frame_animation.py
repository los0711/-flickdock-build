"""Display-paced Python UI timelines using public Qt interpolation only.

One finite clock belongs to each native host. QVariantAnimation's shared native
timer is never started, so it cannot clamp these UI timelines to ~16 ms. This
adapter supports the Python API used by FlickDock; it is deliberately not a
QAnimationGroup/QML/C++-metaobject animation backend. Those APIs would inspect
Qt's private native state rather than the Python UI state exposed here.

Two unused Qt observation details are deliberately outside this adapter's
contract: restart resets value before emitting Running (Qt emits Running while
the old value is still observable), and reversing exactly on a multi-loop
boundary recomputes its loop/value immediately. FlickDock's UI does not use
stateChanged/currentLoopChanged subscribers, loops or direction changes.
Do not extend that scope without native differential tests; this is not a
drop-in replacement for every QAbstractAnimation consumer.
"""
from collections import OrderedDict
import sys
import time
import weakref
from PySide6.QtCore import QCoreApplication,QEvent,QObject,QSignalBlocker,QVariantAnimation
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid
from native_ui_common import ScreenAnimationTimer


def reduced_motion():
    from native_ui_common import reduced_motion as preference
    return preference()


def _anchor(animation):
    parent=animation.parent()
    while parent is not None and not isinstance(parent,QWidget):parent=parent.parent()
    return parent


class _WindowClock(QObject):
    def __init__(self,host):
        super().__init__(host);self.members=OrderedDict();self.timer=ScreenAnimationTimer(self,host if isinstance(host,QWidget) else None)
        self.timer.timeout.connect(self._tick)
        if isinstance(host,QWidget):host.installEventFilter(self)

    def add(self,animation):
        self.members[id(animation)]=weakref.ref(animation)
        watched=animation._destroy_driver() if animation._destroy_driver else None
        if watched is not self:
            if watched is not None and isValid(watched):
                try:animation.destroyed.disconnect(watched.prune)
                except (RuntimeError,TypeError):pass
            animation.destroyed.connect(self.prune);animation._destroy_driver=weakref.ref(self)
        if not self.timer.isActive():self.timer.start()

    def remove(self,animation):
        self.members.pop(id(animation),None)
        if not self.members:self.timer.stop()

    def prune(self,*_):
        for key,reference in tuple(self.members.items()):
            value=reference()
            if value is None or not isValid(value):self.members.pop(key,None)
        if not self.members:self.timer.stop()

    def _tick(self):
        now=time.perf_counter();reduced=reduced_motion()
        for key,reference in tuple(self.members.items()):
            if self.members.get(key) is not reference:continue
            animation=reference()
            if animation is None or not isValid(animation):self.members.pop(key,None);continue
            try:animation._advance(now,reduced)
            except Exception as error:
                # One failed presentation must not starve every other timeline
                # on this host. Stop only that timeline and report the original
                # exception through Python's normal diagnostics hook.
                if isValid(animation):animation.stop()
                sys.excepthook(type(error),error,error.__traceback__)
            if not isValid(self):return
        if not self.members:self.timer.stop()

    def eventFilter(self,watched,event):
        if (event.type()==QEvent.Hide or event.type()==QEvent.WindowStateChange
                and isinstance(watched,QWidget) and watched.isMinimized()):
            for reference in tuple(self.members.values()):
                animation=reference()
                if animation is not None and isValid(animation):animation.stop()
        return False


class FrameAnimation(QVariantAnimation):
    """Qt easing/value interpolation, screen-paced wall-clock lifecycle."""
    def __init__(self,parent=None):
        super().__init__(parent);self._public_state=self.State.Stopped;self._intent=0
        self._driver=None;self._destroy_driver=None;self._watched=None
        self._offset=0;self._started=0.;self._driving=False
        self._deletion=self.DeletionPolicy.KeepWhenStopped

    def state(self):return self._public_state

    def _change_state(self,state):
        old=self._public_state
        if old!=state:self._public_state=state;self.stateChanged.emit(state,old)

    def _clock(self):
        anchor=_anchor(self);host=anchor.window() if anchor is not None else QCoreApplication.instance()
        if host is None:raise RuntimeError('UI animation requires an application')
        clock=getattr(host,'_flickdock_frame_clock',None)
        if clock is None or not isValid(clock):clock=_WindowClock(host);host._flickdock_frame_clock=clock
        if self._watched is not anchor:
            if self._watched is not None and isValid(self._watched):self._watched.removeEventFilter(self)
            self._watched=anchor
            if anchor is not None:anchor.installEventFilter(self)
        if self._driver is not None and self._driver is not clock and isValid(self._driver):self._driver.remove(self)
        self._driver=clock;return clock

    def _detach(self):
        if self._driver is not None and isValid(self._driver):self._driver.remove(self)

    def _anchor_time(self):self._offset=self.currentTime();self._started=time.perf_counter()

    def start(self,policy=QVariantAnimation.DeletionPolicy.KeepWhenStopped):
        if self.state()==self.State.Running or self.loopCount()==0:return
        if self.state()==self.State.Paused:self.resume();return
        self._intent+=1;intent=self._intent;self._deletion=policy
        end=self.totalDuration() if self.totalDuration()>=0 else self.duration()
        QVariantAnimation.setCurrentTime(self,0 if self.direction()==self.Direction.Forward else end)
        if not isValid(self) or intent!=self._intent:return
        self._anchor_time();self._change_state(self.State.Running)
        if not isValid(self) or intent!=self._intent or self.state()!=self.State.Running:return
        if self.duration()==0 or reduced_motion():self._settle_reduced();return
        self._clock().add(self)

    def stop(self):
        self._intent+=1;self._detach();self._change_state(self.State.Stopped)
        if isValid(self) and self._deletion==self.DeletionPolicy.DeleteWhenStopped:self.deleteLater()

    def pause(self):
        if self.state()!=self.State.Running:return
        self._detach();self._change_state(self.State.Paused)

    def resume(self):
        if self.state()!=self.State.Paused:return
        self._anchor_time();intent=self._intent;self._change_state(self.State.Running)
        if not isValid(self) or intent!=self._intent or self.state()!=self.State.Running:return
        if reduced_motion():self._settle_reduced()
        else:self._clock().add(self)

    def setPaused(self,paused):self.pause() if paused else self.resume()

    def setDirection(self,direction):
        active=self.state()!=self.State.Stopped;before=self.currentTime();old=self.direction()
        if not active:QVariantAnimation.setDirection(self,direction);return
        # The native object remains stopped. Prevent its stopped-direction
        # endpoint reset from leaking transient valueChanged(1) into live UI.
        with QSignalBlocker(self):
            QVariantAnimation.setDirection(self,direction);QVariantAnimation.setCurrentTime(self,before)
        self._anchor_time()
        if old!=direction:self.directionChanged.emit(direction)

    def setCurrentTime(self,value):
        intent=self._intent;active=self.state()!=self.State.Stopped
        QVariantAnimation.setCurrentTime(self,value)
        if not isValid(self) or intent!=self._intent:return
        if active:
            total=self.totalDuration()
            ended=(self.direction()==self.Direction.Forward and total>=0 and self.currentTime()>=total
                   or self.direction()==self.Direction.Backward and self.currentTime()<=0)
            if ended:self._complete()
            elif not self._driving and self.state()==self.State.Running:self._anchor_time()

    def _complete(self):
        if self.state()==self.State.Stopped:return
        intent=self._intent;self._detach();self._change_state(self.State.Stopped)
        if not isValid(self) or intent!=self._intent or self.state()!=self.State.Stopped:return
        self.finished.emit()
        if isValid(self) and intent==self._intent and self._deletion==self.DeletionPolicy.DeleteWhenStopped:self.deleteLater()

    def _settle_reduced(self):
        intent=self._intent;end=self.totalDuration()
        if self.direction()==self.Direction.Backward:end=0
        elif end<0:end=(self.currentLoop()+1)*self.duration()
        self.setCurrentTime(end)
        if isValid(self) and intent==self._intent and self.state()!=self.State.Stopped:self._complete()

    def _advance(self,now,reduced=False):
        if self.state()!=self.State.Running:self._detach();return
        if reduced:self._settle_reduced();return
        # A sibling value callback may resume this timeline after the host's
        # captured tick time. Never rewind the freshly anchored pose.
        elapsed=max(0,round((now-self._started)*1000))
        value=self._offset+elapsed*(1 if self.direction()==self.Direction.Forward else -1)
        self._driving=True
        try:self.setCurrentTime(max(0,value))
        finally:
            if isValid(self):self._driving=False

    def eventFilter(self,watched,event):
        if (event.type()==QEvent.Hide or event.type()==QEvent.WindowStateChange
                and isinstance(watched,QWidget) and watched.isMinimized()):self.stop()
        return False
