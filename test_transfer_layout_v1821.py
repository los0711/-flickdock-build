"""Actual WAN forms, queue geometry and native-picker lifetime (isolated Qt)."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QRect,QPoint
from test_html_native_workspace import APP,flush,WorkspaceTests
from app_settings import SettingsStore
from lan_panel import LanDialog
from wan_transfer import WanTransferService
from test_html_native_lan import FakeLan


class TransferGeometryTests(unittest.TestCase):
    def test_short_logical_screen_scrolls_right_column_without_overlapping_destination(self):
        with tempfile.TemporaryDirectory() as directory,patch('window_chrome.WindowChrome._available',return_value=QRect(0,0,853,480)):
            base=Path(directory);wan=WanTransferService(base/'wan',enabled=False)
            dialog=LanDialog(FakeLan(),SettingsStore(base/'profile'));dialog.show();flush()
            try:
                for theme in ('dark','light'):
                    dialog.apply_theme(theme)
                    for service in (dialog.panel.service,wan):
                        panel=dialog.set_service(service);flush()
                        history=QRect(panel.history_scroll.mapTo(panel.activity_column,QPoint()),panel.history_scroll.size())
                        destination=QRect(panel.receive_path.mapTo(panel.activity_column,QPoint()),panel.receive_path.size())
                        self.assertFalse(history.intersects(destination))
                        bar=panel.activity_scroll.verticalScrollBar();bar.setValue(bar.maximum());flush()
                        self.assertTrue(panel.activity_scroll.viewport().rect().contains(panel.choose_destination.mapTo(panel.activity_scroll.viewport(),panel.choose_destination.rect().bottomRight())))
            finally:dialog.hide();dialog.deleteLater();wan.close();wan.deleteLater();flush()

    def test_full_forms_queue_rows_never_overlap_and_shadow_is_not_clipped(self):
        with tempfile.TemporaryDirectory() as directory,patch('window_chrome.WindowChrome._available',return_value=QRect(0,0,1920,1080)):
            base=Path(directory);paths=[]
            for i in range(6):
                path=base/f'{i}.bin';path.write_bytes(b'original');paths.append(str(path))
            store=SettingsStore(base/'profile')
            for mode in ('lan','wan'):
                service=FakeLan() if mode=='lan' else WanTransferService(base/'wan',enabled=False)
                dialog=LanDialog(service,store);dialog.show();flush();panel=dialog.panel
                try:
                    for theme in ('dark','light'):
                        dialog.apply_theme(theme)
                        for height in (704,660,800):
                            dialog.resize(980,height);flush()
                            for count in (1,2,3,6,0,2):
                                panel.queue_paths(paths[:count],replace=True);flush()
                                if count:
                                    drop=QRect(panel.drop_zone.mapTo(panel,QPoint()),panel.drop_zone.size())
                                    queue=QRect(panel.queue_scroll.mapTo(panel,QPoint()),panel.queue_scroll.size())
                                    self.assertFalse(drop.intersects(queue))
                                    self.assertLess(panel.queue_area.geometry().bottom(),panel.send_scroll.geometry().top())
                                    for index,row in enumerate(panel.queue_rows):
                                        if index%2:self.assertEqual(row.geometry().top(),panel.queue_rows[index-1].geometry().top())
                                        if index>=2:self.assertLess(panel.queue_rows[index-2].geometry().bottom(),row.geometry().top())
                                    rows=(count+1)//2;self.assertGreaterEqual(panel.queue_body.height(),rows*36+(rows-1)*6)
                            # A short screen must still make every send action reachable.
                            bar=panel.send_scroll.verticalScrollBar();bar.setValue(bar.maximum());flush()
                            bottom=panel.send_button.mapTo(panel.send_scroll.viewport(),panel.send_button.rect().bottomRight())
                            self.assertTrue(panel.send_scroll.viewport().rect().contains(bottom));bar.setValue(0)
                            image=dialog.grab().toImage();width,height_px=image.width(),image.height()
                            for x,y in ((0,height_px//2),(width-1,height_px//2),(width//2,0),(width//2,height_px-1)):self.assertEqual(image.pixelColor(x,y).alpha(),0)
                finally:
                    dialog.hide();dialog.deleteLater();flush()
                    if mode=='wan':service.close();service.deleteLater();flush()


class PickerLifetimeTests(unittest.TestCase):
    setUp=WorkspaceTests.setUp
    tearDown=WorkspaceTests.tearDown

    def test_cancel_or_choose_preserves_mode_and_gives_return_grace(self):
        shelf=self.shelf
        for expanded in (False,True):
            if expanded:shelf.show_contents()
            before=(shelf.expanded,shelf.presentation_kind)
            def choose(*_):
                self.assertTrue(shelf.picker_hold_active());self.assertTrue(shelf.isVisible())
                self.assertEqual((shelf.expanded,shelf.presentation_kind),before)
                return [],''
            with patch('flickdock.QFileDialog.getOpenFileNames',side_effect=choose),patch('flickdock.time.monotonic',return_value=100):shelf.pick_files('permanent')
            self.assertFalse(shelf._file_choosing);self.assertTrue(shelf.picker_hold_active(101.49));self.assertFalse(shelf.picker_hold_active(101.51))
            self.assertEqual((shelf.expanded,shelf.presentation_kind),before)
        with patch('flickdock.QFileDialog.getOpenFileNames',side_effect=RuntimeError('picker unavailable')):
            with self.assertRaises(RuntimeError):shelf.pick_files('permanent')
        self.assertFalse(shelf._file_choosing)


if __name__=='__main__':unittest.main()
