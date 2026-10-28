"""Real Qt entry paths, isolated profiles and no native hooks or desktop IO."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QPoint,QPointF,QRect,QRectF,Qt,QEvent
from PySide6.QtGui import QImage,QPainter,QColor,QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget,QSlider,QStyle,QStyleOptionSlider
from test_html_native_workspace import APP,flush,until
import test_html_native_workspace as workspace_fixture
from test_account_settings_v181 import Service
from account_panel import AccountPanel
import app_settings
from surface_morph import SurfaceSnapshot


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.fixture=workspace_fixture.WorkspaceTests();self.fixture.setUp();self.shelf=self.fixture.shelf
    def tearDown(self):self.fixture.tearDown()

    def test_total_count_stays_total_and_empty_category_badges_have_no_capsule(self):
        view=self.fixture.overview();total=len(self.shelf.display_refs())
        for category in ('images','files','all'):
            view.set_category(category);flush();self.assertEqual(view.count_label.text(),f'{total} 项')
        for key,badge in view.nav_counts.items():
            self.assertEqual(not badge.isHidden(),bool(badge.text()),key)

    def test_checkbox_full_target_paste_hidden_and_deselect_is_red(self):
        view=self.fixture.overview();row=view.rows_body.findChildren(__import__('flickdock').FileRow)[0]
        QTest.mouseClick(row.selection_check,Qt.LeftButton,pos=QPoint(2,2));flush()
        self.assertIn(row.path,view.selected);self.assertEqual(row.selection_check.size().width(),40)
        self.assertTrue(view.paste_button.isHidden())
        QTest.mouseClick(view.select_all_button,Qt.LeftButton);flush()
        self.assertEqual(view.select_all_button.text(),'弃选');self.assertIn('#FF453A',view.select_all_button.styleSheet())

    def test_blank_space_marquee_selects_actual_files_in_both_modes(self):
        view=self.fixture.overview()
        for mode in ('list','icons'):
            view.change_view_mode(mode);view.resume_content();flush()
            until(lambda:view._build_cursor==len(view.visible_refs()) and view.retirement.is_idle)
            cells=[child for child in view.rows_body.findChildren(QWidget,options=Qt.FindDirectChildrenOnly) if hasattr(child,'path') and child.isVisible()]
            start=QPoint(3,max(cell.geometry().bottom() for cell in cells)+15)
            end=QPoint(view.rows_body.width()-3,min(cell.geometry().top() for cell in cells))
            QTest.mousePress(view.rows_body,Qt.LeftButton,pos=start)
            QTest.mouseMove(view.rows_body,end);QTest.mouseRelease(view.rows_body,Qt.LeftButton,pos=end);flush()
            self.assertEqual(view.selected,set(view.visible_refs()),mode)
            self.assertFalse(view.marquee.band.isVisible())

    def test_compact_footer_logo_cloud_and_hover_do_not_open_workspace(self):
        self.shelf.presentation_kind='drag';self.shelf._layout_header(self.shelf.card.width());flush()
        zone=self.shelf.zones['permanent']
        self.assertTrue(self.shelf.brand_logo.isVisible());self.assertTrue(self.shelf.cloud_button.isVisible())
        self.assertTrue(self.shelf.account_button.isHidden());self.assertTrue(self.shelf.lan_button.isHidden())
        self.assertAlmostEqual(zone.caption.geometry().center().x(),zone.rect().center().x(),delta=1)
        self.assertLess(zone.caption.geometry().bottom(),zone.height()-20)
        zone._track_pill_hover(zone.caption.geometry().center());QTest.qWait(330)
        self.assertFalse(self.shelf.expanded)
        for widget in (self.shelf.pin_button,self.shelf.menu_button,self.shelf.expand_button):self.assertTrue(widget.isHidden())

    def test_foreign_drag_mouse_events_cannot_start_nested_drag_or_open_overview(self):
        zone=self.shelf.zones['permanent']
        event=QMouseEvent(QEvent.MouseMove,QPointF(80,80),QPointF(80,80),Qt.NoButton,Qt.LeftButton,Qt.NoModifier)
        release=QMouseEvent(QEvent.MouseButtonRelease,QPointF(80,80),QPointF(80,80),Qt.LeftButton,Qt.NoButton,Qt.NoModifier)
        with patch.object(self.shelf,'drag_files') as drag,patch.object(self.shelf,'show_contents') as opened:
            zone.mouseMoveEvent(event);zone.mouseReleaseEvent(release)
            drag.assert_not_called();opened.assert_not_called()

    def test_toolbar_edge_feedback_and_activation_use_identical_targets(self):
        QTest.mouseClick(self.shelf.expand_button,Qt.LeftButton,pos=QPoint(1,1));flush()
        self.assertTrue(self.shelf.expanded)
        QTest.mouseClick(self.shelf.expand_button,Qt.LeftButton,pos=QPoint(30,30));flush()
        self.assertFalse(self.shelf.expanded)
        before=self.shelf.pinned
        self.shelf.pin_button.setChecked(before)
        QTest.mouseClick(self.shelf.pin_button,Qt.LeftButton,pos=QPoint(1,1));flush()
        self.assertNotEqual(self.shelf.pinned,before)
        for control in (self.shelf.pin_button,self.shelf.expand_button,self.shelf.menu_button,self.shelf.cloud_button,self.shelf.close_button):
            for point in (QPoint(1,1),control.rect().bottomRight()-QPoint(1,1)):
                self.assertTrue(control.hitButton(point))

    def test_passive_mac_reveal_skips_qt_application_activation_and_hide_keeps_origin(self):
        import flickdock
        with patch.object(flickdock.sys,'platform','darwin'),patch.object(self.shelf,'raise_') as qt_raise,patch.object(flickdock,'raise_shelf_for_reveal',return_value=True) as native:
            self.shelf._raise_for_reveal();native.assert_called_once();qt_raise.assert_not_called()
        previous=self.shelf.visual_geometry().topLeft();self.shelf.hide_immediately()
        self.assertEqual(self.shelf.visual_geometry().topLeft(),previous)

    def test_compact_transfer_strip_entire_surface_opens_transfers(self):
        view=self.fixture.overview()
        for ref in self.fixture.paths:self.shelf.content.transfers[ref]=dict(phase='uploading',total=100,received=64)
        view.update_transfer_status();flush()
        self.assertEqual(len(view.strip_rows),1);self.assertEqual(view.strip_rows[0][2].text(),'64%')
        self.assertIn('+2',view.strip_more.text())
        QTest.mouseClick(view.transfer_strip,Qt.LeftButton,pos=QPoint(2,2));flush()
        self.assertEqual(view.category,'uploading')

    def test_dock_drop_keeps_preview_until_pointer_leaves(self):
        self.shelf.pinned=False;self.shelf.presentation_kind='dock';self.shelf.incoming_drop=True
        self.shelf._finish_intake()
        self.assertTrue(self.shelf.isVisible());self.assertFalse(self.shelf.incoming_drop)
        self.assertEqual(self.shelf.visible_state,'floating')

    def test_rounded_crossfade_keeps_constant_opaque_material(self):
        snapshot=SurfaceSnapshot(self.shelf)
        def endpoint(w,h):
            image=QImage(w,h,QImage.Format_ARGB32_Premultiplied);image.fill(Qt.transparent)
            p=QPainter(image);p.setRenderHint(QPainter.Antialiasing);p.setPen(Qt.NoPen);p.setBrush(QColor('#1c1c1e'))
            p.drawRoundedRect(QRectF(0,0,w,h),10,10);p.end()
            from PySide6.QtGui import QPixmap
            return QPixmap.fromImage(image)
        snapshot.source=endpoint(100,80);snapshot.target=endpoint(400,260)
        for mix in (0,.1,.25,.5,.75,.95,1):
            image=QImage(400,260,QImage.Format_ARGB32_Premultiplied);image.fill(Qt.transparent)
            snapshot.mix=mix;p=QPainter(image);snapshot.paint_frame(p,QRectF(0,0,400,260));p.end()
            self.assertEqual(image.pixelColor(3,12).alpha(),255,mix)
            self.assertEqual(image.pixelColor(200,130).alpha(),255,mix)
            self.assertLess(image.pixelColor(0,0).alpha(),10,mix)
        snapshot.deleteLater()


class SettingsFeedbackTests(unittest.TestCase):
    def test_completed_lan_transfer_releases_active_area_into_history(self):
        from test_html_native_lan import FakeLan
        from lan_panel import LanDialog
        with tempfile.TemporaryDirectory(prefix='flickdock-lan-finished-') as root:
            dialog=LanDialog(FakeLan(),app_settings.SettingsStore(Path(root)));dialog.show();flush();panel=dialog.panel
            panel._progress(dict(id='finished',direction='send',state='transferring',received=25,total=100,peer='Roger',name='设计稿.zip'))
            self.assertTrue(panel._rows['finished']['bar'].isVisible())
            panel._progress(dict(id='finished',direction='send',state='complete',received=100,total=100,peer='Roger',name='设计稿.zip'));flush()
            self.assertTrue(panel._rows['finished']['bar'].isHidden());self.assertTrue(panel._rows['finished']['frame'].isHidden())
            self.assertTrue(panel.transfers_empty.isVisible());self.assertEqual(panel._history[0]['state'],'完成')
            panel._progress(dict(id='interrupted',direction='receive',state='interrupted',received=25,total=100,peer='Mac',name='素材.zip'));flush()
            self.assertTrue(panel._rows['interrupted']['frame'].isHidden());self.assertEqual(panel._history[0]['state'],'已中断')
            panel._progress(dict(id='interrupted',direction='receive',state='reconnecting',peer='Mac',name='素材.zip'));flush()
            self.assertTrue(panel._rows['interrupted']['frame'].isVisible());self.assertFalse(any(x['state']=='已中断' for x in panel._history))
            seen=[];dialog.overview_requested.connect(lambda:seen.append('overview'))
            QTest.mouseClick(dialog.close_button,Qt.LeftButton,pos=QPoint(1,1));flush()
            self.assertFalse(dialog.isVisible());self.assertFalse(seen)
            dialog.close();dialog.deleteLater();flush()

    def test_async_login_devices_error_dialog_scroll_end_and_slider_handles(self):
        with tempfile.TemporaryDirectory(prefix='flickdock-settings-1815-') as root,patch.object(app_settings,'_startup_runtime_allowed',return_value=False):
            dialog=app_settings.SettingsDialog(app_settings.SettingsStore(Path(root)))
            service=Service();service.signed_in=False;service.account=None
            panel=AccountPanel(service);dialog.set_account_panel(panel);dialog.show();flush()
            panel.show_auth();flush();panel._status_changed('账号登录失败，请检查账号和密码。'*3);flush()
            button=panel.submit_button
            self.assertTrue(panel.auth_dialog.surface.rect().contains(button.mapTo(panel.auth_dialog.surface,button.rect().bottomRight())))
            service.account={'id':'async-login','username':'Roger'};service.signed_in=True
            service.account_changed.emit(service.account)
            service.devices.extend([dict(id='three',name='办公室电脑',platform='windows'),dict(id='four',name='MacBook Air',platform='macos')])
            service.list_devices()
            for _ in range(8):flush()
            self.assertEqual(len(panel._rows_by_id),4)
            for row in panel._rows_by_id.values():
                for ancestor in (panel.devices_body,panel.signed_in_page,panel,dialog.account_slot):
                    self.assertTrue(ancestor.rect().contains(row.mapTo(ancestor,row.rect().bottomRight())),(ancestor.objectName(),row.geometry(),ancestor.size()))
            dialog.set_update_state('checking');QTest.qWait(12)
            self.assertTrue(dialog.update_activity.isVisible());dialog.set_update_state('latest');flush()
            self.assertTrue(dialog.update_activity.isHidden());self.assertFalse(dialog.update_activity._clock.isValid())
            dialog.set_update_state('checking');self.assertLess(dialog.update_activity._clock.elapsed(),10)
            dialog.set_update_state('latest')
            scroll=dialog.scroll.verticalScrollBar();scroll.setValue(scroll.maximum());flush()
            last=dialog.body_layout.itemAt(dialog.body_layout.count()-1).widget()
            self.assertIsNotNone(last);self.assertLessEqual(dialog.body.height()-last.geometry().bottom(),32)
            for slider in dialog.findChildren(QSlider):
                self.assertGreaterEqual(slider.height(),24)
                for value in (slider.minimum(),slider.maximum()):
                    slider.setValue(value);option=QStyleOptionSlider();slider.initStyleOption(option)
                    handle=slider.style().subControlRect(QStyle.CC_Slider,option,QStyle.SC_SliderHandle,slider)
                    self.assertTrue(slider.rect().contains(handle),(slider.rect(),handle))
            dialog.close();dialog.deleteLater();flush()


if __name__=='__main__':unittest.main(verbosity=2)
