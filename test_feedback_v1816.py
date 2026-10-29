"""Reported Mac/Windows regressions, real Qt controls and isolated profiles."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import tempfile,unittest,sys,plistlib
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QPoint,Qt
from PySide6.QtTest import QTest
from test_html_native_workspace import APP,flush,until
import test_html_native_workspace as workspace_fixture
from test_account_settings_v181 import Service
from account_panel import AccountPanel
import app_settings,drag_probe,flickdock,native_ui_common

class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.fixture=workspace_fixture.WorkspaceTests();self.fixture.setUp();self.shelf=self.fixture.shelf
    def tearDown(self):self.fixture.tearDown()

    @unittest.skipUnless(sys.platform=='win32','Windows font selection')
    def test_windows_font_and_size_step_preserve_already_small_labels(self):
        with patch.object(native_ui_common.sys,'platform','win32'):
            self.assertEqual(native_ui_common.configure_reference_font(APP).family(),'HarmonyOS Sans SC')
            qss=native_ui_common.readable_qss('a{font-size:12px;} b{font-size:10px;}')
            self.assertIn('font-size:13px',qss);self.assertIn('font-size:12px',qss)

    def test_mac_probe_survives_foreground_source_and_uses_nonactivating_raise(self):
        with patch.object(drag_probe.sys,'platform','darwin'),patch.object(drag_probe,'native_window_move_active',return_value=False):
            gate=drag_probe.ContentDragGate(lambda p:None)
            try:self.assertTrue(gate.probe.testAttribute(Qt.WA_MacAlwaysShowToolWindow))
            finally:gate.close()

    def test_resumed_account_refreshes_existing_overview_profile(self):
        from types import SimpleNamespace
        view=self.fixture.overview();service=Service();self.fixture.manager.account=service
        service.device_name='Mac';self.fixture.manager.lan=SimpleNamespace(device_name='')
        self.fixture.manager.shelves=[self.shelf]
        service.account_changed.connect(lambda account:flickdock.ShelfManager._lan_identity(self.fixture.manager,account))
        service.account={'id':'restored-fixture','username':'已恢复账号'}
        service.account_changed.emit(dict(service.account));flush()
        self.assertEqual(view.account_nav.username,'已恢复账号');self.assertTrue(view.account_nav.connected)

    def test_first_overview_after_account_resume_has_the_current_identity(self):
        service=Service();service.account={'id':'resumed-before-open','username':'恢复后首次打开'}
        self.fixture.manager.account=service
        self.assertIsNone(self.shelf.popup)
        view=self.fixture.overview();flush()
        self.assertEqual(view.account_nav.username,'恢复后首次打开')
        self.assertTrue(view.account_nav.connected)

    def test_large_drop_offer_queues_captured_files_without_removing_the_shelf(self):
        from types import SimpleNamespace
        from sync_files import oversized_intake
        import threading
        path=self.fixture.base/'large.bin'
        with path.open('wb') as stream:stream.truncate(200*1024*1024)
        self.assertEqual(oversized_intake([str(path)],threading.Event()),[])
        with path.open('r+b') as stream:stream.truncate(200*1024*1024+1)
        self.assertEqual(oversized_intake([str(path)],threading.Event()),[str(path)])
        manager=flickdock.ShelfManager.__new__(flickdock.ShelfManager);queued=[];opened=[]
        manager._quitting=False;manager._large_intake_prompt=False;manager._large_intake_queue=[[str(path)]]
        manager._update_busy=lambda:False;manager._confirm_large_remote=lambda:True
        manager.shelf=SimpleNamespace(files={'permanent':[str(path)]})
        manager.wan_dialog=SimpleNamespace(panel=SimpleNamespace(queue_paths=lambda paths:queued.extend(paths)))
        manager.show_wan_dialog=lambda:opened.append(True)
        manager._offer_next_large_intake();flush()
        self.assertEqual(opened,[True]);self.assertEqual(queued,[str(path)])
        self.assertEqual(manager.shelf.files['permanent'],[str(path)])

    def test_secondary_dialog_is_recentered_after_a_previous_move(self):
        from PySide6.QtWidgets import QDialog
        from PySide6.QtCore import QRect
        class Screen:
            def availableGeometry(self):return QRect(1920,30,1600,900)
        dialog=QDialog();dialog.resize(800,500);dialog.move(11,12)
        manager=flickdock.ShelfManager.__new__(flickdock.ShelfManager)
        with patch.object(flickdock.QGuiApplication,'screenAt',return_value=Screen()):
            manager._center_dialog(dialog);first=dialog.pos();dialog.move(38,40);manager._center_dialog(dialog)
        self.assertEqual(dialog.pos(),first)
        self.assertLessEqual((dialog.geometry().center()-Screen().availableGeometry().center()).manhattanLength(),2)
        dialog.deleteLater();flush()

    def test_other_device_rename_has_a_visible_direct_action(self):
        panel=AccountPanel(Service());panel.resize(650,260);panel.show();flush()
        try:
            from account_panel import AccountNameLabel
            for name in panel.devices_body.findChildren(AccountNameLabel):
                self.assertGreater(name.width(),30)
            button=panel.device_rename_buttons['other'];self.assertTrue(button.isVisible())
            QTest.mouseClick(button,Qt.LeftButton);flush()
            self.assertTrue(panel.device_name_edits['other'].isVisible())
            panel.device_name_edits['other'].setText('新的 Mac 名称')
            QTest.mouseClick(panel._rename_save_buttons['other'],Qt.LeftButton)
            self.assertIn(('rename',('other','新的 Mac 名称')),panel.service.calls)
        finally:panel.close();panel.deleteLater();flush()

    def test_view_segment_inner_capsules_are_vertically_centered(self):
        view=self.fixture.overview();flush()
        for control in (view.view_list,view.view_grid):
            top=control.y();bottom=view.view_segment.height()-control.geometry().bottom()-1
            self.assertLessEqual(abs(top-bottom),1,(top,bottom))

    def test_host_routed_expand_button_edges_never_start_title_drag(self):
        self.fixture.overview();flush()
        button=self.shelf.expand_button
        for point in (QPoint(1,1),QPoint(30,1),QPoint(1,30),QPoint(30,30),QPoint(16,16)):
            before=self.shelf.expanded
            QTest.mouseClick(self.shelf,Qt.LeftButton,pos=button.mapTo(self.shelf,point));flush()
            self.assertIsNone(self.shelf.drag_origin)
            self.assertNotEqual(self.shelf.expanded,before)

    def test_mac_green_settings_control_zooms_and_restores(self):
        from PySide6.QtWidgets import QPushButton
        with tempfile.TemporaryDirectory() as root,patch.object(app_settings.sys,'platform','darwin'):
            dialog=app_settings.SettingsDialog(app_settings.SettingsStore(Path(root)))
            dialog.show();flush();before=dialog.geometry()
            try:
                green=dialog.findChildren(QPushButton,'settingsTraffic')[2]
                QTest.mouseClick(green,Qt.LeftButton);flush()
                self.assertNotEqual(dialog.geometry(),before)
                QTest.mouseClick(green,Qt.LeftButton);flush();self.assertEqual(dialog.geometry(),before)
            finally:dialog.close();dialog.deleteLater();flush()

    def test_settings_segments_keep_one_selection_on_repeated_clicks(self):
        from PySide6.QtWidgets import QPushButton
        with tempfile.TemporaryDirectory() as root:
            dialog=app_settings.SettingsDialog(app_settings.SettingsStore(Path(root)))
            dialog.show();flush()
            try:
                segments=dialog.findChildren(__import__('PySide6.QtWidgets',fromlist=['QFrame']).QFrame,'settingsSegment')
                for segment in segments:
                    buttons=segment.findChildren(QPushButton,'settingsSegmentButton')
                    for button in buttons:
                        for _ in range(2):
                            QTest.mouseClick(button,Qt.LeftButton);flush()
                            self.assertEqual(sum(item.isChecked() for item in buttons),1)
                            self.assertTrue(button.isChecked())
                    if any(item.property('segmentGlyph')=='grid' for item in buttons):
                        self.assertEqual({item.property('segmentGlyph') for item in buttons},{'list','grid'})
                        self.assertTrue(all(not item.icon().isNull() for item in buttons))
            finally:dialog.close();dialog.deleteLater();flush()

    def test_empty_overview_has_centered_clickable_drop_box(self):
        from PySide6.QtCore import QEvent,QSize
        self.shelf.files['permanent']=[]
        view=self.fixture.overview();flush()
        self.assertEqual(view.title.text(),'FlickDock');self.assertEqual(view.count_label.text(),'0 项')
        box=view.empty_drop
        self.assertTrue(box.isVisible());self.assertEqual(box.size(),QSize(280,220))
        self.assertLessEqual(abs(box.geometry().center().x()-view.empty_state.rect().center().x()),1)
        with patch.object(self.shelf,'pick_files') as choose:
            QTest.mouseClick(box,Qt.LeftButton);choose.assert_called_once_with('permanent')
        APP.sendEvent(box,QEvent(QEvent.Enter));self.assertTrue(box._hovered)
        APP.sendEvent(box,QEvent(QEvent.Leave));self.assertFalse(box._hovered)

    def test_incoming_cloud_upload_appears_in_an_already_open_overview_and_preview(self):
        self.shelf.files['permanent']=[]
        view=self.fixture.overview();flush();self.fixture.manager._sync_transfer_refs=set()
        state=dict(id='live-remote',phase='remote',remote=True,name='另一台设备.png',kind='file',received=0,total=1000000)
        flickdock.ShelfManager._transfers_changed(self.fixture.manager,[state]);flush()
        ref='flickdock:transfer:live-remote'
        self.assertIn(ref,view.visible_refs());self.assertFalse(view.empty_state.isVisible())
        self.assertEqual(view.nav_counts['all'].text(),'1');self.assertEqual(view.count_label.text(),'1 项')
        view.set_category('images');flush();self.assertIn(ref,view.visible_refs())
        self.assertEqual(view.preview.current_path,ref);self.assertFalse(view.preview.open_button.isEnabled())
        self.assertEqual(self.shelf.zones['permanent'].caption.text(),'1 项')
        state['received']=400000
        flickdock.ShelfManager._transfers_changed(self.fixture.manager,[state]);flush()
        self.assertEqual(view.preview.current_path,ref)
        self.assertEqual(self.shelf.content.transfers[ref]['received'],400000)
        self.assertFalse(self.shelf.content.mime_for([ref]).hasUrls())

    def test_dock_drop_zone_blue_border_follows_hover_without_opening_overview(self):
        from PySide6.QtCore import QEvent
        zone=self.shelf.zones['permanent'] if hasattr(self.shelf,'zones') else self.shelf.drop_zone
        self.shelf.presentation_kind='dock'
        APP.sendEvent(zone,QEvent(QEvent.Enter));flush()
        self.assertNotIn('#0A84FF',zone.styleSheet());self.assertTrue(zone.icon._hovered);self.assertIsNone(self.shelf.popup)
        APP.sendEvent(zone,QEvent(QEvent.Leave));flush()
        self.assertNotIn('#0A84FF',zone.styleSheet())
        zone.drop_highlight=True;zone._apply_style()
        self.assertNotIn('#0A84FF',zone.styleSheet())

    def test_rounded_combo_and_menu_preserve_keyboard_selection_and_close(self):
        from PySide6.QtWidgets import QMenu
        from PySide6.QtCore import QPoint
        combo=app_settings.SettingsComboBox();combo.addItems(['右侧','左侧','顶部'])
        combo.resize(160,32);combo.show();flush()
        try:
            for _ in range(2):
                combo.showPopup();flush();popup=combo.view().window()
                self.assertTrue(popup.isVisible())
                self.assertFalse(popup.mask().contains(QPoint(0,0)))
                self.assertTrue(popup.mask().contains(popup.rect().center()))
                QTest.keyClick(combo.view(),Qt.Key_Down);QTest.keyClick(combo.view(),Qt.Key_Return);flush()
                self.assertFalse(popup.isVisible())
            self.assertEqual(combo.currentIndex(),2)
        finally:combo.close();combo.deleteLater();flush()
        menu=QMenu();menu.addAction('云同步');menu.addAction('设置')
        flickdock.style_menu(menu);menu.popup(QPoint(30,30));flush()
        try:
            self.assertFalse(menu.mask().contains(QPoint(0,0)))
            self.assertTrue(menu.mask().contains(menu.rect().center()))
            QTest.keyClick(menu,Qt.Key_Escape);flush();self.assertFalse(menu.isVisible())
        finally:menu.close();menu.deleteLater();flush()

    def test_one_icon_never_stretches_to_fill_content_pane(self):
        self.shelf.files['permanent']=self.shelf.files['permanent'][:1]
        self.shelf.manager.settings.update(view_mode='icons')
        view=self.fixture.overview();view.resume_content();flush()
        from content_grid import IconItem
        until(lambda:len(view.rows_body.findChildren(IconItem))==1)
        tile=view.rows_body.findChildren(IconItem)[0]
        self.assertLessEqual(tile.width(),180)
        self.assertEqual(tile.height(),120)

    def test_finished_update_removes_the_activity_row_immediately(self):
        with tempfile.TemporaryDirectory() as root:
            dialog=app_settings.SettingsDialog(app_settings.SettingsStore(Path(root)))
            dialog.show();dialog.set_update_state('checking');flush()
            self.assertGreater(dialog.update_activity.height(),0)
            dialog.set_update_state('latest')
            self.assertTrue(dialog.update_activity.isHidden());self.assertEqual(dialog.update_activity.height(),0)
            dialog.set_update_state('checking');self.assertEqual(dialog.update_activity.height(),4)
            dialog.close();dialog.deleteLater();flush()

    def test_new_update_has_a_version_capsule_and_green_action(self):
        with tempfile.TemporaryDirectory() as root:
            dialog=app_settings.SettingsDialog(app_settings.SettingsStore(Path(root)))
            dialog.show();dialog.set_update_state('available',version='1.9.0');flush()
            self.assertTrue(dialog.update_version_label.isVisible())
            self.assertEqual(dialog.update_version_label.text(),'v1.9.0')
            self.assertTrue(dialog.update_button.property('updateAvailable'))
            self.assertEqual(dialog.update_button.text(),'立即更新')
            self.assertTrue(dialog.update_activity.isHidden())
            dialog.set_update_state('latest');self.assertFalse(dialog.update_version_label.isVisible())
            self.assertFalse(dialog.update_button.property('updateAvailable'))
            dialog.close();dialog.deleteLater();flush()

    def test_mac_edge_includes_exact_outer_coordinate_and_native_canvas_stays_inside_workarea(self):
        class Screen:
            def availableGeometry(self):return __import__('PySide6.QtCore',fromlist=['QRect']).QRect(0,24,1500,950)
            def geometry(self):return __import__('PySide6.QtCore',fromlist=['QRect']).QRect(0,0,1512,982)
        self.fixture.manager.dock_values=lambda:dict(dock_edge='right',dock_position=8,dock_length=76,dock_thickness=8)
        with patch.object(flickdock.QGuiApplication,'primaryScreen',return_value=Screen()),patch.object(flickdock.QGuiApplication,'screenAt',return_value=Screen()),patch.object(flickdock.sys,'platform','darwin'):
            edge=flickdock.DockHandle(self.fixture.manager)
            self.assertTrue(edge.activation_region().contains(QPoint(1512,edge.geometry().center().y())))
            from PySide6.QtCore import QRect
            canvas=self.shelf._reserved_surface_rect(QRect(1140,28,360,345))
            self.assertTrue(Screen().availableGeometry().contains(canvas),canvas)
            edge.deleteLater()

    def test_received_web_link_rehydrates_preview_and_shortcuts_keep_native_file_payload(self):
        from sync_adapter import ShelfAdapter
        from types import SimpleNamespace
        from shortcut_preview import shortcut_details
        ref=self.shelf.content.add_text('https://example.invalid/page')
        self.fixture.manager.app=SimpleNamespace(platformName=lambda:'cocoa')
        view={'permanent':[ref],'texts':{ref:'https://example.invalid/page'},'metadata':{ref:{'title':'链接'}}}
        with patch.object(self.shelf.resolver,'resolve',return_value=True) as resolve:
            ShelfAdapter(self.fixture.manager).restore(view)
            resolve.assert_called_once_with(ref,'https://example.invalid/page')
        from PySide6.QtGui import QImage,QColor
        cover=self.fixture.base/'remote-cover.png';image=QImage(8,8,QImage.Format_RGB32);image.fill(QColor('blue'));image.save(str(cover))
        self.shelf._resolved_content(ref,dict(path=str(cover),source_url='https://example.invalid/page',kind='image'))
        self.assertIn(ref,self.shelf.files['permanent']);self.assertEqual(self.shelf.content.text(ref),'https://example.invalid/page')
        self.assertEqual(self.shelf.content.metadata[ref]['preview_image'],str(cover))
        self.shelf.content.metadata[ref].pop('preview_image')
        self.shelf._resolved_content(ref,dict(source_url='https://example.invalid/page',kind='link',error=''))
        with patch.object(self.shelf.resolver,'resolve',return_value=True) as resolve:
            self.shelf.queue_link_previews([ref]);self.shelf.queue_link_previews([ref])
            resolve.assert_not_called()
        path=self.fixture.base/'网页.url';original=b'[InternetShortcut]\r\nURL=https://example.invalid/page\r\n';path.write_bytes(original)
        self.assertEqual(shortcut_details(path)['url'],'https://example.invalid/page')
        self.shelf.add_paths('permanent',[str(path)])
        popup=self.fixture.overview();popup.preview.show_path(str(path),force=True)
        self.assertIs(popup.preview.stack.currentWidget(),popup.preview.text)
        self.assertEqual(path.read_bytes(),original);self.assertFalse(self.shelf.content.is_text(str(path)))

    def test_lan_long_names_and_failure_reason_survive_history(self):
        import threading
        from lan_transfer import _make_manifest
        from lan_panel import LanPanel
        from test_html_native_lan import FakeLan
        path=self.fixture.base/('x'*170+'.txt');path.write_bytes(b'original')
        entries,_,total=_make_manifest([path],threading.Event())
        self.assertEqual(entries[0]['path'],path.name);self.assertEqual(total,8)
        service=FakeLan();panel=LanPanel(service,settings=self.fixture.settings);panel.show();flush()
        panel._progress(dict(id='controlled',direction='send',peer='Mac',state='error',received=0,total=8,message='文件权限测试原因'))
        self.assertEqual(panel._history[0]['error'],'文件权限测试原因')
        self.assertIn('文件权限测试原因',panel.status_label.text())
        self.assertTrue(panel._rows['controlled']['frame'].isHidden())
        panel.close();panel.deleteLater();flush()

    def test_mac_default_startup_is_registered_in_isolated_launchagent(self):
        from platform_startup import StartupManager,LaunchAgentBackend
        with tempfile.TemporaryDirectory() as root:
            base=Path(root);binary=base/'FlickDock.app/Contents/MacOS/FlickDock';binary.parent.mkdir(parents=True);binary.write_bytes(b'fixture')
            self.assertTrue(app_settings.SettingsStore(base/'profile').autostart)
            backend=LaunchAgentBackend(base,system_actions=False)
            manager=StartupManager(binary,platform='darwin',frozen=True,qt_platform='cocoa',backend=backend,runtime_allowed=True)
            self.assertTrue(manager.reconcile(True).enabled)
            self.assertTrue(plistlib.loads(backend.path.read_bytes())['RunAtLoad'])

if __name__=='__main__':unittest.main(verbosity=2)
