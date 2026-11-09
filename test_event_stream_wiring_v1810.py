"""Offscreen check that native socket state reaches the shared push-only scheduler."""
import os
from pathlib import Path
import tempfile
import unittest
import json
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from PySide6.QtWidgets import QApplication, QSystemTrayIcon

import app_settings
import flickdock


APP = QApplication.instance() or QApplication([])


class EventStreamWiringTests(unittest.TestCase):
    def test_validated_live_progress_reaches_the_other_shelf_without_a_changefeed_get(self):
        from types import SimpleNamespace
        from uuid import uuid4
        from cloud_events import CloudEvents
        from shelf_sync import ShelfSync
        from PySide6.QtCore import QObject,Signal
        class Account(QObject):
            account_changed=Signal(object)
        account=Account();stream=CloudEvents(account,enabled=False);engine=ShelfSync.__new__(ShelfSync);QObject.__init__(engine)
        identifier=str(uuid4());engine.closed=False;engine.account_id='fixture';engine.receive_enabled=True;engine.upload_enabled=False
        engine._live_upload_progress={};engine._remote_heads={};engine.remote_transfers=[dict(upload_id=identifier,item_id='item',size=1000000,received_bytes=0,device_id='sender')]
        engine.service=SimpleNamespace(device_id='receiver',signed_in=True);engine.state={'entries':{}};engine.download_transfers={}
        from sync_scheduler import SyncScheduler
        scheduler=SyncScheduler();engine.schedule=scheduler.register('shelf',lambda:None)
        gets=[];engine.request_refresh=lambda:gets.append(True);seen=[];engine.transfers_changed.connect(seen.append)
        stream.transfer_progress.connect(engine.remote_progress)
        try:
            for received in (100000,300000,700000):stream._message(json.dumps(dict(schema_version=1,type='progress',upload_id=identifier,received_bytes=received,total=1000000,at=received)))
            self.assertEqual([row[0]['received'] for row in seen],[100000,300000,700000]);self.assertEqual(gets,[])
            stream._message(json.dumps(dict(schema_version=1,type='progress',upload_id=identifier,received_bytes=2000000,total=1000000,at=800000)))
            self.assertEqual(len(seen),3)
        finally:scheduler.close();stream.close();stream.deleteLater();engine.deleteLater();APP.processEvents()

    def test_connected_and_disconnected_reach_the_shared_push_scheduler(self):
        with tempfile.TemporaryDirectory(prefix='flickdock-events-') as folder, \
             patch.dict(os.environ, {'APPDATA': str(Path(folder) / 'profile')}), \
             patch.object(QSystemTrayIcon, 'show'), patch.object(QSystemTrayIcon, 'hide'), \
             patch.object(app_settings, '_read_startup_value'), \
             patch.object(app_settings, '_write_startup_value'):
            manager = flickdock.ShelfManager(APP)
            manager.timer.stop()
            try:
                self.assertIs(manager.sync.scheduler,manager.clipboard_sync.scheduler)
                self.assertFalse(manager.sync_scheduler.connected)
                self.assertFalse(manager.sync_scheduler.timer.isActive())
                manager.events.socket.connected.emit()
                self.assertTrue(manager.sync_scheduler.connected)
                manager.events.socket.disconnected.emit()
                self.assertFalse(manager.sync_scheduler.connected)
            finally:
                manager.quit()
                APP.processEvents()


if __name__ == '__main__':
    unittest.main(verbosity=2)
