"""The existing preview action exposes an honest retry for a stopped download."""
from types import SimpleNamespace
import unittest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
import test_html_native_workspace as workspace


class DownloadFailureUiTests(unittest.TestCase):
    setUp=workspace.WorkspaceTests.setUp
    tearDown=workspace.WorkspaceTests.tearDown
    overview=workspace.WorkspaceTests.overview

    def test_failed_download_has_no_busy_overlay_and_existing_button_retries(self):
        ref=self.paths[0];identifier='owned-failed-item';retried=[]
        self.manager.sync=SimpleNamespace(download_transfers={identifier:{}},retry_download=retried.append)
        self.shelf.content.transfers[ref]=dict(id=identifier,phase='error',remote=True,
            total=100,received=40,message='保存位置不可写（DOWNLOAD_SAVE_FAILED）')
        preview=self.overview().preview
        preview.show_path(ref,force=True);workspace.flush()
        self.assertEqual(preview.cancel_transfer.text(),'重试下载')
        self.assertTrue(preview.cancel_transfer.isVisible())
        self.assertFalse(preview.transfer_progress.isVisible())
        self.assertFalse(preview.transfer_overlay.isVisible())
        self.assertIn('保存位置不可写',preview.transfer_meta.text())
        QTest.mouseClick(preview.cancel_transfer,Qt.LeftButton);workspace.flush()
        self.assertEqual(retried,[identifier])
        self.shelf.transfers_updated();workspace.flush()
        self.assertFalse(self.shelf._transfer_timer.isActive())
        self.assertIn('下载失败',self.shelf.popup.transfer_count.text())
        self.assertEqual(self.shelf.popup.transfer_eta.text(),'已停止自动重试')


if __name__=='__main__':unittest.main()
