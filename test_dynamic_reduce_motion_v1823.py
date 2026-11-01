"""Live Qt clocks/pixels at an OS-preference boundary; no desktop changes."""
import os,unittest
os.environ['QT_QPA_PLATFORM']='offscreen'
from unittest.mock import patch
from types import SimpleNamespace
from PySide6.QtCore import QAbstractAnimation,QPoint,QRect,QRectF,Qt
from PySide6.QtWidgets import QWidget,QDialog,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QButtonGroup,QLineEdit
from PySide6.QtTest import QTest
from test_html_native_workspace import APP,flush
from native_ui_common import UiSwitch
from app_settings import SettingsToggleSwitch
from input_focus import install_focus
from ui_transitions import WindowFade,SegmentIndicator,ToolbarEntrance,DwellHint,SnapshotFade,close_snapshot
from intake_notice import ClipboardNotice
from flickdock_motion import Spring
from surface_morph import SurfaceMorph


class DynamicReduceTests(unittest.TestCase):
    def setUp(self):
        self.reduced=False;self.windows=[]
        preference=patch('native_ui_common.reduced_motion',side_effect=lambda:self.reduced)
        preference.start();self.addCleanup(preference.stop)
    def tearDown(self):
        for window in self.windows:window.hide();window.deleteLater()
        flush()
    def host(self,dialog=False):
        host=QDialog() if dialog else QWidget();host.resize(420,240);self.windows.append(host);return host
    def stopped(self,animation):self.assertEqual(animation.state(),QAbstractAnimation.Stopped)

    def test_switches_settle_latest_check_without_extra_business_signal_and_pixels_stop(self):
        host=self.host();layout=QVBoxLayout(host);settings=SettingsToggleSwitch('Setting');native=UiSwitch()
        layout.addWidget(settings);layout.addWidget(native);host.show();flush();signals=[];settings.toggled.connect(signals.append)
        settings.setChecked(True);native.setChecked(True)
        settings._toggle_motion.animation.setCurrentTime(40);native._motion.setCurrentTime(40)
        self.assertLess(settings._toggle_motion.value,1);self.assertLess(native._position,1)
        settings.setChecked(False);settings._toggle_motion.animation.setCurrentTime(30)
        self.reduced=True;settings._toggle_motion.animation.setCurrentTime(70);native._motion.setCurrentTime(70)
        self.assertEqual(signals,[True,False]);self.assertEqual(settings._toggle_motion.value,0);self.assertEqual(native._position,1)
        self.stopped(settings._toggle_motion.animation);self.stopped(native._motion)
        image=host.grab().toImage();QTest.qWait(25);self.assertEqual(host.grab().toImage(),image)
        settings.setChecked(True);native.setChecked(False)
        self.assertEqual(settings._toggle_motion.value,1);self.assertEqual(native._position,0)
        self.stopped(settings._toggle_motion.animation);self.stopped(native._motion)

    def test_focus_and_segment_land_at_current_intent_without_changing_text_or_selection(self):
        host=self.host();layout=QVBoxLayout(host);edit=QLineEdit('kept input');layout.addWidget(edit);focus=install_focus(edit)
        bar=QWidget(host);row=QHBoxLayout(bar);buttons=[QPushButton(name,bar) for name in ('first','second')]
        group=QButtonGroup(bar)
        for button in buttons:button.setCheckable(True);group.addButton(button);row.addWidget(button)
        buttons[0].setChecked(True);layout.addWidget(bar);indicator=SegmentIndicator(bar,buttons);host.show();flush()
        focus._target(1.,True);focus.animation.setCurrentTime(35);focus._target(0.,True)
        buttons[1].setChecked(True);flush();indicator.animation.setCurrentTime(40)
        self.reduced=True;focus.animation.setCurrentTime(75);indicator.animation.setCurrentTime(75)
        self.stopped(focus.animation);self.stopped(indicator.animation)
        self.assertEqual(focus.value,0);self.assertEqual(edit.text(),'kept input')
        self.assertTrue(buttons[1].isChecked());self.assertEqual(indicator._rect,QRectF(buttons[1].geometry()))

    def test_popup_entry_removes_transient_effect_and_close_callback_executes_once(self):
        window=self.host(True);QVBoxLayout(window).addWidget(QLabel('Popup content'));fade=WindowFade(window)
        with patch.object(fade,'enabled',side_effect=lambda:not self.reduced):
            window.show();fade.animation.setCurrentTime(35);self.assertIsNotNone(window.graphicsEffect())
            self.reduced=True;fade.animation.setCurrentTime(80)
            self.assertIsNone(window.graphicsEffect());self.stopped(fade.animation);self.assertEqual((fade.alpha,fade.scale),(1,1))
            self.reduced=False;calls=[]
            self.assertTrue(fade.defer_done(None,lambda _: (calls.append('closed'),window.hide())))
            fade.animation.setCurrentTime(35);self.reduced=True;fade.animation.setCurrentTime(80)
            self.assertEqual(calls,['closed']);self.assertFalse(window.isVisible());self.assertIsNone(window.graphicsEffect())
            fade.animation.finished.emit();self.assertEqual(calls,['closed'])

    def test_reopened_popup_does_not_execute_obsolete_close_when_reduction_changes(self):
        window=self.host(True);fade=WindowFade(window);calls=[]
        with patch.object(fade,'enabled',side_effect=lambda:not self.reduced):
            window.show();fade.animation.setCurrentTime(180)
            fade.defer_done(None,lambda _:calls.append('obsolete'));fade.animation.setCurrentTime(50)
            pose=(fade.alpha,fade.scale);fade.reopen();self.assertEqual((fade.alpha,fade.scale),pose)
            self.reduced=True;fade.animation.setCurrentTime(80)
            self.stopped(fade.animation);self.assertEqual(calls,[]);self.assertTrue(window.isVisible());self.assertIsNone(window.graphicsEffect())

    def test_close_callback_reopening_same_window_keeps_new_entry_pose_and_effect(self):
        window=self.host(True);QVBoxLayout(window).addWidget(QLabel('Reopened popup'));fade=WindowFade(window);calls=[]
        with patch.object(fade,'enabled',side_effect=lambda:not self.reduced):
            window.show();fade.animation.setCurrentTime(180)
            def reopened(_):calls.append(True);window.hide();window.show()
            fade.defer_done(None,reopened);fade.animation.setCurrentTime(180)
            self.assertEqual(calls,[True]);self.assertTrue(window.isVisible())
            self.assertIsNotNone(window.graphicsEffect());self.assertEqual((fade.alpha,fade.scale),(.78,.97))
            opacity=window.windowOpacity();fade.animation.setCurrentTime(50)
            self.assertGreaterEqual(window.windowOpacity(),opacity)
            fade.animation.setCurrentTime(180);self.assertIsNone(window.graphicsEffect());self.assertEqual(calls,[True])

    def test_notice_lands_overlap_positions_and_finishes_ghost_once(self):
        manager=SimpleNamespace(settings=SimpleNamespace(theme='light',clipboard_success_notice=True),_quitting=False,_clipboard_runtime_enabled=lambda:True)
        notice=ClipboardNotice(manager);self.windows.append(notice)
        for value in ('one','two'):notice.show_result(True,dict(id=value,value=('text',{'text':value}),point=(10,10)))
        notice.animation.setCurrentTime(35);self.reduced=True;notice.animation.setCurrentTime(80)
        self.assertEqual([(e['y'],e['alpha']) for e in notice.motion.active],[(0,1),(16,1)])
        self.stopped(notice.animation);image=notice.grab().toImage();QTest.qWait(25);self.assertEqual(notice.grab().toImage(),image)
        self.reduced=False;notice.dismiss();self.assertEqual(len(notice.motion.ghosts),1)
        self.reduced=True;notice.animation.setCurrentTime(75)
        self.assertFalse(notice.isVisible());self.assertFalse(notice.motion.ghosts);self.stopped(notice.animation)
        notice.animation.finished.emit();self.assertFalse(notice.isVisible());self.assertFalse(notice.motion.active)

    def test_spring_settles_once_and_ignores_a_late_tick_or_old_reentrant_close(self):
        host=self.host();updates=[];calls=[];spring=Spring(host,dict(x=0.,alpha=0.),updates.append)
        spring.animate(dict(x=40.,alpha=1.),lambda:calls.append('done'))
        self.reduced=True;spring._tick();self.assertEqual(spring.values,dict(x=40.,alpha=1.))
        self.assertEqual(calls,['done']);self.assertFalse(spring._timer.isActive());count=len(updates)
        spring._tick();self.assertEqual(calls,['done']);self.assertEqual(len(updates),count)
        self.reduced=False
        def reopen(values):
            if values['x']==60.:spring.animate(dict(x=90.),lambda:calls.append('new'))
        spring._on_update=reopen;spring.animate(dict(x=60.),lambda:calls.append('old-close'))
        self.reduced=True;spring._tick();self.assertNotIn('old-close',calls);self.assertIn('new',calls)
        self.assertEqual(spring.values['x'],90.);self.assertFalse(spring._timer.isActive())
        spring.kick(dict(x=100.));self.assertFalse(spring._timer.isActive())

    def test_surface_morph_current_target_settles_once_without_changing_native_owner(self):
        host=self.host();host.show();flush();geometry=host.geometry();updates=[];settles=[];done=[]
        morph=SurfaceMorph(host,QRect(0,0,240,160),updates.append,settle=settles.append)
        morph.animate(QRect(20,20,400,240),lambda:done.append('old'));morph.animation.setCurrentTime(40)
        morph.animate(QRect(10,10,260,180),lambda:done.append('current'))
        self.reduced=True;morph.animation.setCurrentTime(75)
        self.assertEqual(morph.current,QRect(10,10,260,180));self.assertEqual(done,['current'])
        self.assertEqual(len(settles),1);self.stopped(morph.animation);self.assertEqual(host.geometry(),geometry)
        morph.animation.finished.emit();self.assertEqual(done,['current'])

    def test_surface_reentrant_new_target_cancels_old_finish_and_settle(self):
        host=self.host();settles=[];done=[];old=QRect(10,10,400,240);new=QRect(5,5,300,180)
        def update(rect):
            if rect==old:morph.animate(new,lambda:done.append('new'))
        morph=SurfaceMorph(host,QRect(0,0,240,160),update,settle=settles.append)
        morph.animate(old,lambda:done.append('old'))
        self.reduced=True;morph.animation.setCurrentTime(75)
        self.assertEqual(morph.current,new);self.assertEqual(done,['new']);self.assertEqual(settles,[new]);self.stopped(morph.animation)

    def test_toolbar_dwell_and_snapshot_ghost_clocks_all_stop(self):
        host=self.host();layout=QVBoxLayout(host);button=QPushButton('Button');layout.addWidget(button);host.show();flush()
        entrance=ToolbarEntrance(host,lambda:[button]);entrance.reveal(reset=True);entrance.animation.setCurrentTime(30)
        hint=DwellHint(button);hint.wait.stop();hint._start();hint.animation.setCurrentTime(70)
        page=SnapshotFade(host);mutations=[];page.change(lambda:mutations.append(True));page.animation.setCurrentTime(35)
        child=QWidget(host);child.setGeometry(10,10,120,50);child.show();close_snapshot(child);ghost=child._closing_snapshot;child.hide()
        self.reduced=True;entrance.animation.setCurrentTime(80);hint.animation.setCurrentTime(100);page.animation.setCurrentTime(80);ghost.animation.setCurrentTime(80)
        for animation in (entrance.animation,hint.animation,page.animation,ghost.animation):self.stopped(animation)
        self.assertEqual(button._entry_dx,0);self.assertEqual(button._entry_alpha,1);self.assertEqual(hint.value,0)
        self.assertFalse(page.isVisible());self.assertIsNone(page.snapshot);self.assertEqual(mutations,[True])
        self.assertFalse(ghost.isVisible());self.assertIsNone(ghost.snapshot)


class PreferenceCacheTests(unittest.TestCase):
    def test_shared_os_read_expires_within_four_ms_and_forced_preference_is_immediate(self):
        import native_ui_common as native
        with patch.dict(os.environ,{'FLICKDOCK_REDUCE_MOTION':'0','FLICKDOCK_LOW_PERFORMANCE':'0'}),patch.object(native,'_motion_preference_cache',(None,0.,False)),patch.object(native,'perf_counter',side_effect=[10.,10.002,10.005]),patch.object(native,'_read_reduced_motion',side_effect=[False,True]) as query:
            self.assertFalse(native.reduced_motion());self.assertFalse(native.reduced_motion());self.assertTrue(native.reduced_motion())
            self.assertEqual(query.call_count,2)
            with patch.dict(os.environ,{'FLICKDOCK_REDUCE_MOTION':'1'}):self.assertTrue(native.reduced_motion())
            self.assertEqual(query.call_count,2)


if __name__=='__main__':unittest.main()
