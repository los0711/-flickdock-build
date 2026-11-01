"""Actual child feedback and Qt drop entry; disposable data, no desktop/native input."""
import tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from PySide6.QtCore import QEvent,QMimeData,QPoint,QPointF,QUrl,Qt
from PySide6.QtGui import QDragEnterEvent,QDragMoveEvent,QDropEvent,QFocusEvent
from PySide6.QtWidgets import QWidget,QPushButton
from test_html_native_workspace import APP,flush,until
from test_html_native_lan import FakeLan
from app_settings import SettingsStore
from lan_panel import LanDialog
from transfer_entry import TransferEntryRouter
from transfer_drop_feedback import DropFeedback


def mime(paths):
    value=QMimeData();value.setUrls([QUrl.fromLocalFile(path) for path in paths]);return value


class EntryFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.root=QWidget();self.root.resize(440,420)
        self.root._set_stack_spread=Mock();self.root._restore_stack_spread=Mock()
        self.button=QPushButton('互传',self.root);self.button.setGeometry(20,360,180,34)
        self.queued=[];self.presented=[]
        def deliver(mode,paths):
            self.queued.extend(path for path in paths if path not in self.queued)
            return lambda:self.presented.append(mode)
        self.router=TransferEntryRouter(self.root,self.button,deliver);self.root.show();flush()
        self.motion=patch('transfer_drop_feedback.motion_allowed',return_value=True);self.motion.start()
        self.reduced=patch('transfer_entry.reduced_motion',return_value=False);self.reduced.start()
    def tearDown(self):
        self.root.hide();self.root.deleteLater();flush();self.motion.stop();self.reduced.stop()
    def drop(self,paths,mode='lan'):
        data=mime(paths)
        APP.sendEvent(self.button,QDragEnterEvent(QPoint(20,20),Qt.CopyAction,data,Qt.LeftButton,Qt.NoModifier))
        event=QDropEvent(QPointF(30,25 if mode=='lan' else 95),Qt.CopyAction,data,Qt.LeftButton,Qt.NoModifier)
        self.router.eventFilter(self.router.surface,event);self.assertTrue(event.isAccepted())

    def test_latest_drop_queues_immediately_and_presents_only_latest_mode(self):
        self.drop(['/not-mounted/image.png']);self.assertEqual(self.queued,['/not-mounted/image.png'])
        self.assertEqual(self.presented,[]);self.assertEqual(self.router.drop_feedback.type_label,'PNG')
        self.drop(['/not-mounted/archive.zip'],'wan')
        self.assertEqual(self.queued,['/not-mounted/image.png','/not-mounted/archive.zip'])
        self.assertEqual(self.presented,[]);self.assertEqual(self.router.drop_feedback.type_label,'ZIP')
        self.router.drop_feedback.animation.setCurrentTime(220);flush()
        self.assertEqual(self.presented,['wan']);self.assertFalse(self.router.surface.isVisible())

    def test_hidden_owner_cancels_pending_presentation_and_reduced_motion_is_immediate(self):
        self.drop(['/not-mounted/image.png']);self.root.hide();flush()
        self.router.drop_feedback.animation.setCurrentTime(220);self.router._finish_drop()
        self.assertEqual(self.presented,[])
        self.root.show();flush()
        with patch('transfer_entry.reduced_motion',return_value=True):self.drop(['/not-mounted/archive.zip'],'wan')
        self.assertEqual(self.presented,['wan']);self.assertFalse(self.router.drop_feedback.isVisible())

    def test_hover_restores_through_shelf_hit_test_and_drag_outside_entry_spreads(self):
        self.router.eventFilter(self.button,QEvent(QEvent.Enter))
        self.root._set_stack_spread.assert_called_with(False)
        self.router.eventFilter(self.button,QEvent(QEvent.Leave));flush()
        self.root._restore_stack_spread.assert_called_once()
        self.root._set_stack_spread.reset_mock()
        data=mime(['/not-mounted/file.png'])
        event=QDragMoveEvent(QPoint(340,150),Qt.CopyAction,data,Qt.LeftButton,Qt.NoModifier)
        self.router.eventFilter(self.root,event);self.root._set_stack_spread.assert_called_with(True)
        self.router.eventFilter(self.root,QEvent(QEvent.Leave));self.root._set_stack_spread.assert_called_with(False)
        self.root._restore_stack_spread.reset_mock()
        self.router.eventFilter(self.button,QEvent(QEvent.Leave))
        self.router.eventFilter(self.root,QEvent(QEvent.Leave));flush()
        self.root._restore_stack_spread.assert_not_called()

    def test_icon_is_cached_once_and_arc_bends_above_linear_path(self):
        from flickdock_icons import document_pixmap
        with patch('flickdock_icons.document_pixmap',wraps=document_pixmap) as render,patch.object(Path,'stat',side_effect=AssertionError('Feedback must not stat')):
            self.drop(['/not-mounted/image.png'])
            layer=self.router.drop_feedback
            for time in (20,60,100,150):
                layer.animation.setCurrentTime(time);layer.grab()
            t=layer.value;linear=layer.origin*(1-t)+layer.target*t
            self.assertLess(layer.point().y(),linear.y());self.assertEqual(render.call_count,1)

    def test_resize_cancels_only_artwork_and_presents_current_queued_intent_once(self):
        self.drop(['/not-mounted/image.png']);self.root.resize(460,430);flush()
        self.assertEqual(self.presented,['lan']);self.assertFalse(self.router._dropping)
        self.assertFalse(self.router.surface.isVisible());self.assertIsNone(self.router._pending_present)
        self.root.resize(480,440);flush();self.assertEqual(self.presented,['lan'])

    def test_resize_callback_cannot_present_a_newer_drop_or_reopen_a_hidden_owner(self):
        self.drop(['/not-mounted/image.png']);self.root.resize(460,430)
        self.drop(['/not-mounted/archive.zip'],'wan');flush();self.assertEqual(self.presented,[])
        self.router.drop_feedback.animation.setCurrentTime(220);flush();self.assertEqual(self.presented,['wan'])
        self.drop(['/not-mounted/another.txt']);self.root.resize(480,440);self.root.hide();flush()
        self.assertEqual(self.presented,['wan'])


class WindowFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.service=FakeLan();self.dialog=LanDialog(self.service,SettingsStore(self.root/'settings'))
        self.motion=patch('transfer_drop_feedback.motion_allowed',return_value=True);self.motion.start()
    def tearDown(self):
        self.dialog.hide();self.dialog.deleteLater();flush();self.motion.stop();self.temp.cleanup()
    def paths(self):
        values=[]
        for name in ('image.png','notes.txt'):
            path=self.root/name;path.write_bytes(b'original unchanged');values.append(str(path))
        return values

    def test_window_drop_has_type_feedback_and_rows_scale_at_actual_layout_geometry(self):
        self.dialog.show();flush();native=self.dialog.winId();geometry=self.dialog.geometry()
        paths=self.paths();data=mime(paths);target=self.dialog.panel.send_column
        event=QDropEvent(QPointF(35,45),Qt.CopyAction,data,Qt.LeftButton,Qt.NoModifier)
        self.dialog.drop_router.eventFilter(target,event)
        self.assertEqual(self.dialog.panel._queued_paths,paths);self.assertEqual(self.service.calls,[])
        layer=self.dialog.drop_feedback;self.assertEqual(layer.type_label,'PNG')
        self.assertEqual(layer.target,QPointF(target.mapTo(self.dialog,QPoint(35,45))))
        self.dialog.panel.queue_area.animation.setCurrentTime(200);flush()
        rows=self.dialog.panel.queue_rows;self.assertEqual(len(rows),2)
        for row in rows:
            rect=row.geometry();row.entrance.animation.setCurrentTime(80)
            self.assertGreater(row.entrance.value,0);self.assertLess(row.entrance.value,1)
            row.grab();self.assertEqual(row.geometry(),rect)
        self.assertEqual(self.dialog.geometry(),geometry);self.assertEqual(self.dialog.winId(),native)
        self.assertTrue(all(Path(path).read_bytes()==b'original unchanged' for path in paths))
        self.dialog.hide();flush();self.assertFalse(layer.isVisible())
        self.assertTrue(all(row.entrance.animation.state()==row.entrance.animation.State.Stopped for row in rows))

    def test_prepared_rows_wait_for_show_and_passive_reveal_never_shows_again(self):
        self.dialog.panel.queue_paths(self.paths());self.assertTrue(all(row.entrance.pending for row in self.dialog.panel.queue_rows))
        with patch.object(self.dialog,'show',wraps=self.dialog.show) as show,patch('platform_native.raise_shelf_for_reveal',return_value=False):
            self.dialog.show();flush();geometry=self.dialog.geometry();native=self.dialog.winId()
            self.dialog.present_dropped_files();self.dialog.present_dropped_files();flush()
            self.assertEqual(show.call_count,1)
            self.assertTrue(all(not row.entrance.pending for row in self.dialog.panel.queue_rows))
            self.assertEqual(self.dialog.geometry(),geometry);self.assertEqual(self.dialog.winId(),native)

    def test_reduced_motion_keeps_queue_and_does_not_start_feedback_clocks(self):
        with patch('transfer_drop_feedback.motion_allowed',return_value=False):
            self.dialog.panel.queue_paths(self.paths());self.dialog.show();flush()
            with patch('platform_native.raise_shelf_for_reveal',return_value=False):self.dialog.present_dropped_files()
            self.assertEqual(len(self.dialog.panel._queued_paths),2)
            self.assertTrue(all(not row.entrance.isEnabled() for row in self.dialog.panel.queue_rows))

    def test_bulk_queue_does_not_animate_clipped_rows(self):
        self.dialog.show();flush();paths=[]
        for index in range(24):
            path=self.root/f'{index}.txt';path.write_text('original');paths.append(str(path))
        self.dialog.panel.queue_paths(paths);self.dialog.panel.queue_area.animation.setCurrentTime(200);flush()
        running=[row for row in self.dialog.panel.queue_rows if row.entrance.isEnabled()]
        self.assertGreater(len(running),0);self.assertLessEqual(len(running),4)
        self.assertEqual(self.dialog.panel._queued_paths,paths)

    def test_all_remote_inputs_get_focus_feedback_without_changing_values_or_selection(self):
        from wan_transfer import WanTransferService
        wan=WanTransferService(self.root/'wan',enabled=False)
        try:
            panel=self.dialog.set_service(wan);self.dialog.show();flush()
            for theme in ('light','dark'):
                self.dialog.apply_theme(theme);flush()
                for edit in (panel.peer_code_input,panel.custom_input,panel.receive_path):
                    before=edit.text();edit.setSelection(0,min(2,len(before)));selection=edit.selectedText()
                    control=edit._input_focus_motion
                    self.assertEqual(control.theme(),theme)
                    with patch('native_ui_common.reduced_motion',return_value=False):
                        self.assertFalse(control.eventFilter(edit,QFocusEvent(QEvent.FocusIn,Qt.MouseFocusReason)))
                        control.animation.setCurrentTime(50)
                        self.assertGreater(control.value,0);self.assertLessEqual(control.value,1)
                        self.assertFalse(control.eventFilter(edit,QFocusEvent(QEvent.FocusOut,Qt.MouseFocusReason)))
                    self.assertEqual(edit.text(),before);self.assertEqual(edit.selectedText(),selection)
                    edit.setProperty('validationError',True)
                    self.assertFalse(edit.property('focusMotion'));self.assertEqual(control.value,0)
                    edit.setProperty('validationError',False)
        finally:wan.close();wan.deleteLater();flush()


class ShelfEntryIntegrationTests(unittest.TestCase):
    def test_preview_and_overview_deliver_into_real_manager_queue_before_presentation(self):
        from flickdock import ShelfManager
        from wan_transfer import WanTransferService
        from test_html_native_workspace import WorkspaceTests
        fixture=WorkspaceTests('runTest');fixture.setUp();manager=fixture.manager
        manager.lan=FakeLan();manager.wan=WanTransferService(fixture.base/'wan',enabled=False)
        manager.lan_dialog=manager.wan_dialog=None
        manager._lan_selected=lambda:[];manager._center_dialog=lambda _:None
        manager._use_recommended_lan=lambda *_:None;manager.show_shortcut=lambda:None
        manager._show_transfer_dialog=lambda service,**kwargs:ShelfManager._show_transfer_dialog(manager,service,**kwargs)
        manager.queue_transfer_paths=lambda mode,paths,**kwargs:ShelfManager.queue_transfer_paths(manager,mode,paths,**kwargs)
        try:
            with patch('transfer_drop_feedback.motion_allowed',return_value=True),patch('transfer_entry.reduced_motion',return_value=False),patch('platform_native.raise_shelf_for_reveal',return_value=False):
                fixture.shelf.presentation_kind='dock';fixture.shelf._layout_header(fixture.shelf.card.width());flush()
                for mode,make_router in (('lan',lambda:fixture.shelf.transfer_entry),('wan',lambda:fixture.overview().transfer_entry)):
                    router=make_router()
                    data=mime([fixture.paths[0]])
                    APP.sendEvent(router.button,QDragEnterEvent(QPoint(15,15),Qt.CopyAction,data,Qt.LeftButton,Qt.NoModifier))
                    self.assertTrue(router.surface.isVisible(),mode)
                    APP.sendEvent(router.surface,QDragEnterEvent(QPoint(30,25 if mode=='lan' else 95),Qt.CopyAction,data,Qt.LeftButton,Qt.NoModifier))
                    drop=QDropEvent(QPointF(30,25 if mode=='lan' else 95),Qt.CopyAction,data,Qt.LeftButton,Qt.NoModifier)
                    APP.sendEvent(router.surface,drop)
                    self.assertTrue(drop.isAccepted());self.assertEqual(manager.lan_dialog.panel._queued_paths,[fixture.paths[0]])
                    self.assertFalse(manager.lan_dialog.isVisible())
                    router.drop_feedback.animation.setCurrentTime(220);flush()
                    self.assertTrue(manager.lan_dialog.isVisible())
                    self.assertIs(manager.lan_dialog.panel.service,manager.wan if mode=='wan' else manager.lan)
                    manager.lan_dialog.hide();flush()
                self.assertEqual(Path(fixture.paths[0]).read_bytes(),b'original 0')
        finally:
            if manager.lan_dialog:manager.lan_dialog.hide();manager.lan_dialog.deleteLater();flush()
            manager.wan.close();manager.wan.deleteLater();flush();fixture.tearDown()


if __name__=='__main__':unittest.main()
