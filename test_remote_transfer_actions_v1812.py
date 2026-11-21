"""Offscreen actions for downloads whose transfer phase changes after I/O."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
_import_profile = tempfile.TemporaryDirectory(prefix='flickdock-remote-import-')
os.environ['APPDATA'] = _import_profile.name

from PySide6.QtWidgets import QApplication, QSystemTrayIcon

import app_settings
import flickdock


APP = QApplication.instance() or QApplication([])


class RemoteTransferActionsTests(unittest.TestCase):
    def setUp(self):
        self.profile = tempfile.TemporaryDirectory(prefix='flickdock-remote-actions-')
        self.patches = [
            patch.dict(os.environ, {'APPDATA': str(Path(self.profile.name) / 'profile')}),
            patch.object(flickdock, 'BASE', Path(self.profile.name) / 'state'),
            patch.object(flickdock, 'STATE_FILE', Path(self.profile.name) / 'state' / 'shelves.json'),
            patch.object(flickdock, 'OLD_STATE_FILE', Path(self.profile.name) / 'old' / 'shelves.json'),
            patch.object(QSystemTrayIcon, 'show'),
            patch.object(QSystemTrayIcon, 'hide'),
            patch.object(app_settings, '_read_startup_value'),
            patch.object(app_settings, '_write_startup_value'),
        ]
        for item in self.patches:
            item.start()
        self.manager = flickdock.ShelfManager(APP)
        self.manager.timer.stop()
        self.shelf = self.manager.shelf

    def tearDown(self):
        self.manager.quit()
        APP.processEvents()
        for item in reversed(self.patches):
            item.stop()
        self.profile.cleanup()

    def _set_transfer(self, identity, phase, *, remote=True):
        ref = 'flickdock:transfer:' + identity
        self.shelf.content.pending[ref] = dict(name='remote.bin', kind='file')
        self.shelf.content.transfers[ref] = dict(id=identity, remote=remote, phase=phase)
        return ref

    def test_cancel_receive_uses_download_ownership_in_every_phase(self):
        for phase in ('downloading', 'validating', 'retrying'):
            with self.subTest(phase=phase):
                identity = 'receive-' + phase
                ref = self._set_transfer(identity, phase)
                self.manager.sync.download_transfers[identity] = dict(version=1)
                with patch.object(self.manager.sync, 'cancel_download') as download, \
                     patch.object(self.manager.sync, 'cancel_upload') as upload:
                    self.shelf.cancel_transfer(ref)
                download.assert_called_once_with(identity)
                upload.assert_not_called()

    def test_pending_remote_upload_still_uses_upload_cancel(self):
        ref = self._set_transfer('pending-other-device', 'remote')
        with patch.object(self.manager.sync, 'cancel_download') as download, \
             patch.object(self.manager.sync, 'cancel_upload') as upload:
            self.shelf.cancel_transfer(ref)
        download.assert_not_called()
        upload.assert_called_once_with('pending-other-device')

    def test_delete_retrying_receive_removes_cloud_item(self):
        identity = 'retrying-item'
        ref = self._set_transfer(identity, 'retrying')
        self.manager.sync.download_transfers[identity] = dict(version=1)
        with patch.object(self.manager.sync, 'cancel_download') as download, \
             patch.object(self.manager.sync, 'cancel_upload') as upload:
            self.shelf.remove_path('permanent', ref)
        download.assert_called_once_with(identity, remove=True)
        upload.assert_not_called()

    def test_clear_all_cancels_receive_once_even_when_ref_is_also_local(self):
        identity = 'validating-item'
        ref = Path(self.profile.name) / 'file.bin'
        ref.write_bytes(b'content')
        self.shelf.files['permanent'].append(str(ref))
        self.shelf.content.transfers[str(ref)] = dict(id=identity, remote=True, phase='validating')
        self.manager.sync.download_transfers[identity] = dict(version=1)
        with patch.object(self.manager.sync, 'cancel_download') as download, \
             patch.object(self.manager.sync, 'cancel_upload') as upload, \
             patch.object(self.manager, 'save'):
            self.shelf.remove_all('permanent')
        download.assert_called_once_with(identity, remove=True)
        upload.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
