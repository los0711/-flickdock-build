"""Native Qt update delivery and deterministic 60–240 Hz animation samples."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'

import unittest
import time
from math import ceil
from unittest.mock import Mock,patch
from PySide6.QtCore import QAbstractAnimation,QCoreApplication,QEvent,QPoint,QRect,QRectF,QTimer,Qt
from PySide6.QtGui import QImage,QPainter,QPainterPath
from PySide6.QtWidgets import QApplication,QWidget
from PySide6.QtTest import QTest
from shiboken6 import isValid
from display_frames import FramePulse,DisplayAnimation,FrameRateGate,_set_on_battery
from flickdock_motion import Spring
from surface_morph import SurfaceMorph

APP=QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)

def flush():
    APP.processEvents()
    QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)

class DisplayFramesTests(unittest.TestCase):
    def test_closing_animated_popup_does_not_invalidate_shared_screen(self):
        from shelf_ui_common import AnimatedMenu
        from frame_diagnostics import normalize_refresh_rate
        screen=APP.primaryScreen()
        menu=AnimatedMenu(self.window);menu.addAction('Test')
        menu.aboutToHide.connect(menu.deleteLater)
        with patch('native_ui_common.reduced_motion',return_value=False):
            menu.popup(self.window.mapToGlobal(QPoint(30,30)))
            QTest.qWait(80);flush()
            clock=menu.fade._frames.clock
            self.assertIsNotNone(clock)
            self.assertEqual(clock.refresh_hz,normalize_refresh_rate(screen.refreshRate()))
            menu.close();QTest.qWait(80);flush()
        self.assertTrue(isValid(screen),'Closing a popup invalidated the shared screen wrapper')
        self.assertIs(screen,APP.primaryScreen())

    def test_drop_targets_share_native_clock_stop_when_hidden_and_respect_reduced_motion(self):
        from native_ui_common import DropBorderMotion
        targets=[QWidget(self.window) for _ in range(3)]
        borders=[DropBorderMotion(target) for target in targets]
        for target in targets:target.resize(120,80);target.show()
        with patch('native_ui_common.reduced_motion',return_value=False):
            for border in borders:border.set_active(True)
            flush();clock=borders[0].animation._frames.clock
            self.assertTrue(all(border.animation._frames.clock is clock and border.animation.state()==border.animation.Running for border in borders))
            self.assertTrue(all(border.animation._completion.isSingleShot() for border in borders))
            targets[0].hide();flush();self.assertEqual(borders[0].animation.state(),borders[0].animation.Stopped)
            targets[0].show();flush();self.assertEqual(borders[0].animation.state(),borders[0].animation.Stopped)
            path=QPainterPath();path.addRoundedRect(QRectF(1,1,118,78),12,12)
            frames=[]
            border=borders[1]
            for at in (0,90,180):
                border.animation.setCurrentTime(at)
                image=QImage(120,80,QImage.Format_ARGB32_Premultiplied);image.fill(Qt.transparent)
                painter=QPainter(image)
                border.paint(painter,path,dark=True,radius=12);self.assertEqual(painter.pen().dashOffset(),0)
                painter.end();frames.append(image)
            self.assertNotEqual(frames[0],frames[1]);self.assertNotEqual(frames[1],frames[2])
            self.assertLess(frames[1].pixelColor(60,40).alpha(),13);self.assertEqual(frames[2].pixelColor(60,40).alpha(),13)
            self.assertEqual(border.animation.state(),border.animation.Stopped)
            for border in borders:
                border.set_active(False);border.animation.setCurrentTime(border.animation.duration())
            flush();self.assertFalse(clock.pulses)
        with patch('native_ui_common.reduced_motion',return_value=True):
            borders[0].set_active(True);self.assertEqual(borders[0].animation.state(),borders[0].animation.Stopped)
            self.assertEqual(borders[0].progress,1.)

    def setUp(self):
        self.window=QWidget();self.window.resize(320,200);self.window.show();flush()

    def tearDown(self):
        self.window.close();self.window.deleteLater();flush()

    def test_window_coalesces_clients_without_animation_polling_timers(self):
        first,second=FramePulse(self.window),FramePulse(self.window)
        first.start();second.start();flush();clock=first.clock
        self.assertIs(clock,second.clock)
        # The only timer is the one-shot display-cadence timer; there is no
        # repeating animation polling timer.
        cadence=clock.findChildren(QTimer)
        self.assertTrue(all(timer.isSingleShot() for timer in cadence),cadence)
        samples=[0,0]
        first.timeout.connect(lambda:samples.__setitem__(0,samples[0]+1))
        second.timeout.connect(lambda:samples.__setitem__(1,samples[1]+1))
        with patch.object(clock.handle,'requestUpdate') as request:
            clock.pending=False;clock.queued=False
            clock.request_next();clock.request_next()
            self.assertEqual(request.call_count,1)
        self.assertFalse(clock.eventFilter(clock.handle,QEvent(QEvent.UpdateRequest)))
        self.assertEqual(samples,[1,1])
        first.stop();second.stop();flush();self.assertFalse(clock.pulses)

    def test_display_paced_clock_ignores_update_requests_and_steps_per_period(self):
        delivered=[];pulse=FramePulse(self.window);pulse.timeout.connect(lambda:delivered.append(True))
        pulse.start();flush();clock=pulse.clock;clock._display_paced=True;clock._reduced_motion=False
        clock._cancel_stall_timer();clock.pending=False;clock.queued=False
        now=[100.]
        try:
            with patch('display_frames._on_battery',False), \
                 patch('display_frames.time.perf_counter',side_effect=lambda:now[0]), \
                 patch.object(clock.handle,'isExposed',return_value=True):
                for hz in (60,75,120,144,240):
                    clock.refresh_hz=hz;clock._next_frame_at=now[0]
                    before=len(delivered);clock._cadence_fired()
                    self.assertEqual(len(delivered),before+1,hz)
                    self.assertAlmostEqual(clock._next_frame_at,now[0]+1/hz,places=7)
                    clock.eventFilter(clock.handle,QEvent(QEvent.UpdateRequest))
                    now[0]+=.5/hz;clock._cadence_fired()
                    self.assertEqual(len(delivered),before+1,hz)
                    now[0]+=.5/hz;clock._cadence_fired()
                    self.assertEqual(len(delivered),before+2,hz)
                    now[0]+=1.
        finally:pulse.stop()

    def test_hidden_window_has_no_frame_loop_and_show_resumes_it(self):
        delivered=[];pulse=FramePulse(self.window);pulse.timeout.connect(lambda:delivered.append(True))
        pulse.start();flush();clock=pulse.clock
        self.window.hide();flush()
        with patch.object(clock.handle,'requestUpdate') as request:
            clock.pending=False;clock.request_next();self.assertEqual(request.call_count,0)
        before=len(delivered);self.window.show();flush();deadline=time.monotonic()+1.
        while len(delivered)==before and time.monotonic()<deadline:QTest.qWait(10)
        self.assertTrue(pulse.isActive());self.assertGreater(len(delivered),before)
        pulse.stop()

    def test_display_rate_change_keeps_an_active_paced_clock_scheduled(self):
        pulse=FramePulse(self.window);pulse.start();flush();clock=pulse.clock
        try:
            clock._display_paced=True;clock.pending=False;clock.queued=False
            clock._arm_cadence_timer(.1)
            self.assertTrue(clock._cadence.isActive())
            clock._screen_rate_changed()
            self.assertTrue(clock._cadence.isActive())
        finally:pulse.stop()

    def test_display_paced_dispatch_preserves_frame_diagnostics(self):
        pulse=FramePulse(self.window);pulse.start();flush();clock=pulse.clock
        clock._display_paced=True;clock._reduced_motion=False;clock.refresh_hz=75
        trace=Mock()
        try:
            with patch('display_frames._on_battery',False), \
                 patch.object(clock,'trace',trace), \
                 patch.object(clock.handle,'isExposed',return_value=True), \
                 patch('display_frames.time.perf_counter',return_value=100.):
                clock._next_frame_at=100.;clock._cadence_fired()
                trace.update_request.assert_called_once_with('other',capped=False)
                trace.dispatched.assert_called_once_with()
        finally:pulse.stop()

    def test_minimized_and_unexposed_windows_do_not_request_frames(self):
        delivered=[];pulse=FramePulse(self.window);pulse.timeout.connect(lambda:delivered.append(True))
        pulse.start();flush();clock=pulse.clock;handle=clock.handle
        try:
            with patch.object(self.window,'isMinimized',return_value=True),patch.object(handle,'requestUpdate') as request:
                clock.pending=False;clock.request_next();self.assertEqual(request.call_count,0)
                before=len(delivered);clock.eventFilter(handle,QEvent(QEvent.UpdateRequest))
                self.assertEqual(len(delivered),before)
            with patch.object(handle,'isExposed',return_value=False),patch.object(handle,'requestUpdate') as request:
                clock.pending=False;clock.request_next();self.assertEqual(request.call_count,0)
                before=len(delivered);clock.eventFilter(handle,QEvent(QEvent.UpdateRequest))
                self.assertEqual(len(delivered),before)
            with patch.object(handle,'isExposed',return_value=True),patch.object(clock,'schedule') as schedule:
                clock.eventFilter(handle,QEvent(QEvent.Expose));schedule.assert_called_once()
        finally:pulse.stop()

    def test_capped_pulses_follow_native_frames_without_extending_time(self):
        for hz in (60,75,90,120,144,165,240):
            gate=FrameRateGate()
            samples=[frame for frame in range(hz) if gate.allows(frame/hz,True)]
            self.assertLessEqual(abs(len(samples)-60),1,(hz,len(samples)))
            self.assertTrue(all(a<b for a,b in zip(samples,samples[1:])))
        gate=FrameRateGate();self.assertTrue(gate.allows(1.,True))
        self.assertFalse(gate.allows(1.005,True));gate.reset()
        self.assertTrue(gate.allows(1.005,True))

    def test_battery_changes_apply_to_the_next_native_frame(self):
        delivered=[];pulse=FramePulse(self.window);pulse.timeout.connect(lambda:delivered.append(True))
        with patch('native_ui_common.reduced_motion',return_value=False):pulse.start();flush()
        clock=pulse.clock;now=[100.]
        try:
            delivered.clear()
            with patch('display_frames.time.perf_counter',side_effect=lambda:now[0]):
                clock.eventFilter(clock.handle,QEvent(QEvent.UpdateRequest))
                now[0]+=.005;clock.eventFilter(clock.handle,QEvent(QEvent.UpdateRequest))
                now[0]+=.012;clock.eventFilter(clock.handle,QEvent(QEvent.UpdateRequest))
                self.assertEqual(len(delivered),3)
                _set_on_battery(True);now[0]+=.001
                clock.eventFilter(clock.handle,QEvent(QEvent.UpdateRequest))
                now[0]+=.005;clock.eventFilter(clock.handle,QEvent(QEvent.UpdateRequest))
                self.assertEqual(len(delivered),4)
                _set_on_battery(False);now[0]+=.001
                clock.eventFilter(clock.handle,QEvent(QEvent.UpdateRequest))
                self.assertEqual(len(delivered),5)
            clock._screen_changed(None);self.assertIsNone(clock.refresh_hz)
            clock._screen_changed(APP.primaryScreen());self.assertIsNotNone(clock.refresh_hz)
        finally:
            _set_on_battery(False);pulse.stop()

    def test_windows_power_notification_changes_battery_policy(self):
        import ctypes
        from ctypes import wintypes
        from PySide6.QtCore import QByteArray
        from display_frames import _PowerEvents
        message=wintypes.MSG();message.message=0x0218;message.wParam=0x000A
        with patch('display_frames._query_on_battery',return_value=True),patch('display_frames._set_on_battery') as update:
            self.assertEqual(_PowerEvents().nativeEventFilter(QByteArray(b'windows_generic_MSG'),ctypes.addressof(message)),(False,0))
            update.assert_called_once_with(True)

    def test_reparented_surface_leaves_the_previous_native_clock(self):
        child=QWidget(self.window);pulse=FramePulse(child);pulse.start();flush();old=pulse.clock
        other=QWidget();other.show();flush()
        try:
            child.setParent(other);pulse.start();flush()
            self.assertIsNot(pulse.clock,old);self.assertNotIn(pulse,old.pulses)
        finally:pulse.stop();other.deleteLater();flush()

    def test_deleted_qobject_is_not_called_from_an_outstanding_frame(self):
        child=QWidget(self.window);pulse=FramePulse(child);pulse.start();flush();clock=pulse.clock
        child.deleteLater();flush();self.assertFalse(isValid(pulse))
        self.assertFalse(clock.eventFilter(clock.handle,QEvent(QEvent.UpdateRequest)))
        self.assertNotIn(pulse,clock.pulses)

    def test_spring_uses_each_display_frame_at_60_120_144_and_240_hz(self):
        for hz in (60,120,144,240):
            with self.subTest(hz=hz),patch('native_ui_common.reduced_motion',return_value=False):
                samples=[];spring=Spring(self.window,{'x':0},lambda value:samples.append(value['x']))
                now=[100.0]
                with patch('flickdock_motion.time.perf_counter',side_effect=lambda:now[0]):
                    spring.animate({'x':100});flush();clock=spring._timer.clock
                    # Native startup may deliver a paint during flush; count
                    # only the deterministic display frames injected below.
                    samples.clear()
                    for frame in range(1,round(hz*.2)+1):
                        now[0]=100.0+frame/hz
                        clock.eventFilter(clock.handle,QEvent(QEvent.UpdateRequest))
                expected=round(hz*.2)
                self.assertEqual(len(samples),expected)
                self.assertEqual(len(set(samples)),expected)
                spring.stop();spring.deleteLater();flush()

    def test_morph_interpolation_uses_native_pulses_and_keeps_exact_endpoint(self):
        values=[];settled=[]
        morph=SurfaceMorph(self.window,QRect(0,0,100,100),lambda rect:values.append(QRect(rect)),settle=lambda rect:settled.append(QRect(rect)))
        target=QRect(0,0,600,400)
        with patch('native_ui_common.reduced_motion',return_value=False),patch('surface_morph.time.perf_counter',return_value=100.0):
            morph.animate(target,duration=360,elastic=True)
        flush();clock=morph._frames.clock
        self.assertEqual(morph.animation.state(),QAbstractAnimation.Stopped)
        for frame in range(1,ceil(240*morph.animation.duration()/1000)+1):
            with patch('surface_morph.time.perf_counter',return_value=100.0+frame/240):
                clock.eventFilter(clock.handle,QEvent(QEvent.UpdateRequest))
        self.assertGreater(len(set((rect.width(),rect.height()) for rect in values)),50)
        self.assertEqual(morph.current,target);self.assertEqual(morph.progress,1.0)
        self.assertEqual(settled,[target]);self.assertFalse(morph.active);self.assertFalse(morph._frames.isActive())

    def test_pause_and_resume_keep_the_same_morph_position(self):
        morph=SurfaceMorph(self.window,QRect(0,0,100,100),lambda rect:None)
        with patch('native_ui_common.reduced_motion',return_value=False):morph.animate(QRect(0,0,400,300))
        morph.pause();morph.animation.setCurrentTime(80);before=QRect(morph.current)
        self.assertFalse(morph._frames.isActive());morph.resume();self.assertEqual(morph.current,before)
        morph.complete();self.assertFalse(morph._frames.isActive())

    def test_occluded_window_still_settles_and_releases_its_surface(self):
        from PySide6.QtTest import QTest
        settled=[];target=QRect(0,0,400,300)
        morph=SurfaceMorph(self.window,QRect(0,0,100,100),lambda rect:None,settle=lambda rect:settled.append(QRect(rect)))
        with patch('native_ui_common.reduced_motion',return_value=False):morph.animate(target,duration=60)
        # AppKit may stop native display callbacks when another app covers the
        # shelf. This is not an intentional pause: the endpoint must still land.
        morph._frames.stop();QTest.qWait(120)
        self.assertFalse(morph.active);self.assertEqual(morph.current,target);self.assertEqual(settled,[target])

    def test_morph_deadline_respects_pause_and_interruption(self):
        from PySide6.QtTest import QTest
        settled=[];morph=SurfaceMorph(self.window,QRect(0,0,100,100),lambda rect:None,settle=lambda rect:settled.append(QRect(rect)))
        with patch('native_ui_common.reduced_motion',return_value=False):morph.animate(QRect(0,0,400,300),duration=60)
        morph.pause();QTest.qWait(100);self.assertTrue(morph.active);self.assertEqual(settled,[])
        morph.resume();morph._frames.stop()
        # Busy CI hosts can deliver a precise Qt timer late; verify that the
        # paused morph still settles instead of assuming a fixed scheduling lag.
        for _ in range(150):
            if not morph.active:break
            QTest.qWait(10)
        self.assertFalse(morph.active);self.assertEqual(settled,[QRect(0,0,400,300)])
        with patch('native_ui_common.reduced_motion',return_value=False):
            morph.animate(QRect(0,0,500,400),duration=40)
            morph.animate(QRect(0,0,600,500),duration=100)
        morph._frames.stop();QTest.qWait(65);self.assertTrue(morph.active)
        QTest.qWait(80);self.assertEqual(settled[-1],QRect(0,0,600,500))

    def test_reduced_motion_lands_without_a_native_frame_subscription(self):
        morph=SurfaceMorph(self.window,QRect(0,0,100,100),lambda rect:None)
        with patch('native_ui_common.reduced_motion',return_value=True):morph.animate(QRect(0,0,400,300))
        self.assertFalse(morph.active);self.assertFalse(morph._frames.isActive())

    def test_window_animation_interpolation_uses_every_native_frame_without_unified_timer(self):
        for hz in (60,75,120,144,240):
            with self.subTest(hz=hz):
                animation=DisplayAnimation(self.window);animation.setDuration(180)
                animation.setStartValue(0.);animation.setEndValue(1.)
                samples=[];done=[];animation.valueChanged.connect(samples.append);animation.finished.connect(lambda:done.append(True))
                now=[100.]
                with patch('display_frames.time.perf_counter',side_effect=lambda:now[0]):
                    animation.start();animation._frames.stop()
                    for frame in range(1,ceil(hz*.18)+1):now[0]=100.+frame/hz;animation._advance()
                self.assertEqual(animation._values.state(),QAbstractAnimation.Stopped)
                self.assertGreaterEqual(len(set(samples)),ceil(hz*.18))
                self.assertEqual(samples[-1],1.);self.assertEqual(done,[True])
                self.assertEqual(animation.state(),QAbstractAnimation.Stopped)
                animation.deleteLater();flush()

    def test_window_animation_pause_seek_and_reversal_keep_the_current_frame(self):
        animation=DisplayAnimation(self.window);animation.setDuration(240);animation.setStartValue(0.);animation.setEndValue(1.)
        animation.start();animation.pause();animation.setCurrentTime(80)
        value=animation.currentValue();animation.stop()
        self.assertEqual(animation.currentValue(),value)
        animation.setStartValue(value);animation.setEndValue(0.);animation.start()
        self.assertEqual(animation.currentValue(),value)
        animation.setCurrentTime(240);self.assertEqual(animation.currentValue(),0.)
        self.assertEqual(animation.state(),QAbstractAnimation.Stopped);self.assertFalse(animation._frames.isActive())
        animation.deleteLater();flush()

if __name__=='__main__':unittest.main()
