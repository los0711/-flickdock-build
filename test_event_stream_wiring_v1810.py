"""Offscreen check that native socket state reaches the sync fallback policy."""
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
        with tempfile.TemporaryDirectory(prefix='flickdock-live-progress-') as folder:
            account=Account();account.api=object();account.device_id='receiver'
            stream=CloudEvents(account,enabled=False)
            adapter=SimpleNamespace(snapshot=lambda:dict(permanent=[],texts={},metadata={}))
            # Exercise the real initialized journal, including first-arrival
            # persistence. Bypassing __init__ omitted ownership and lane state.
            engine=ShelfSync(folder,account,adapter)
            identifier=str(uuid4());engine.account_id='fixture';engine.upload_enabled=False
            engine.remote_transfers=[dict(upload_id=identifier,item_id='item',size=1000000,received_bytes=0,device_id='sender')]
            engine.state={'entries':{}}
            gets=[];engine.request_refresh=lambda:gets.append(True);seen=[];engine.transfers_changed.connect(seen.append)
            stream.transfer_progress.connect(engine.remote_progress)
            try:
                for received in (100000,300000,700000):stream._message(json.dumps(dict(schema_version=1,type='progress',upload_id=identifier,received_bytes=received,total=1000000,at=received)))
                self.assertEqual([row[0]['received'] for row in seen],[100000,300000,700000]);self.assertEqual(gets,[])
                arrivals=[row[0]['shelf_added_at'] for row in seen]
                self.assertEqual(len(set(arrivals)),1)
                journal=json.loads(engine._profile('fixture').read_text(encoding='utf8'))
                self.assertEqual(journal['remote_arrivals']['item'],arrivals[0])
                stream._message(json.dumps(dict(schema_version=1,type='progress',upload_id=identifier,received_bytes=2000000,total=1000000,at=800000)))
                self.assertEqual(len(seen),3)
            finally:stream.close();engine.close();stream.deleteLater();engine.deleteLater();APP.processEvents()

    def test_connected_and_disconnected_change_sync_poll_policy(self):
        with tempfile.TemporaryDirectory(prefix='flickdock-events-') as folder, \
             patch.dict(os.environ, {'APPDATA': str(Path(folder) / 'profile')}), \
             patch.object(QSystemTrayIcon, 'show'), patch.object(QSystemTrayIcon, 'hide'), \
             patch.object(app_settings, '_read_startup_value'), \
             patch.object(app_settings, '_write_startup_value'):
            manager = flickdock.ShelfManager(APP)
            manager.timer.stop()
            try:
                self.assertFalse(manager.sync._event_stream_online)
                manager.events.socket.connected.emit()
                self.assertTrue(manager.sync._event_stream_online)
                manager.events.socket.disconnected.emit()
                self.assertFalse(manager.sync._event_stream_online)
            finally:
                manager.quit()
                APP.processEvents()


if __name__ == '__main__':
    unittest.main(verbosity=2)
