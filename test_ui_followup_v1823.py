"""Actual offscreen routes for the user's follow-up; no native desktop input."""
import tempfile,unittest
from pathlib import Path
from PySide6.QtCore import QEvent,QPoint,QPointF,QRect,Qt,QMimeData,QUrl
from PySide6.QtGui import QEnterEvent,QMouseEvent,QDragEnterEvent
from unittest.mock import patch
import test_html_native_workspace as ws
from test_html_native_lan import FakeLan
from lan_panel import LanDialog
from app_settings import SettingsStore
from flickdock import ShelfManager
from content_grid import added_time_for_ref

class FollowupTests(unittest.TestCase):
    def setUp(self):
        # Ordinary motion tests need both the local intent and shared clock
        # enabled, independently of the host OS accessibility preference.
        shared=patch('native_ui_common.reduced_motion',return_value=False)
        shared.start();self.addCleanup(shared.stop)
        self.fixture=ws.WorkspaceTests('runTest');self.fixture.setUp();self.shelf=self.fixture.shelf
    def tearDown(self):self.fixture.tearDown()

    def test_pointer_hits_only_cards_while_external_drag_uses_whole_compact_panel(self):
        shelf=self.shelf;zone=shelf.zones['permanent'];tile=zone.icon
        tile.motion=False;tile.landing.stop();tile.arrival=1
        def move(point):
            global_point=zone.mapToGlobal(point)
            event=QMouseEvent(QEvent.MouseMove,QPointF(point),QPointF(global_point),Qt.NoButton,Qt.NoButton,Qt.NoModifier)
            zone.mouseMoveEvent(event)
        move(QPoint(10,10));self.assertEqual(tile.spread,0)
        move(tile.mapTo(zone,QPoint(120,85)));self.assertEqual(tile.spread,1)
        move(QPoint(10,10));self.assertEqual(tile.spread,0)
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(self.fixture.paths[0])])
        event=QDragEnterEvent(QPoint(10,10),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier)
        shelf.dragEnterEvent(event);self.assertTrue(event.isAccepted());self.assertEqual(tile.spread,1)
        shelf.dragLeaveEvent(QEvent(QEvent.DragLeave));self.assertEqual(tile.spread,0)
        self.assertFalse(shelf._drag_stack_active)

    def test_back_uses_moved_transfer_surface_and_keeps_overview_size(self):
        shelf=self.shelf;shelf.show_contents(animate=False);ws.flush()
        manager=self.fixture.manager;manager.reset_drag_gesture=lambda:None
        manager._overview_return_rect=QRect(shelf.visual_geometry());size=manager._overview_return_rect.size()
        with tempfile.TemporaryDirectory() as directory:
            dialog=LanDialog(FakeLan(),SettingsStore(Path(directory)));dialog.show();dialog.move(260,110);ws.flush()
            anchor=dialog.card.mapToGlobal(QPoint());shelf.hide_immediately()
            dialog.overview_requested.connect(lambda:ShelfManager.show_overview_from_transfer(manager,dialog))
            try:
                dialog.back_button.click();ws.until(lambda:not dialog.isVisible() and shelf.isVisible() and shelf.expanded)
                self.assertEqual(shelf.visual_geometry().topLeft(),anchor)
                self.assertEqual(shelf.visual_geometry().size(),size)
            finally:dialog.hide();dialog.deleteLater();ws.flush()

    def test_remote_pending_time_reaches_list_card_and_detail_formatter(self):
        ref='flickdock:transfer:remote';stamp='2026-10-31T12:34:56Z'
        self.shelf.content.metadata[ref]={'shelf_added_at':'2020-01-01T00:00:00Z'}
        self.shelf.content.transfers[ref]={'remote':True,'shelf_added_at':stamp}
        from PySide6.QtCore import QDateTime
        expected=QDateTime.fromString(stamp,Qt.ISODate).toLocalTime()
        self.assertEqual(added_time_for_ref(self.shelf,ref),expected.toString('MM-dd HH:mm:ss'))
        self.assertEqual(added_time_for_ref(self.shelf,ref,full=True),expected.toString('yyyy-MM-dd HH:mm:ss'))
        self.assertEqual(self.shelf.content.metadata[ref]['shelf_added_at'],'2020-01-01T00:00:00Z')

    def test_category_entrance_is_user_intent_only_and_keeps_live_metadata(self):
        popup=self.fixture.overview()
        with patch('content_entrance.motion_allowed',return_value=True):
            popup.set_category('files');ws.flush()
            controller=popup._category_entrance
            self.assertGreater(controller.active_count,0)
            epoch=controller.generation
            # Metadata/status refresh does not create a second entrance clock.
            popup.update_transfer_status();popup.rebuild()
            self.assertEqual(controller.generation,epoch)
            popup.set_category('text');popup.set_category('all');ws.flush()
            self.assertLessEqual(controller.active_count,len(popup._item_widgets))
            controller.animation.setCurrentTime(controller.animation.duration())
            self.assertEqual(controller.active_count,0)
            self.assertTrue(all(w.graphicsEffect() is None for w in popup._item_widgets.values()))

    def test_search_detail_and_filename_editor_have_input_local_focus_feedback(self):
        popup=self.fixture.overview()
        self.assertIsNotNone(popup.search_edit._input_focus_motion)
        self.assertIsNotNone(popup.preview.text._input_focus_motion)
        self.shelf.rename_ref(self.fixture.paths[0])
        dialog=self.shelf._rename_dialog;ws.flush()
        self.assertIsNotNone(self.shelf.rename_edit._input_focus_motion)
        original=self.shelf.rename_edit.text()
        dialog.reject();ws.flush()
        self.assertFalse(dialog.isVisible())
        self.assertTrue(Path(self.fixture.paths[0]).exists())
        self.assertEqual(self.shelf.rename_edit.text(),original)

    def test_fresh_edge_reveal_has_small_presentation_overshoot_without_native_resize(self):
        shelf=self.shelf;shelf.hide_immediately();shelf.manager.motion=True;shelf.presentation.motion=True
        with patch('native_ui_common.reduced_motion',return_value=False):
            shelf._show_at(QPoint(80,90),'dock',QPoint(30,0));shelf.presentation._timer.stop()
            bounds=QRect(shelf.geometry());values=[]
            shelf.presentation._last_tick=shelf.presentation._started_at=0.
            import flickdock_motion
            for i in range(1,31):
                with patch.object(flickdock_motion.time,'perf_counter',return_value=i*.016):shelf.presentation._tick()
                values.append(shelf.presentation.values['sx'])
                self.assertEqual(shelf.geometry(),bounds)
            self.assertGreater(max(values),1.005);self.assertLess(max(values),1.015)
            self.assertEqual(shelf.presentation.values['sx'],1)
            self.assertFalse(shelf.presentation._timer.isActive())

    def test_context_transfer_uses_overview_selection_but_compact_menu_uses_every_file(self):
        import flickdock as subject
        popup=self.fixture.overview();popup.selected=set(self.fixture.paths[:2])
        calls=[];self.fixture.manager.queue_transfer_paths=lambda transport,paths:calls.append((transport,list(paths)))
        def trigger(menu,*_):
            actions={action.text():action for action in menu.actions()}
            for title in ('使用局域网传输','使用远程传输'):
                self.assertTrue(actions[title].isEnabled());actions[title].trigger()
        with patch.object(subject.AnimatedMenu,'exec',trigger):
            popup.show_item_menu(self.fixture.paths[0],QPoint())
        self.assertEqual(calls,[('lan',self.fixture.paths[:2]),('wan',self.fixture.paths[:2])])
        calls.clear();self.shelf.collapse_contents();self.shelf.geometry_morph.complete();ws.flush()
        with patch.object(subject.AnimatedMenu,'exec',trigger):self.shelf.show_menu(pos=QPoint())
        self.assertEqual(calls,[('lan',self.fixture.paths),('wan',self.fixture.paths)])

    def test_each_click_including_second_press_of_double_click_only_toggles_selection(self):
        shelf=self.shelf;popup=self.fixture.overview();ref=self.fixture.paths[0]
        for mode in ('list','icons'):
            popup.change_view_mode(mode);ws.until(lambda:popup._build_cursor==len(popup.visible_refs()))
            item=popup._item_widgets[ref];popup.selected.clear();popup.primary_selection=None
            point=QPointF(item.width()/2,item.height()/2);global_point=QPointF(item.mapToGlobal(point.toPoint()))
            def send(kind,buttons):
                event=QMouseEvent(kind,point,global_point,Qt.LeftButton,buttons,Qt.NoModifier)
                ws.APP.sendEvent(item,event)
            with patch.object(shelf,'open_path') as opened:
                send(QEvent.MouseButtonPress,Qt.LeftButton);send(QEvent.MouseButtonRelease,Qt.NoButton)
                self.assertIn(ref,popup.selected)
                send(QEvent.MouseButtonDblClick,Qt.LeftButton);send(QEvent.MouseButtonRelease,Qt.NoButton)
                self.assertNotIn(ref,popup.selected);opened.assert_not_called()
                send(QEvent.MouseButtonDblClick,Qt.LeftButton);send(QEvent.MouseButtonRelease,Qt.NoButton)
                self.assertIn(ref,popup.selected);opened.assert_not_called()

    def test_open_success_closes_with_position_appropriate_motion_without_native_resize(self):
        from types import SimpleNamespace
        shelf=self.shelf;manager=self.fixture.manager
        manager.edge=SimpleNamespace(screen_area=lambda:QRect(0,0,1920,1080),geometry=lambda:QRect(1912,300,8,90))
        manager.motion=True;shelf.presentation.motion=True;shelf.pinned=False
        shelf.presentation_kind='manual';shelf.presentation.jump(dict(sx=1,sy=1,dx=3,dy=4,alpha=1))
        host=QRect(shelf.geometry())
        with patch('PySide6.QtGui.QDesktopServices.openUrl',return_value=True):shelf.open_path(self.fixture.paths[0])
        self.assertEqual(shelf.visible_state,'hiding');self.assertEqual(shelf.geometry(),host)
        self.assertEqual((shelf.presentation._targets['dx'],shelf.presentation._targets['dy']),(3,4))
        self.assertEqual(shelf.presentation._targets['sx'],shelf.presentation._targets['sy'])
        ws.until(lambda:not shelf.isVisible())
        shelf._show_at(QPoint(1500,200),'dock',QPoint());shelf.presentation.jump(dict(sx=1,sy=1,dx=0,dy=0,alpha=1))
        host=QRect(shelf.geometry());shelf.hide_animated(explicit=False)
        self.assertEqual((shelf.presentation._targets['dx'],shelf.presentation._targets['dy']),(32,0))
        self.assertEqual(shelf.geometry(),host);ws.until(lambda:not shelf.isVisible())

    def test_failed_external_open_keeps_the_current_surface_visible(self):
        with patch('PySide6.QtGui.QDesktopServices.openUrl',return_value=False):self.shelf.open_path(self.fixture.paths[0])
        self.assertTrue(self.shelf.isVisible());self.assertNotEqual(self.shelf.visible_state,'hiding')

    def test_tray_click_only_presents_menu_and_never_reveals_a_shelf(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from PySide6.QtWidgets import QSystemTrayIcon
        import flickdock as subject
        menu=Mock();menu.isVisible.return_value=False
        manager=SimpleNamespace(tray=SimpleNamespace(contextMenu=lambda:menu),show_first=Mock())
        with patch.object(subject.sys,'platform','win32'):
            ShelfManager.tray_activated(manager,QSystemTrayIcon.Trigger);self.assertEqual(menu.popup.call_count,1)
            menu.isVisible.return_value=True
            ShelfManager.tray_activated(manager,QSystemTrayIcon.DoubleClick);self.assertEqual(menu.popup.call_count,1)
        with patch.object(subject.sys,'platform','darwin'):ShelfManager.tray_activated(manager,QSystemTrayIcon.Trigger)
        self.assertEqual(menu.popup.call_count,1);manager.show_first.assert_not_called()

    def test_status_drag_uses_existing_shake_intake_and_does_not_drop_original_bytes(self):
        from unittest.mock import Mock
        manager=self.fixture.manager;manager.status_drop=Mock();manager.settings_open=lambda:False
        manager.reset_drag_gesture=Mock();manager._status_drag_entered=lambda p:ShelfManager._status_drag_entered(manager,p)
        path=self.fixture.base/'tray-original.txt';path.write_bytes(b'exact original file')
        self.assertTrue(ShelfManager._status_drag_entered(manager,QPoint(500,10)))
        self.assertEqual(self.shelf.presentation_kind,'drag');self.assertTrue(self.shelf.isVisible())
        self.assertFalse(self.shelf.expanded);self.assertIs(manager.auto_shown,self.shelf)
        with patch.object(self.shelf,'receive_mime',wraps=self.shelf.receive_mime) as receive:
            self.assertTrue(ShelfManager._status_drag_dropped(manager,[str(path)],QPoint(500,10)))
            self.assertEqual(receive.call_count,1)
        ws.until(lambda:str(path) in self.shelf.files['permanent'])
        self.assertEqual(path.read_bytes(),b'exact original file')
        self.assertGreaterEqual(manager.status_drop.keep_preview_on_top.call_count,2)
        ShelfManager._status_drag_exited(manager);self.assertIsNone(manager._status_drag_shelf)
        manager.settings_open=lambda:True
        self.assertFalse(ShelfManager._status_drag_entered(manager,QPoint(500,10)))
        self.assertFalse(ShelfManager._status_drag_dropped(manager,[str(path)],QPoint(500,10)))

    def test_partially_pending_selection_cannot_silently_send_only_its_ready_files(self):
        import flickdock as subject
        popup=self.fixture.overview();pending='flickdock:transfer:not-ready'
        self.shelf.content.pending[pending]={'name':'preparing.dat'}
        self.assertNotIn(pending,self.shelf.files['permanent']);self.assertIn(pending,self.shelf.display_refs())
        popup.selected={self.fixture.paths[0],pending};calls=[]
        self.fixture.manager.queue_transfer_paths=lambda *args:calls.append(args)
        def inspect(menu,*_):
            actions={action.text():action for action in menu.actions()}
            for title in ('使用局域网传输','使用远程传输'):
                self.assertFalse(actions[title].isEnabled());actions[title].trigger()
        with patch.object(subject.AnimatedMenu,'exec',inspect):popup.show_item_menu(pending,QPoint())
        self.assertEqual(calls,[])

    def test_manual_hide_holds_translation_velocity_during_an_unfinished_entrance(self):
        from types import SimpleNamespace
        from PySide6.QtTest import QTest
        shelf=self.shelf;manager=self.fixture.manager
        manager.edge=SimpleNamespace(screen_area=lambda:QRect(0,0,1920,1080),geometry=lambda:QRect(1912,300,8,90))
        manager.motion=True;shelf.presentation.motion=True;shelf.pinned=False;shelf.hide_immediately()
        shelf._show_at(QPoint(120,160),'manual',QPoint(20,10));QTest.qWait(48)
        pose=(shelf.presentation.values['dx'],shelf.presentation.values['dy'])
        self.assertGreater(sum(abs(shelf.presentation._velocities[k]) for k in ('dx','dy')),0)
        shelf.hide_animated(explicit=False)
        for _ in range(3):
            QTest.qWait(16)
            self.assertEqual((shelf.presentation.values['dx'],shelf.presentation.values['dy']),pose)

if __name__=='__main__':unittest.main()
