"""Real UI entry regressions for responsive settings and in-place navigation."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from PySide6.QtCore import QEvent,QPoint,QRect,Qt,QObject
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QToolTip
from test_html_native_workspace import APP,flush,until
import test_html_native_workspace as _workspace
from test_account_settings_v181 import Service
from account_panel import AccountPanel
import account_panel
from lan_panel import _CopyCodeButton,LanDialog
from lan_transfer import LanTransferService
from shelf_sync import ShelfSync
import flickdock,app_settings,reference_widgets

class FeedbackTests(unittest.TestCase):
    def test_wrapped_profile_height_does_not_reuse_old_fixed_height_at_a_previous_width(self):
        # The Mac font/layout branch reproduces Qt's stale 44px HFW allocation
        # after cycling widths, even on the offscreen Windows backend.
        with tempfile.TemporaryDirectory() as directory,patch.object(app_settings,'_startup_runtime_allowed',return_value=False),patch.object(account_panel.sys,'platform','darwin'):
            from native_ui_common import configure_reference_font
            configure_reference_font(APP)
            dialog=app_settings.SettingsDialog(app_settings.SettingsStore(Path(directory)))
            service=Service();service.account=dict(id='fixture',username='Roger',login_alias='id:legacy-account-identity')
            panel=AccountPanel(service);dialog.set_account_panel(panel)
            panel.set_sync_status('可同步内容已同步；1 项暂留本机，请检查文件大小或原文件所在位置。')
            dialog.show();flush()
            try:
                for width in (892,740,1024,892,1024):
                    dialog.resize(width,632);flush()
                    bottom=panel.profile_frame.mapTo(panel.signed_in_page,panel.profile_frame.rect().bottomRight()).y()
                    top=panel.login_alias_edit.mapTo(panel.signed_in_page,QPoint()).y()
                    self.assertGreaterEqual(top-bottom,12,(width,top,bottom))
            finally:dialog.close();dialog.deleteLater();flush()

    def test_lan_remote_switch_keeps_one_visible_native_window_and_independent_queues(self):
        from wan_transfer import WanTransferService
        class Watch(QObject):
            def __init__(self):super().__init__();self.events=[]
            def eventFilter(self,obj,event):
                if event.type() in (QEvent.Hide,QEvent.Show,QEvent.Move,QEvent.Resize):self.events.append(event.type())
                return False
        with tempfile.TemporaryDirectory() as directory:
            store=app_settings.SettingsStore(Path(directory)/'profile')
            lan=LanTransferService(discovery=False);wan=WanTransferService(Path(directory)/'wan',enabled=False)
            dialog=LanDialog(lan,store);dialog.show();flush()
            manager=SimpleNamespace(shelf=SimpleNamespace(_release_reveal_level=lambda:None),lan=lan,wan=wan,
                lan_dialog=dialog,wan_dialog=dialog,settings=store,_lan_selected=lambda:[],_center_dialog=Mock())
            manager._show_transfer_dialog=lambda service:flickdock.ShelfManager._show_transfer_dialog(manager,service)
            manager.show_lan_dialog=lambda:flickdock.ShelfManager.show_lan_dialog(manager)
            manager.show_wan_dialog=lambda:flickdock.ShelfManager.show_wan_dialog(manager)
            dialog.mode_selected.connect(lambda mode:manager.show_wan_dialog() if mode=='wan' else manager.show_lan_dialog())
            watch=Watch();dialog.installEventFilter(watch);handle=dialog.winId();geometry=dialog.geometry()
            first=Path(directory)/'lan.txt';first.write_text('LAN');second=Path(directory)/'remote.txt';second.write_text('WAN')
            lan_panel=dialog.panel;lan_panel.queue_paths([first])
            try:
                QTest.mouseClick(dialog.mode_buttons['lan' if dialog._wan else 'wan'],Qt.LeftButton);flush();wan_panel=dialog.panel
                self.assertIs(wan_panel.service,wan);wan_panel.queue_paths([second])
                for _ in range(12):
                    QTest.mouseClick(dialog.mode_buttons['lan' if dialog._wan else 'wan'],Qt.LeftButton);flush()
                    self.assertEqual(dialog.winId(),handle);self.assertEqual(dialog.geometry(),geometry);self.assertTrue(dialog.isVisible())
                self.assertFalse(watch.events);manager._center_dialog.assert_not_called()
                self.assertEqual(lan_panel._queued_paths,[str(first),str(second)]);self.assertEqual(wan_panel._queued_paths,[str(first),str(second)])
                self.assertEqual(len(dialog._panels),2)
            finally:dialog.close();dialog.deleteLater();lan.close();wan.close();flush()

    def test_settings_footer_has_opaque_gutter_and_does_not_clip_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            dialog=app_settings.SettingsDialog(app_settings.SettingsStore(Path(directory)))
            try:
                for theme in ('light','dark'):
                    dialog.apply_theme(theme);dialog.show();flush()
                    dialog.scroll.verticalScrollBar().setValue(dialog.scroll.verticalScrollBar().maximum());flush()
                    self.assertGreaterEqual(dialog.scroll.viewportMargins().bottom(),2)
                    footer=dialog.footer_frame
                    self.assertTrue(footer.testAttribute(Qt.WA_StyledBackground))
                    for button in (dialog.cancel_button,dialog.save_button):
                        self.assertTrue(footer.rect().contains(QRect(button.mapTo(footer,QPoint()),button.size())))
                    image=dialog.grab().toImage();point=footer.mapTo(dialog,QPoint(footer.width()//2,3))
                    pixel=image.pixelColor(round(point.x()*image.devicePixelRatio()),round(point.y()*image.devicePixelRatio()))
                    self.assertEqual(pixel.alpha(),255)
            finally:dialog.close();dialog.deleteLater();flush()

    def test_shake_stays_compact_when_edge_trigger_prefers_overview(self):
        fixture=_workspace.WorkspaceTests();fixture.setUp()
        try:
            shelf=fixture.shelf;shelf.hide_immediately();fixture.settings.update(trigger_view='expanded')
            manager=SimpleNamespace(outgoing_drag=False,auto_shown=None,shelf=shelf,active_shelf=shelf,
                shelves=[shelf],settings=fixture.settings,shake_detector=SimpleNamespace(trigger_velocity=(1000,20)))
            flickdock.ShelfManager._content_drag_validated(manager,QPoint(500,300));flush()
            self.assertFalse(shelf.expanded);self.assertEqual(shelf.presentation_kind,'drag');self.assertTrue(shelf.isVisible())
        finally:fixture.tearDown()

    def test_unsigned_entry_has_centered_unclipped_icon_and_one_whole_click_target(self):
        service=Service();service.logout();panel=AccountPanel(service);panel.resize(540,230);panel.show();flush()
        try:
            button=panel.unsigned_profile_button;glyph=panel.unsigned_glyph
            bounds=QRect(glyph.mapTo(button,QPoint()),glyph.size())
            self.assertTrue(button.rect().contains(bounds))
            self.assertLessEqual(abs(bounds.center().y()-button.rect().center().y()),1)
            self.assertIn('border:1px solid',panel.styleSheet())
            QTest.mouseClick(button,Qt.LeftButton,pos=QPoint(button.width()-4,button.height()//2));flush()
            self.assertTrue(panel.auth_dialog.isVisible())
        finally:panel.close();panel.deleteLater();flush()

    def test_low_resolution_login_register_and_password_fields_scroll_without_overlap(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(app_settings,'_startup_runtime_allowed',return_value=False):
            service=Service();service.logout();panel=AccountPanel(service);panel.show();flush()
            try:
                for size in ((800,450),(640,360),(400,300)):
                    area=QRect(0,0,*size)
                    with patch.object(app_settings.ReferenceModalDialog,'_available_geometry',return_value=area):
                        for mode in ('login','register'):
                            panel.show_auth(mode);flush();dialog=panel.auth_dialog;dialog.adjustSize();flush()
                            self.assertLessEqual(dialog.height(),area.height()-16)
                            self.assertLessEqual(dialog.width(),area.width()-16)
                            previous=None
                            for field in (panel.username_edit,panel.password_edit,panel.submit_button):
                                bounds=QRect(field.mapTo(dialog.body,QPoint()),field.size())
                                if previous is not None:self.assertLess(previous.bottom(),bounds.top())
                                previous=bounds;dialog.scroll.ensureWidgetVisible(field);flush()
                                viewport=dialog.scroll.viewport()
                                visible=QRect(field.mapTo(viewport,QPoint()),field.size()).intersected(viewport.rect())
                                self.assertEqual(visible.height(),field.height(),(size,mode,field.accessibleName(),field.geometry(),viewport.size(),dialog.scroll.verticalScrollBar().value(),dialog.scroll.verticalScrollBar().maximum()))
                            panel.close_auth()
                        service.signed_in=True;panel._toggle_password(True);flush()
                        dialog=panel.password_dialog
                        self.assertLessEqual(dialog.height(),area.height()-16)
                        previous=None
                        for field in (panel.old_password_edit,panel.new_password_edit,panel.confirm_password_edit,panel.password_change_button):
                            bounds=QRect(field.mapTo(dialog.body,QPoint()),field.size())
                            if previous is not None:self.assertLess(previous.bottom(),bounds.top())
                            previous=bounds;dialog.scroll.ensureWidgetVisible(field);flush()
                            self.assertEqual(QRect(field.mapTo(dialog.scroll.viewport(),QPoint()),field.size()).intersected(dialog.scroll.viewport().rect()).height(),field.height())
                        panel._reset_password();service.signed_in=False
            finally:panel.close();panel.deleteLater();flush()

    def test_status_placement_for_zero_one_two_and_four_devices(self):
        for count in (0,1,2,4):
            with tempfile.TemporaryDirectory() as directory,patch.object(app_settings,'_startup_runtime_allowed',return_value=False):
                service=Service();service.devices=[dict(id=str(i),name='设备 '+str(i),platform='windows',current=i==0) for i in range(count)]
                dialog=app_settings.SettingsDialog(app_settings.SettingsStore(Path(directory)));panel=AccountPanel(service);dialog.set_account_panel(panel);dialog.show();flush()
                try:
                    panel.set_sync_status('搁架已同步。');service.status_changed.emit('设备名称修改成功。');flush()
                    signed=QRect(panel.signed_in_page.mapTo(panel,QPoint()),panel.signed_in_page.size())
                    status=QRect(panel.status_label.mapTo(panel,QPoint()),panel.status_label.size())
                    devices=QRect(panel.devices_body.mapTo(panel,QPoint()),panel.devices_body.size())
                    self.assertLess(devices.bottom(),status.top(),count)
                    self.assertTrue(panel.rect().contains(status),count)
                    self.assertTrue(panel.profile_frame.rect().contains(QRect(panel.sync_status_label.mapTo(panel.profile_frame,QPoint()),panel.sync_status_label.size())),count)
                finally:dialog.close();dialog.deleteLater();flush()

    def test_finishing_update_paints_a_clean_surface_without_waiting_for_input(self):
        with tempfile.TemporaryDirectory() as directory:
            dialog=app_settings.SettingsDialog(app_settings.SettingsStore(Path(directory)));dialog.show();flush()
            try:
                for state in ('latest','available','error','cancelled'):
                    dialog.set_update_state('checking');flush()
                    line_y=dialog.update_activity.mapTo(dialog,QPoint(0,1)).y()
                    with patch.object(dialog.surface,'repaint',wraps=dialog.surface.repaint) as refresh:
                        dialog.set_update_state(state);flush()
                    self.assertIsNone(dialog.surface.graphicsEffect())
                    self.assertTrue(refresh.called);self.assertTrue(dialog.update_activity.isHidden());self.assertEqual(dialog.update_activity.height(),0)
                    image=dialog.grab().toImage();dpr=image.devicePixelRatio()
                    blue=sum(1 for x in range(40,dialog.width()-40) if (lambda c:c.blue()>150 and c.blue()>c.red()+50)(image.pixelColor(round(x*dpr),round(line_y*dpr))))
                    self.assertLess(blue,5,(state,blue))
            finally:dialog.close();dialog.deleteLater();flush()

    def test_menu_bar_icon_is_a_template_and_accessory_policy_uses_only_native_adapter(self):
        from flickdock_icons import menu_bar_icon
        from platform_native import configure_menu_bar_application
        self.assertTrue(menu_bar_icon().isMask())
        application=SimpleNamespace(setActivationPolicy_=Mock(return_value=True))
        appkit=SimpleNamespace(NSApplicationActivationPolicyAccessory=1,NSApplication=SimpleNamespace(sharedApplication=lambda:application))
        self.assertTrue(configure_menu_bar_application(platform='darwin',appkit=appkit))
        application.setActivationPolicy_.assert_called_once_with(1)
    def test_copy_code_click_immediately_shows_feedback_without_replacing_digits(self):
        for code in ('123456','ABCD2345'):
            button=_CopyCodeButton(code);button.show();flush()
            try:
                QTest.mouseClick(button,Qt.LeftButton);flush()
                self.assertEqual(APP.clipboard().text(),code)
                self.assertEqual(button.text(),code)
                self.assertTrue(QToolTip.isVisible());self.assertIn('已复制',QToolTip.text())
            finally:QToolTip.hideText();button.close();button.deleteLater();flush()

    def test_save_only_changed_fields_skips_disk_probe_and_shortcut_reregistration(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(app_settings,'_startup_runtime_allowed',return_value=False):
            store=app_settings.SettingsStore(Path(directory));dialog=app_settings.SettingsDialog(store)
            dialog.show();flush();dialog.shortcut_validator=Mock(side_effect=AssertionError('Unchanged shortcut validated'))
            try:
                dialog.keep_empty_handle_checkbox.toggle()
                with patch.object(store,'_validate_directory',side_effect=AssertionError('Unchanged directory probed')),patch.object(store,'update',wraps=store.update) as update:
                    dialog.save()
                update.assert_called_once_with(keep_empty_handle=dialog.keep_empty_handle_checkbox.isChecked())
                self.assertFalse(dialog.isVisible())
                self.assertEqual(app_settings.SettingsStore(Path(directory)).keep_empty_handle,store.keep_empty_handle)
            finally:dialog.close();dialog.deleteLater();flush()

    def test_presentation_setting_does_not_reapply_global_services(self):
        fixture=_workspace.WorkspaceTests();fixture.setUp()
        try:
            manager=fixture.manager;manager._applied_settings=dict(fixture.settings._data)
            manager.reset_drag_gesture=Mock(side_effect=AssertionError('Unrelated drag reset'))
            manager.hotkeys=SimpleNamespace(apply=Mock(side_effect=AssertionError('Unrelated hotkey registration')))
            manager.sync=SimpleNamespace(set_directions=Mock(side_effect=AssertionError('Unrelated sync reset')))
            manager.update_handles=Mock()
            fixture.settings.update(keep_empty_handle=not fixture.settings.keep_empty_handle)
            with patch.object(flickdock.ui_theme,'apply_application',side_effect=AssertionError('Unrelated application repolish')):
                flickdock.ShelfManager.apply_settings(manager)
            manager.update_handles.assert_called_once()
        finally:fixture.tearDown()

    def test_hover_colors_inner_dashes_and_keeps_outer_panel_transparent_for_dock_and_shake(self):
        fixture=_workspace.WorkspaceTests();fixture.setUp()
        try:
            shelf=fixture.shelf;shelf.files['permanent']=[];shelf.refresh();zone=shelf.zones['permanent']
            def blue_pixels():
                image=zone.icon.grab().toImage();count=0
                scale=image.devicePixelRatio()
                for x in range(round(30*scale),round(35*scale)):
                    for y in range(round(47*scale),round(140*scale)):
                        c=image.pixelColor(x,y)
                        if c.blue()>c.red()+40 and c.blue()>c.green()+25:count+=1
                return count
            for kind in ('dock','drag'):
                shelf.presentation_kind=kind
                APP.sendEvent(zone,QEvent(QEvent.Leave));flush();normal=blue_pixels()
                APP.sendEvent(zone,QEvent(QEvent.Enter));flush()
                self.assertNotIn('#0A84FF',zone.styleSheet())
                self.assertGreater(blue_pixels(),normal+15)
                self.assertIsNone(shelf.popup)
                APP.sendEvent(zone,QEvent(QEvent.Leave));flush();self.assertEqual(blue_pixels(),normal)
        finally:fixture.tearDown()

    def test_return_from_transfer_keeps_same_card_bounds_without_morph_or_edge_reveal(self):
        fixture=_workspace.WorkspaceTests();fixture.setUp();service=LanTransferService(discovery=False)
        dialog=LanDialog(service,fixture.settings);dialog.move(300,160);dialog.show();flush()
        manager=flickdock.ShelfManager.__new__(flickdock.ShelfManager);manager.shelf=fixture.shelf
        manager.app=APP;manager._quitting=False
        manager.motion=fixture.manager.motion
        manager.reset_drag_gesture=lambda:None
        dialog.overview_requested.connect(lambda:manager.show_overview_from_transfer(dialog))
        previous=QRect(200,100,1148,707)
        manager._overview_return_rect=previous
        expected=previous.adjusted(14,7,-14,-20)
        try:
            with patch.object(fixture.shelf,'show_transient',side_effect=AssertionError('Edge reveal called')):
                QTest.mouseClick(dialog.back_button,Qt.LeftButton);flush()
            actual=QRect(fixture.shelf.card.mapToGlobal(QPoint()),fixture.shelf.card.size())
            self.assertEqual(actual,expected)
            self.assertTrue(fixture.shelf.expanded);self.assertTrue(fixture.shelf.popup.isVisible())
            self.assertFalse(fixture.shelf.geometry_morph.active)
        finally:dialog.close();dialog.deleteLater();service.close();flush();fixture.tearDown()

    def test_rename_status_and_sync_subtitle_do_not_overlap_controls_or_clip(self):
        for theme in ('light','dark'):
            with tempfile.TemporaryDirectory() as directory,patch.object(app_settings,'_startup_runtime_allowed',return_value=False):
                dialog=app_settings.SettingsDialog(app_settings.SettingsStore(Path(directory)))
                panel=AccountPanel(Service(),theme=theme);dialog.set_account_panel(panel);dialog.show();flush()
                try:
                    panel.set_sync_status('搁架已同步。');panel.service.status_changed.emit('设备名称修改成功。')
                    panel.device_rename_buttons['other'].click();flush()
                    form=panel._rename_forms['other'];row=panel._rows_by_id['other']
                    self.assertTrue(form.isWindow());self.assertEqual(row.height(),44)
                    self.assertTrue(form.screen().availableGeometry().contains(form.frameGeometry()))
                    for label in (panel.username_label,panel.sync_status_label):
                        bounds=QRect(label.mapTo(panel.profile_frame,QPoint()),label.size())
                        self.assertTrue(panel.profile_frame.rect().contains(bounds),(label.text(),bounds,panel.profile_frame.rect()))
                    signed=QRect(panel.signed_in_page.mapTo(panel,QPoint()),panel.signed_in_page.size())
                    status=QRect(panel.status_label.mapTo(panel,QPoint()),panel.status_label.size())
                    devices=QRect(panel.devices_body.mapTo(panel,QPoint()),panel.devices_body.size())
                    self.assertLess(devices.bottom(),status.top())
                    self.assertTrue(panel.rect().contains(status));self.assertTrue(panel.rect().contains(signed))
                    self.assertFalse(panel.profile_frame.geometry().intersects(panel.devices_body.geometry()))
                finally:dialog.close();dialog.deleteLater();flush()

    def test_idle_status_counts_only_present_blocked_items_not_tombstones_or_paused_uploads(self):
        statuses=[]
        entries={
            'removed':dict(dirty=True,deleted=True,version=1),
            'paused':dict(dirty=True,paused_upload=True,ref='paused'),
            'stale':dict(dirty=True,ref='gone',fingerprint='old',blocked_fingerprint='old')}
        engine=SimpleNamespace(upload_enabled=True,receive_enabled=True,state=dict(entries=entries),running=True,transfer=None,
            _clipboard_priority=False,_lane_context=None,small_lane=SimpleNamespace(active=None,retry_at={}),clipboard_bridge=None,
            _cycle_direction='',_refresh_again=False,_notify_transfers=lambda:None,_pending_removal=lambda:None,_paused=lambda _:False,
            adapter=SimpleNamespace(snapshot=lambda:dict(permanent=['blocked'])),status_changed=SimpleNamespace(emit=statuses.append))
        for identity,entry in entries.items():entry['id']=identity
        ShelfSync._push_next(engine);self.assertEqual(statuses[-1],'搁架已同步。')
        engine.adapter.snapshot=lambda:dict(permanent=['paused','blocked'])
        ShelfSync._push_next(engine);self.assertIn('1 项暂停上传',statuses[-1]);self.assertNotIn('暂留本机',statuses[-1])
        entries['blocked']=dict(id='blocked',dirty=True,ref='blocked',fingerprint='current',blocked_fingerprint='current')
        ShelfSync._push_next(engine);self.assertIn('1 项暂留本机',statuses[-1])

if __name__=='__main__':unittest.main()
