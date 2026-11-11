"""Drag the real entries, preserving original paths and mode service state."""
import tempfile,unittest,threading,time
from unittest.mock import patch
from pathlib import Path
from PySide6.QtCore import QPoint,QPointF,QMimeData,QUrl,Qt
from PySide6.QtGui import QDragEnterEvent,QDragMoveEvent,QDropEvent,QDragLeaveEvent
from PySide6.QtWidgets import QWidget,QPushButton
from test_html_native_workspace import APP,flush,until
from transfer_entry import TransferEntryRouter
from app_settings import SettingsStore
from lan_panel import LanDialog
from test_html_native_lan import FakeLan
from wan_transfer import WanTransferService


class EntryTests(unittest.TestCase):
    def test_only_hovered_transfer_option_animates_its_border(self):
        from transfer_entry import TransferDropSurface
        root=QWidget();root.resize(240,160);surface=TransferDropSurface(root);surface.resize(220,108)
        root.show();surface.show();flush()
        try:
            with patch('native_ui_common.reduced_motion',return_value=False):
                surface.set_target(None)
                self.assertFalse(surface.border_motion.frames.isActive())
                surface.set_target('lan');self.assertTrue(surface.border_motion.frames.isActive())
                with patch('native_ui_common.time.perf_counter',return_value=0):first=surface.grab().toImage()
                with patch('native_ui_common.time.perf_counter',return_value=.1):second=surface.grab().toImage()
                from PySide6.QtCore import QRect
                self.assertNotEqual(first.copy(QRect(0,0,220,52)),second.copy(QRect(0,0,220,52)))
                self.assertEqual(first.copy(QRect(0,55,220,52)),second.copy(QRect(0,55,220,52)))
                surface.set_target(None);self.assertFalse(surface.border_motion.frames.isActive())
        finally:root.hide();root.deleteLater();flush()

    def test_mode_transition_captures_the_painted_old_page_not_an_empty_surface(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);lan=FakeLan();wan=WanTransferService(base/'wan',enabled=False)
            dialog=LanDialog(lan,SettingsStore(base/'profile'));dialog.show();flush()
            try:
                from PySide6.QtCore import QRect
                old=dialog.panel;area=QRect(old.drop_zone.mapTo(dialog.panel_stack,QPoint()),old.drop_zone.size())
                before=dialog.panel_stack.grab().toImage().copy(area)
                with patch('ui_transitions.motion_allowed',return_value=True):dialog.set_service(wan)
                self.assertFalse(dialog._page_fade.snapshot.isNull())
                self.assertEqual(dialog._page_fade.snapshot.toImage().copy(area),before)
                until(lambda:not dialog._page_fade.isVisible())
                self.assertIs(dialog.panel.service,wan);self.assertTrue(dialog.panel.drop_zone.isVisible())
            finally:dialog.hide();dialog.deleteLater();wan.close();wan.deleteLater();flush()

    def test_slow_network_path_information_never_blocks_mode_switch_or_duplicates_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);path=base/'slow.bin';path.write_bytes(b'original')
            lan=FakeLan();wan=WanTransferService(base/'wan',enabled=False)
            dialog=LanDialog(lan,SettingsStore(base/'profile'));dialog.show();dialog.set_service(wan);dialog.set_service(lan);flush()
            entered=threading.Event();release=threading.Event();calls=[];native_stat=Path.stat;main=threading.get_ident()
            def slow_stat(candidate,*args,**kwargs):
                if str(candidate)==str(path):
                    calls.append(threading.get_ident())
                    if threading.get_ident()==main:raise AssertionError('File probe blocked GUI')
                    entered.set();release.wait(3)
                return native_stat(candidate,*args,**kwargs)
            try:
                with patch('file_metadata.Path.stat',side_effect=None,autospec=True) as probe:
                    probe.side_effect=slow_stat
                    dialog.panel.queue_paths([str(path)]);until(entered.is_set)
                    before=time.perf_counter();panel=dialog.set_service(wan)
                    self.assertLess(time.perf_counter()-before,.15)
                    self.assertEqual(panel._queued_paths,[str(path)]);self.assertEqual(len(calls),1)
                    release.set();until(lambda:panel.file_metadata.request(str(path)) is not None)
                self.assertEqual(panel.file_metadata.request(str(path))['size'],len(b'original'))
            finally:release.set();dialog.hide();dialog.deleteLater();wan.close();wan.deleteLater();flush()

    def test_drag_choose_modes_preserves_all_original_paths_and_does_not_import_shelf(self):
        root=QWidget();root.resize(440,420);button=QPushButton('文件互传',root);button.setGeometry(20,360,180,34)
        calls=[];router=TransferEntryRouter(root,button,lambda mode,paths:calls.append((mode,paths)))
        root.show();flush()
        mime=QMimeData();paths=[(Path(tempfile.gettempdir())/'flickdock original'/name).as_posix()
                              for name in ('file one.bin','file two.bin')]
        mime.setUrls([QUrl.fromLocalFile(path) for path in paths])
        try:
            for mode,y in (('lan',25),('wan',77)):
                enter=QDragEnterEvent(button.rect().center(),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier)
                APP.sendEvent(button,enter);self.assertTrue(enter.isAccepted());self.assertTrue(router.surface.isVisible())
                point=QPoint(60,y)
                APP.sendEvent(router.surface,QDragEnterEvent(point,Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier))
                move=QDragMoveEvent(point,Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier)
                APP.sendEvent(router.surface,move);self.assertEqual(router.surface.active,mode)
                drop=QDropEvent(QPointF(point),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier)
                APP.sendEvent(router.surface,drop);self.assertTrue(drop.isAccepted())
                self.assertEqual(calls[-1],(mode,paths));until(lambda:not router.surface.isVisible())
            enter=QDragEnterEvent(button.rect().center(),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier)
            APP.sendEvent(button,enter);APP.sendEvent(button,QDragLeaveEvent());until(lambda:not router.surface.isVisible())
            self.assertEqual(len(calls),2)
            text=QMimeData();text.setText('ordinary shelf content')
            enter=QDragEnterEvent(button.rect().center(),Qt.CopyAction,text,Qt.LeftButton,Qt.NoModifier)
            self.assertFalse(router.eventFilter(button,enter));self.assertFalse(router.surface.isVisible())
        finally:root.hide();root.deleteLater();flush()

    def test_cached_panels_share_pending_files_but_keep_inputs_and_services_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);path=base/'original.bin';path.write_bytes(b'original')
            lan=FakeLan();wan=WanTransferService(base/'wan',enabled=False)
            dialog=LanDialog(lan,SettingsStore(base/'profile'));dialog.show();flush()
            try:
                native=dialog.winId();geometry=dialog.geometry();first=dialog.panel
                first.queue_paths([str(path)]);second=dialog.set_service(wan)
                second.peer_code_input.setText('1234')
                self.assertEqual(second._queued_paths,[str(path)])
                self.assertIs(dialog.set_service(lan),first);self.assertIs(first.service,lan)
                self.assertEqual(first._queued_paths,[str(path)])
                self.assertIs(dialog.set_service(wan),second);self.assertEqual(second.peer_code_input.text(),'1234')
                self.assertEqual(dialog.winId(),native);self.assertEqual(dialog.geometry(),geometry)
                self.assertTrue(dialog.mode_buttons['wan'].isChecked());self.assertFalse(dialog.mode_buttons['lan'].isChecked())
            finally:dialog.hide();dialog.deleteLater();wan.close();wan.deleteLater();flush()


import test_html_native_workspace as workspace_fixture


class DropFeedbackOwnershipTests(unittest.TestCase):
    setUp=workspace_fixture.WorkspaceTests.setUp
    tearDown=workspace_fixture.WorkspaceTests.tearDown
    overview=workspace_fixture.WorkspaceTests.overview

    def test_compact_chooser_clears_and_blocks_the_underlying_empty_target(self):
        self.shelf.files['permanent'].clear();self.shelf.refresh();flush()
        zone=self.shelf.zones['permanent'];router=self.shelf.transfer_entry
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(self.paths[0])])
        self.shelf._highlight_content_drop(True)
        self.assertTrue(zone.icon.border_motion.active)
        APP.sendEvent(self.shelf.lan_button,QDragEnterEvent(QPointF(20,15),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier))
        self.assertTrue(router.surface.isVisible())
        self.assertFalse(zone.drop_highlight);self.assertFalse(zone.icon._hovered)
        self.assertFalse(zone.icon.border_motion.active)
        for y in (25,77):
            APP.sendEvent(router.surface,QDragEnterEvent(QPointF(60,y),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier))
            APP.sendEvent(router.surface,QDragMoveEvent(QPointF(60,y),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier))
            self.shelf._highlight_content_drop(True)
            zone._set_card_hover(True)
            self.assertFalse(zone.icon.border_motion.active)
            self.assertFalse(zone.icon._hovered)
            self.assertEqual(router.surface.active,'lan' if y==25 else 'wan')
        point=zone.mapTo(self.shelf,zone.icon.geometry().center())
        APP.sendEvent(self.shelf,QDragEnterEvent(QPointF(point),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier))
        APP.sendEvent(self.shelf,QDragMoveEvent(QPointF(point),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier))
        self.assertFalse(router.surface.isVisible());self.assertTrue(zone.icon.border_motion.active)
        self.assertFalse(self.events)

    def test_overview_chooser_owns_feedback_until_drag_returns_to_the_shelf(self):
        self.shelf.files['permanent'].clear();self.shelf.refresh();popup=self.overview()
        target=popup.empty_drop;router=popup.transfer_entry
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(self.paths[0])])
        target.set_drag_active(True);self.assertTrue(target.border_motion.active)
        APP.sendEvent(popup.lan_nav,QDragEnterEvent(QPointF(20,15),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier))
        self.assertTrue(router.surface.isVisible());self.assertFalse(target.border_motion.active)
        target.set_drag_active(True);self.assertFalse(target.border_motion.active)
        point=target.mapTo(popup,target.rect().center())
        APP.sendEvent(popup,QDragMoveEvent(QPointF(point),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier))
        self.assertFalse(router.surface.isVisible());self.assertTrue(target.border_motion.active)
        self.assertFalse(self.events)


if __name__=='__main__':unittest.main()
