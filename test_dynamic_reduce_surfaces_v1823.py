"""Remaining finite surface clocks: actual widgets, disposable files, no cursor IO."""
import os,tempfile,unittest
os.environ['QT_QPA_PLATFORM']='offscreen'
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch
from PySide6.QtCore import QPoint,QPointF,QRect,QRectF,Qt,QAbstractAnimation
from PySide6.QtWidgets import QWidget,QLabel,QPushButton,QScrollArea,QVBoxLayout
from test_html_native_workspace import APP,flush
import test_html_native_workspace as workspace
from transfer_drop_feedback import DropFeedback,ScaleFade
from transfer_item_motion import TransferItemMotion
from transfer_entry import TransferEntryRouter
from lan_panel import LanDialog,_ScanIndicator
from test_html_native_lan import FakeLan
from app_settings import SettingsStore
from reference_widgets import SortButton,IconTextButton
from account_panel import AuthStatusLabel,AccountPanel
from test_account_settings_v181 import Service
from item_transitions import ItemTransitions


class SurfaceReduceTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.reduced=False;self.windows=[]
        for name in ('native_ui_common.reduced_motion','lan_panel.reduced_motion','transfer_entry.reduced_motion'):
            self.stack.enter_context(patch(name,side_effect=lambda:self.reduced))
    def tearDown(self):
        for window in self.windows:window.hide();window.deleteLater()
        flush();self.stack.close()
    def host(self):
        widget=QWidget();widget.resize(440,420);self.windows.append(widget);widget.show();flush();return widget
    def stopped(self,animation):self.assertEqual(animation.state(),QAbstractAnimation.Stopped)

    def test_stack_spread_landing_and_parallel_delete_settle_without_data_or_reads(self):
        fixture=workspace.WorkspaceTests('runTest');fixture.setUp()
        try:
            tile=fixture.shelf.zones['permanent'].icon;tile.motion=True;tile.landing.stop();tile.arrival=1
            tile.set_spread(True);tile.spread_animation.setCurrentTime(35)
            self.reduced=True;tile.spread_animation.setCurrentTime(80);self.assertEqual(tile.spread,1);self.stopped(tile.spread_animation)
            self.reduced=False;tile.replay_landing();tile.landing.setCurrentTime(35)
            self.reduced=True;tile.landing.setCurrentTime(90);self.assertEqual(tile.arrival,1);self.stopped(tile.landing)
            self.reduced=False;refs=list(tile.refs);tile.set_stack(fixture.shelf.content,refs[1:]);tile.removal.setCurrentTime(35)
            self.reduced=True
            with patch('reference_widgets.thumbnail',side_effect=AssertionError('Reduce read thumbnail')),patch('reference_widgets.item_detail',side_effect=AssertionError('Reduce read metadata')):
                tile.removal.setCurrentTime(80);image=tile.grab().toImage()
            self.stopped(tile.removal);self.assertFalse(tile._mini_exit_cards);self.assertEqual(tile._exit_progress,1)
            self.assertEqual(list(tile.refs),refs[1:]);self.assertTrue(all(Path(path).exists() for path in fixture.paths))
            self.assertEqual(tile.grab().toImage(),image)
        finally:fixture.tearDown()

    def test_parabolic_finish_once_and_scale_effect_disabled_with_original_geometry(self):
        host=self.host();drop=DropFeedback(host);calls=[];row=QPushButton('Original target',host);row.setGeometry(20,20,160,40);row.show();effect=ScaleFade(row)
        drop.play(['file.zip'],QPoint(40,40),QPoint(180,180),lambda:calls.append(True));drop.animation.setCurrentTime(30)
        effect.arm();effect.animation.setCurrentTime(30);geometry=row.geometry()
        self.reduced=True;drop.animation.setCurrentTime(90);effect.animation.setCurrentTime(90)
        self.assertEqual(calls,[True]);self.stopped(drop.animation);self.assertIsNone(drop.pixmap);self.assertFalse(drop.isVisible())
        drop.animation.finished.emit();self.assertEqual(calls,[True]);self.stopped(effect.animation)
        self.assertFalse(effect.isEnabled());self.assertEqual(effect.value,1);self.assertEqual(row.geometry(),geometry)
        effect.arm();self.stopped(effect.animation);drop.play(['file.zip'],QPoint(),QPoint(),lambda:calls.append(True));self.assertEqual(calls,[True,True]);self.stopped(drop.animation)

    def test_transfer_removal_and_old_shelf_move_clean_visuals_keep_immediate_layout(self):
        host=self.host();scroll=QScrollArea(host);scroll.setGeometry(0,0,400,300);body=QWidget();body.resize(380,600);scroll.setWidget(body);scroll.show()
        row=QLabel('Survivor',body);row.setGeometry(0,100,200,40);row.show();flush()
        motion=TransferItemMotion(host,scroll.viewport());positions=motion.capture({'row':row});row.move(0,0);motion.reflow({'row':row},positions);flush();motion.animation.setCurrentTime(35)
        self.assertIsNotNone(row.graphicsEffect());self.reduced=True;motion.animation.setCurrentTime(90)
        self.assertEqual(row.pos(),QPoint());self.assertIsNone(row.graphicsEffect());self.stopped(motion.animation);self.assertFalse(motion.ghosts)
        self.reduced=False;old=ItemTransitions(scroll,body);old.moves={'row':(row,QPoint(),QPoint(0,80),0.)};old.timer.start();self.reduced=True;old._tick()
        self.assertEqual(row.pos(),QPoint(0,80));self.assertFalse(old.timer.isActive());self.assertFalse(old.moves)

    def test_queue_plane_scanner_and_chooser_clocks_follow_current_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            dialog=LanDialog(FakeLan(),SettingsStore(Path(directory)));self.windows.append(dialog);dialog.show();flush();plane=dialog.panel.queue_area
            plane.set_filled(True);plane.animation.setCurrentTime(35);self.reduced=True;plane.animation.setCurrentTime(90)
            self.stopped(plane.animation);self.assertEqual(plane.value,1)
            scanner=_ScanIndicator(dialog);scanner.show()
            with patch('lan_panel._motion_allowed',side_effect=lambda:not self.reduced):
                self.reduced=False;scanner.set_scanning(True);self.assertTrue(scanner._timer.isActive())
                self.reduced=True;scanner._advance();self.assertFalse(scanner._timer.isActive());self.assertTrue(scanner._scanning)
            host=self.host();button=QPushButton('Transfer',host);button.setGeometry(20,360,180,34);button.show();router=TransferEntryRouter(host,button,lambda *_:None)
            self.reduced=False;router._show();router.entrance.setCurrentTime(35);router.feedback.start();router.feedback.setCurrentTime(35)
            self.reduced=True;router.entrance.setCurrentTime(90);router.feedback.setCurrentTime(90)
            self.stopped(router.entrance);self.stopped(router.feedback);self.assertEqual(router.surface.enter,1);self.assertEqual(router.surface.press,0)

    def test_icon_and_sort_feedback_land_latest_visual_target_without_changing_sort(self):
        host=self.host();icon=IconTextButton('Button',host);icon.show();sort=SortButton('Size',host);sort.move(0,70);sort.show();flush()
        icon._retarget_icon(hover=1);icon._icon_animation.setCurrentTime(35);sort.set_direction(1);sort._sort_animation.setCurrentTime(30);sort.set_direction(2)
        self.reduced=True;icon._icon_animation.setCurrentTime(80);sort._sort_animation.setCurrentTime(80)
        self.stopped(icon._icon_animation);self.stopped(sort._sort_animation)
        self.assertEqual(icon._icon_value,QPointF(1,0));self.assertEqual(sort._sort_value,QPointF(0,1));self.assertEqual(sort.direction,2)

    def test_status_and_short_feedback_fades_stop_once_without_hiding_new_feedback(self):
        host=self.host();status=AuthStatusLabel(host);status.setText('Actual message');status.setGeometry(0,0,260,30);status.show();flush();status._fade.setCurrentTime(35)
        self.reduced=True;status._fade.setCurrentTime(80);self.stopped(status._fade);self.assertEqual(status._opacity.opacity(),1)
        self.reduced=False;panel=AccountPanel(Service());self.windows.append(panel);panel.show();flush();label=panel.status_label
        label.setText('First');panel._transient_feedback(label);panel._feedback_timers[label].timeout.emit();old=panel._feedback_animations[label];old.setCurrentTime(35)
        label.setText('Second');panel._transient_feedback(label);self.stopped(old);self.assertEqual(label.text(),'Second')
        panel._feedback_timers[label].timeout.emit();animation=panel._feedback_animations[label]
        self.reduced=True;animation.setCurrentTime(80);self.stopped(animation);self.assertFalse(label.isVisible());self.assertEqual(label.text(),'')
        animation.finished.emit();self.assertNotIn(label,panel._feedback_animations)

    def test_expiring_feedback_cannot_clear_a_new_error_written_to_same_label(self):
        panel=AccountPanel(Service());self.windows.append(panel);panel.show();flush();label=panel.status_label
        label.setText('Old success');label.show();panel._transient_feedback(label);panel._feedback_timers[label].timeout.emit()
        animation=panel._feedback_animations[label];animation.setCurrentTime(35)
        label.setText('New real error');self.reduced=True;animation.setCurrentTime(80)
        self.assertEqual(label.text(),'New real error');self.assertTrue(label.isVisible());self.assertEqual(label.graphicsEffect().opacity(),1)
        self.stopped(animation);self.assertNotIn(label,panel._feedback_animations)

    def test_display_accordion_lands_newest_expansion_and_keeps_draft(self):
        from test_display_groups_v1821 import DisplayTests
        fixture=DisplayTests('runTest');fixture.setUp()
        try:
            group=fixture.editor.rows[next(iter(fixture.editor.rows))]
            with patch.object(group,'_motion_enabled',side_effect=lambda:not self.reduced):
                before=group.profile();group.set_expanded(False);group.animation.setCurrentTime(35);group.set_expanded(True)
                self.reduced=True;group.animation.setCurrentTime(80);self.stopped(group.animation)
                self.assertTrue(group.expanded);self.assertTrue(group.details.isVisible());self.assertEqual(group.details.maximumHeight(),16777215);self.assertEqual(group.profile(),before)
        finally:fixture.tearDown()

    def test_clipboard_popup_reduce_and_finished_reopen_keep_new_anchors_and_pixels(self):
        from test_clipboard_popup_motion_v1823 import ClipboardPopupTests
        fixture=ClipboardPopupTests();fixture.setUp()
        try:
            with patch('clipboard_popup_motion.motion_allowed',side_effect=lambda:not self.reduced):
                source,dialog,motion=fixture.open();motion.animation.setCurrentTime(35);self.reduced=True;motion.animation.setCurrentTime(80)
                self.stopped(motion.animation);self.assertTrue(dialog.surface.isVisible());self.assertTrue(motion.source_image.isNull())
                self.reduced=False
                def reopen(_):dialog.finished.disconnect(reopen);dialog.open_from(source)
                dialog.finished.connect(reopen);dialog.dismiss();fixture.land(motion)
                self.assertTrue(dialog.isVisible());self.assertIs(motion.source,source);self.assertIs(dialog.owner_window,fixture.shelf)
                self.assertTrue(motion.presenting);self.assertFalse(motion.target_image.isNull())
                self.reduced=True;motion.animation.setCurrentTime(80);self.assertTrue(dialog.surface.isVisible());self.stopped(motion.animation)
        finally:fixture.tearDown()


if __name__=='__main__':unittest.main()
