"""Public Python UI lifecycle plus one display clock; not native monitor FPS."""
import time,unittest
from unittest.mock import patch
from PySide6.QtCore import QObject,QEvent,QEventLoop,QTimer,QVariantAnimation as QtAnimation,QPoint,QPointF,QRectF,Qt
from PySide6.QtGui import QColor,QImage,QPainter
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid
from test_html_native_workspace import APP,flush
from frame_animation import FrameAnimation
from native_ui_common import ScreenAnimationTimer


class FrameAnimationTests(unittest.TestCase):
    def setUp(self):
        self.reduce=patch('frame_animation.reduced_motion',return_value=False);self.reduce.start();self.owned=[]
    def tearDown(self):
        for value in self.owned:
            if isValid(value):
                if isinstance(value,FrameAnimation):value.stop()
                value.deleteLater()
        flush();self.reduce.stop()
    def make(self,parent=None,cls=FrameAnimation):
        value=cls(parent);value.setDuration(100);value.setStartValue(0.);value.setEndValue(1.);self.owned.append(value);return value
    def widget(self,parent=None):
        widget=QWidget(parent);self.owned.append(widget);return widget

    def test_python_api_state_values_and_signals_match_qt_trace(self):
        def trace(cls):
            a=self.make(cls=cls);events=[];snapshots=[]
            a.finished.connect(lambda:events.append('finished'))
            a.stateChanged.connect(lambda state,old:events.append([old.name,state.name]))
            def snap(label):snapshots.append((label,a.state().name,a.currentTime(),a.currentValue(),list(events)))
            a.setCurrentTime(40);snap('stopped jump');a.start();snap('start')
            a.pause();a.setCurrentTime(100);snap('paused end');a.start();a.setCurrentTime(40)
            a.pause();a.start();snap('resume via start');a.stop();snap('stop')
            a.setLoopCount(2);a.start();a.setCurrentTime(150);snap('loop2');a.setCurrentTime(200);snap('loop2 end')
            a.setLoopCount(1);a.setDirection(a.Direction.Backward);a.start();snap('reverse start')
            a.setCurrentTime(30);snap('reverse jump');a.setCurrentTime(0);snap('reverse end')
            return snapshots
        self.assertEqual(trace(FrameAnimation),trace(QtAnimation))

    def test_direction_change_while_running_never_leaks_stopped_endpoint(self):
        for cls in (QtAnimation,FrameAnimation):
            a=self.make(cls=cls);values=[];a.valueChanged.connect(values.append)
            a.start();a.setCurrentTime(40);values.clear();a.setDirection(a.Direction.Backward)
            self.assertEqual(a.currentTime(),40);self.assertAlmostEqual(a.currentValue(),.4)
            self.assertEqual(values,[],'Direction switch must not paint an endpoint flash')
            a.setCurrentTime(0);self.assertEqual(a.state(),a.State.Stopped)

    def test_window_has_one_clock_for_many_animations_and_idle_clock_stops(self):
        owner=self.widget();owner.show();flush();animations=[self.make(owner) for _ in range(20)]
        for a in animations:a.start()
        clock=owner._flickdock_frame_clock
        self.assertEqual(len(clock.members),20);self.assertEqual(len(owner.findChildren(ScreenAnimationTimer)),1)
        for a in animations[:-1]:a.stop()
        self.assertTrue(clock.timer.isActive());self.assertEqual(len(clock.members),1)
        animations[-1].setCurrentTime(100)
        self.assertFalse(clock.timer.isActive());self.assertEqual(clock.members,{})

    def test_rapid_restart_cancels_old_completion_and_does_not_accumulate_slots(self):
        owner=self.widget();a=self.make(owner);finished=[];a.finished.connect(lambda:finished.append(True))
        for _ in range(200):
            a.start();a.setCurrentTime(20);a.stop();a.setStartValue(a.currentValue());a.start();a.stop()
        clock=owner._flickdock_frame_clock
        self.assertEqual(len(owner.findChildren(ScreenAnimationTimer)),1);self.assertFalse(clock.timer.isActive())
        self.assertEqual(finished,[]);a.start();a.setCurrentTime(100);a.setCurrentTime(100)
        self.assertEqual(finished,[True]);self.assertFalse(clock.members)

    def test_value_callback_restarts_without_completing_old_intent(self):
        a=self.make();finished=[];restarted=[];a.finished.connect(lambda:finished.append(True))
        def change(value):
            if value>=1. and not restarted:restarted.append(True);a.stop();a.start()
        a.valueChanged.connect(change);a.start();a.setCurrentTime(100)
        self.assertEqual(a.state(),a.State.Running);self.assertEqual(a.currentTime(),0);self.assertEqual(finished,[])
        a.setCurrentTime(100);self.assertEqual(finished,[True])

    def test_state_callback_restart_matches_qt_and_drops_stale_finished(self):
        for cls in (QtAnimation,FrameAnimation):
            a=self.make(cls=cls);restarted=[];finished=[];a.finished.connect(lambda:finished.append(True))
            def changed(state,old,a=a):
                if state==a.State.Stopped and not restarted:restarted.append(True);a.start()
            a.stateChanged.connect(changed);a.start();a.setCurrentTime(100)
            self.assertEqual(a.state(),a.State.Running);self.assertEqual(a.currentTime(),0);self.assertEqual(finished,[])
            a.setCurrentTime(100);self.assertEqual(finished,[True])

    def test_pause_resume_keeps_pose_and_finite_and_infinite_loops(self):
        a=self.make();a.start();a.setCurrentTime(35);a.pause();clock=a._driver
        a._advance(time.perf_counter()+10);self.assertEqual(a.currentTime(),35);self.assertFalse(clock.timer.isActive())
        a.resume();self.assertEqual(a.currentTime(),35);self.assertTrue(clock.timer.isActive())
        a.stop();a.setLoopCount(-1);a.start();a.setCurrentTime(250)
        self.assertEqual(a.currentLoop(),2);self.assertAlmostEqual(a.currentValue(),.5);self.assertEqual(a.state(),a.State.Running)
        a.stop();self.assertFalse(clock.timer.isActive())

    def test_hidden_anchor_and_destroyed_parent_release_timelines_immediately(self):
        owner=self.widget();child=self.widget(owner);owner.show();child.show();flush();a=self.make(child);a.start()
        clock=owner._flickdock_frame_clock;self.assertTrue(clock.timer.isActive())
        child.hide();flush();self.assertEqual(a.state(),a.State.Stopped);self.assertFalse(clock.timer.isActive())
        child.show();a.start();child.deleteLater();flush()
        self.assertFalse(isValid(a));self.assertFalse(clock.timer.isActive());self.assertFalse(clock.members)

    def test_delete_when_stopped_and_reduced_motion_finish_once_without_clock(self):
        owner=self.widget();a=self.make(owner);a.start(a.DeletionPolicy.DeleteWhenStopped);a.setCurrentTime(100);flush()
        self.assertFalse(isValid(a));b=self.make(owner);finished=[];b.finished.connect(lambda:finished.append(True))
        with patch('frame_animation.reduced_motion',return_value=True):b.start()
        self.assertEqual(b.currentValue(),1.);self.assertEqual(b.state(),b.State.Stopped);self.assertEqual(finished,[True])
        self.assertFalse(owner._flickdock_frame_clock.timer.isActive())

    def test_reduced_motion_changes_during_animation_settle_once(self):
        a=self.make();finished=[];a.finished.connect(lambda:finished.append(True));a.start();a.setCurrentTime(40)
        a._advance(time.perf_counter(),True);a._advance(time.perf_counter(),True)
        self.assertEqual(a.currentValue(),1.);self.assertEqual(finished,[True]);self.assertFalse(a._driver.timer.isActive())

    def test_zero_loops_and_duration_zero_match_native_completion(self):
        for cls in (QtAnimation,FrameAnimation):
            a=self.make(cls=cls);finished=[];a.finished.connect(lambda:finished.append(True))
            a.setLoopCount(0);a.start();self.assertEqual(a.state(),a.State.Stopped);self.assertEqual(finished,[])
            a.setLoopCount(1);a.setDuration(0);a.start();self.assertEqual(a.state(),a.State.Stopped);self.assertEqual(finished,[True])

    def test_real_wall_clock_completion_uses_display_driver_without_native_state(self):
        owner=self.widget();owner.show();flush();a=self.make(owner);a.setDuration(90);values=[];finished=[]
        a.valueChanged.connect(values.append);a.finished.connect(lambda:finished.append(True));a.start()
        self.assertEqual(QtAnimation.state(a),a.State.Stopped,'No hidden shared native animation timer')
        end=time.perf_counter()+.5
        while a.state()!=a.State.Stopped and time.perf_counter()<end:APP.processEvents();time.sleep(.002)
        self.assertEqual(a.currentValue(),1.);self.assertGreater(len(values),2);self.assertEqual(finished,[True])
        self.assertFalse(owner._flickdock_frame_clock.timer.isActive())

    def test_one_bad_timeline_reports_its_error_without_starving_other_timelines(self):
        owner=self.widget();bad=self.make(owner);healthy=self.make(owner);finished=[]
        healthy.finished.connect(lambda:finished.append(True));bad.start();healthy.start()
        clock=owner._flickdock_frame_clock
        with patch.object(bad,'_advance',side_effect=RuntimeError('synthetic animation fault')),patch('sys.excepthook') as diagnostic:
            clock._tick();clock._tick()
            self.assertEqual(diagnostic.call_count,1)
            self.assertEqual(bad.state(),bad.State.Stopped)
            self.assertEqual(healthy.state(),healthy.State.Running)
        healthy.setCurrentTime(100);self.assertEqual(finished,[True]);self.assertFalse(clock.timer.isActive())

    def test_sibling_resume_during_same_tick_never_rewinds_its_new_anchor(self):
        owner=self.widget();first=self.make(owner);second=self.make(owner)
        first.start();second.start();second.setCurrentTime(35);clock=owner._flickdock_frame_clock
        first._started=999.98
        def resume_sibling(_):second.pause();second.resume()
        first.valueChanged.connect(resume_sibling);values=[];second.valueChanged.connect(values.append)
        # One captured tick time precedes a later callback's resume anchor.
        with patch('frame_animation.time.perf_counter',side_effect=[1000.,1000.012]):clock._tick()
        self.assertEqual(second.currentTime(),35);self.assertAlmostEqual(second.currentValue(),.35)
        self.assertEqual(values,[],'A sibling cannot paint the previous timeline before its resume')
        first.stop();second.stop();self.assertFalse(clock.timer.isActive())

    def test_ignored_close_request_does_not_stop_live_window_animation(self):
        class KeepOpen(QWidget):
            def closeEvent(self,event):event.ignore()
        owner=KeepOpen();self.owned.append(owner);owner.show();flush();a=self.make(owner);a.start();a.setCurrentTime(35)
        owner.close();self.assertTrue(owner.isVisible());self.assertEqual(a.state(),a.State.Running)
        a.setCurrentTime(100);self.assertFalse(owner._flickdock_frame_clock.timer.isActive())

    def test_account_and_menu_opacity_migrations_interpolate_actual_properties(self):
        from account_panel import AuthStatusLabel
        from flickdock import AnimatedMenu,style_menu
        owner=self.widget();status=AuthStatusLabel(owner);status.setText('Actual status');status.resize(180,30)
        owner.show();status.show();flush();status._fade.pause();status._fade.setCurrentTime(45)
        self.assertGreater(status._opacity.opacity(),.2);self.assertLess(status._opacity.opacity(),1.)
        self.assertEqual(QtAnimation.state(status._fade),QtAnimation.Stopped)
        status._fade.setCurrentTime(180);self.assertEqual(status._opacity.opacity(),1.)
        self.assertFalse(owner._flickdock_frame_clock.timer.isActive())
        menu=AnimatedMenu();self.owned.append(menu);style_menu(menu);menu.addAction('Original operation')
        menu.popup(QPoint(40,40));flush();menu.fade.pause();menu.fade.setCurrentTime(35)
        self.assertGreater(menu.windowOpacity(),.25);self.assertLess(menu.windowOpacity(),1.)
        self.assertEqual(QtAnimation.state(menu.fade),QtAnimation.Stopped)
        menu.fade.setCurrentTime(120);self.assertEqual(menu.windowOpacity(),1.)
        self.assertFalse(menu._flickdock_frame_clock.timer.isActive());menu.hide()

    def test_category_ink_remains_readable_when_moving_surface_half_covers_text(self):
        from test_html_native_workspace import WorkspaceTests,composed_image,contrast_colors
        from reference_widgets import refresh_tree,palette,_category_ink_path
        import ui_theme,flickdock as subject
        fixture=WorkspaceTests();fixture.setUp();checked=0
        try:
            popup=fixture.overview();ref=fixture.paths[0]
            fixture.shelf.content.transfers[ref]=dict(phase='uploading',received=25,total=100)
            for theme in ('light','dark'):
                fixture.settings.update(theme=theme);ui_theme.set_theme(theme);ui_theme.apply_application(APP,subject.FONT_FAMILY)
                refresh_tree(fixture.shelf);popup._render_signature=None;popup.rebuild();popup.set_category('all');flush()
                indicator=popup.category_indicator;indicator.animation.stop();control=popup.category_buttons['uploading']
                badge=popup.nav_counts['uploading'];ratio=badge.devicePixelRatioF()
                for widget,is_badge in ((badge,True),(control,False)):
                    text=widget.text();mask=QImage(round(widget.width()*ratio),round(widget.height()*ratio),QImage.Format_ARGB32)
                    mask.setDevicePixelRatio(ratio);mask.fill(Qt.transparent);p=QPainter(mask);p.setFont(widget.font());p.setPen(Qt.white)
                    if is_badge:p.drawText(widget.rect(),Qt.AlignCenter,text)
                    else:p.drawText(QRectF(34,0,widget.width()-42,widget.height()),Qt.AlignLeft|Qt.AlignVCenter,text)
                    p.end();points=[(x,y) for y in range(mask.height()) for x in range(mask.width()) if mask.pixelColor(x,y).alpha()>230]
                    self.assertTrue(points)
                    for offset in (0,4,9,14,20,23,29,35):
                        rect=QRectF(control.geometry());rect.moveTop(control.y()-offset);indicator._frame(rect)
                        widget.setText('' if is_badge else ' ');background=composed_image(popup)
                        widget.setText(text);actual=composed_image(popup);at=widget.mapTo(popup,QPoint())
                        path=_category_ink_path(control,badge.pos() if is_badge else QPoint())
                        for x,y in points:
                            local=QPointF((x+.5)/ratio,(y+.5)/ratio)
                            # Contrast applies to solid surfaces, not the one
                            # antialiased boundary pixel of the moving clip.
                            sides=[path.contains(local+QPointF(dx,dy)) for dx,dy in ((-1,0),(1,0),(0,-1),(0,1))]
                            if any(sides) and not all(sides):continue
                            ink=QColor('#FFFFFF' if path.contains(local) else palette()['link'] if is_badge else palette()['secondary'])
                            sx=round(at.x()*ratio)+x;sy=round(at.y()*ratio)+y;base=background.pixelColor(sx,sy);pixel=actual.pixelColor(sx,sy)
                            self.assertGreaterEqual(contrast_colors(ink,base),4.5,(theme,is_badge,offset,ink.name(),base.name()))
                            self.assertLessEqual(max(abs(a-b) for a,b in zip(pixel.getRgb()[:3],ink.getRgb()[:3])),28,
                                                 (theme,is_badge,offset,local,pixel.name(),ink.name()))
                            checked+=1
            self.assertGreater(checked,100)
        finally:fixture.tearDown()
