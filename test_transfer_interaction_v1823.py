"""Transfer mode group and Paste use real Qt input, with no native hooks."""
import tempfile,unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
from PySide6.QtCore import QMimeData,QUrl,Qt,QPoint
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from test_html_native_workspace import APP,flush,until
from test_html_native_lan import FakeLan
from wan_transfer import WanTransferService
from app_settings import SettingsStore
from lan_panel import LanDialog


class TransferInteractionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.lan=FakeLan();self.wan=WanTransferService(self.base/'wan',enabled=False)
        self.dialog=LanDialog(self.lan,SettingsStore(self.base/'profile'));self.dialog.show();flush()
        self.dialog.mode_selected.connect(lambda mode:self.dialog.set_service(self.wan if mode=='wan' else self.lan))
    def tearDown(self):
        APP.clipboard().clear();flush()
        self.dialog.hide();self.dialog.deleteLater();self.wan.close();self.wan.deleteLater();flush();self.temp.cleanup()
    def copy_files(self):
        paths=[]
        for index in range(2):
            path=self.base/f'original{index}.bin';path.write_bytes(b'original');paths.append(str(path))
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(path) for path in paths]);APP.clipboard().setMimeData(mime)
        return paths
    def paste(self,widget):QTest.keySequence(widget,QKeySequence(QKeySequence.StandardKey.Paste));flush()

    def test_modes_share_one_container_and_keep_exactly_one_selection(self):
        segment=self.dialog.mode_segment;lan=self.dialog.mode_buttons['lan'];wan=self.dialog.mode_buttons['wan']
        self.assertIs(lan.parentWidget(),segment);self.assertIs(wan.parentWidget(),segment)
        self.assertIs(segment.parentWidget(),self.dialog.mode_bar)
        self.assertGreater(self.dialog.title_label.width(),self.dialog.title_label.fontMetrics().horizontalAdvance('文件互传'))
        self.assertGreater(self.dialog.mode_bar.geometry().top(),self.dialog.title_bar.geometry().bottom())
        self.assertGreater(self.dialog.panel_stack.geometry().top(),self.dialog.mode_bar.geometry().bottom())
        self.assertEqual(segment.layout().spacing(),0)
        native=self.dialog.winId()
        for button in (wan,wan,lan,lan,wan):
            QTest.mouseClick(button,Qt.LeftButton);flush()
            self.assertEqual(sum(control.isChecked() for control in (lan,wan)),1)
            self.assertEqual(self.dialog.winId(),native)
            self.assertEqual(wan.isChecked(),self.dialog.panel.service is self.wan)
        self.assertTrue(segment.rect().contains(lan.geometry()));self.assertTrue(segment.rect().contains(wan.geometry()))

    def test_paste_queues_original_files_once_in_both_modes(self):
        paths=self.copy_files();mime=APP.clipboard().mimeData();sequence=bytes(mime.data('text/uri-list'))
        for mode in ('lan','wan'):
            self.dialog.mode_buttons[mode].click();flush()
            self.paste(self.dialog.panel.drop_zone);self.paste(self.dialog.panel.drop_zone)
            self.assertEqual(self.dialog.panel._queued_paths,paths)
            self.assertEqual(bytes(APP.clipboard().mimeData().data('text/uri-list')),sequence)
            self.assertTrue(all(Path(path).read_bytes()==b'original' for path in paths))

    def test_text_paste_in_receiver_code_remains_native_and_hidden_window_cannot_queue(self):
        self.dialog.mode_buttons['wan'].click();flush();edit=self.dialog.panel.peer_code_input
        APP.clipboard().setText('1234');edit.setFocus();self.paste(edit)
        self.assertEqual(edit.text(),'1234');self.assertEqual(self.dialog.panel._queued_paths,[])
        paths=self.copy_files();self.dialog.hide();self.paste(self.dialog.panel.drop_zone)
        self.assertEqual(self.dialog.panel._queued_paths,[])

    def test_explicit_reopen_cancels_fading_back_without_changing_window_size(self):
        from flickdock import ShelfManager
        seen=[];self.dialog.overview_requested.connect(lambda:seen.append('overview'))
        manager=SimpleNamespace(shelf=SimpleNamespace(isVisible=lambda:False,_release_reveal_level=lambda:None),
            lan_dialog=self.dialog,wan_dialog=self.dialog,_lan_selected=lambda:[],_center_dialog=lambda _:None)
        with patch.object(self.dialog._window_fade,'enabled',return_value=True):
            self.dialog.resize(1020,750);before=self.dialog.size();native=self.dialog.winId()
            self.dialog.back_button.click();self.assertIsNotNone(self.dialog._window_fade.pending)
            ShelfManager._show_transfer_dialog(manager,self.lan)
            self.assertIsNone(self.dialog._window_fade.pending)
            QTest.qWait(240);flush()
            self.assertTrue(self.dialog.isVisible());self.assertEqual(seen,[])
            self.assertEqual(self.dialog.size(),before);self.assertEqual(self.dialog.winId(),native)

    def test_back_emits_only_after_all_close_handlers_have_finished(self):
        order=[]
        self.dialog.overview_requested.connect(lambda:order.append('overview'))
        self.dialog.finished.connect(lambda _:order.append('finished-tail'))
        self.dialog.back_button.click();until(lambda:'overview' in order)
        self.assertEqual(order,['finished-tail','overview'])

    def test_empty_file_url_cannot_queue_the_working_directory(self):
        mime=QMimeData();mime.setUrls([QUrl('file:')]);APP.clipboard().setMimeData(mime)
        self.paste(self.dialog.panel.drop_zone);self.assertEqual(self.dialog.panel._queued_paths,[])


if __name__=='__main__':unittest.main()
