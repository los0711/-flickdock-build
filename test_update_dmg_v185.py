"""DMG update handoff uses verified bytes and never silently replaces an app."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

import hashlib
from pathlib import Path
import tempfile
import time
import unittest

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from test_update_macos_controller import Client
from update_client import validate_manifest
from update_controller import UpdateController, verify_mac_dmg

APP = QApplication.instance() or QApplication([])


def until(predicate):
    deadline = time.perf_counter() + 3
    while not predicate() and time.perf_counter() < deadline:
        loop = QEventLoop(); QTimer.singleShot(5, loop.quit); loop.exec()
    assert predicate()


class DmgUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='flickdock-dmg-update-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / 'updates/1.8.5/update.dmg'
        self.image.parent.mkdir(parents=True)
        self.image.write_bytes(b'FlickDock disk image' + bytes(2048) + b'koly' + bytes(508))
        self.manifest = dict(schema_version=1, version='1.8.5',
            published_at='2026-10-26T00:00:00Z', notes='DMG update', platform='darwin', architecture='arm64',
            installer=dict(filename='FlickDock-1.8.5-macOS-arm64.dmg',
                url='https://gitee.com/releases/FlickDock-1.8.5-macOS-arm64.dmg',
                size=self.image.stat().st_size, sha256=hashlib.sha256(self.image.read_bytes()).hexdigest()))

    def test_verified_dmg_handoff_opens_native_image_without_quitting(self):
        validate_manifest(self.manifest, {'gitee.com'}, platform='darwin', machine='arm64')
        self.assertEqual(verify_mac_dmg(self.image, self.manifest, current_version='1.8.4',
            updates_dir=self.root/'updates', allowed_hosts={'gitee.com'}, machine='arm64'), self.image)
        launched=[]; client=Client()
        controller=UpdateController('1.8.4', self.root, client=client, platform='darwin', machine='arm64',
            popen=lambda args,**kwargs: launched.append((args,kwargs)),
            quit_callback=lambda: launched.append('quit'))
        try:
            client.offer(self.manifest)
            self.assertTrue(controller.start_update())
            client.complete(self.image,self.manifest)
            until(lambda:controller.state=='handoff')
            self.assertEqual(launched,[(['/usr/bin/open',str(self.image)],{'shell':False})])
            self.assertFalse(controller.busy)
        finally: controller.close()

    def test_corrupt_or_external_dmg_is_never_opened(self):
        outside=self.root/'outside.dmg';outside.write_bytes(self.image.read_bytes())
        with self.assertRaises(ValueError):
            verify_mac_dmg(outside,self.manifest,current_version='1.8.4',
                updates_dir=self.root/'updates',allowed_hosts={'gitee.com'},machine='arm64')
        self.image.write_bytes(self.image.read_bytes()[:-512]+b'BAD!'+bytes(508))
        with self.assertRaises(ValueError):
            verify_mac_dmg(self.image,self.manifest,current_version='1.8.4',
                updates_dir=self.root/'updates',allowed_hosts={'gitee.com'},machine='arm64')


if __name__=='__main__': unittest.main()
