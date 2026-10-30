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


if __name__=='__main__':unittest.main()
