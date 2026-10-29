"""Anonymous signaling failure/recovery, trusted alternate origin and UI retry."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QCoreApplication,QEvent,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from wan_transfer import WanTransferService,SIGNAL_ORIGINS
from lan_panel import LanDialog
import app_settings
APP=QApplication.instance() or QApplication([])

class SignalingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.service=WanTransferService(self.base,enabled=True);self.messages=[]
        self.service.status_changed.connect(self.messages.append)
    def tearDown(self):
        self.service.close();self.service.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);APP.processEvents();self.temp.cleanup()
    def test_initial_handshake_has_a_deadline_and_switches_to_verified_alternate_origin(self):
        opened=[]
        with patch.object(self.service.socket,'open',side_effect=lambda request:opened.append(request.url().toString())):
            self.service._connect();self.assertTrue(self.service.connect_timeout.isActive())
            self.service._connection_timed_out();self.service.retry.stop();self.service._connect()
        self.assertEqual(opened,['wss://'+value.removeprefix('https://')+'/v1/wan/connect' for value in SIGNAL_ORIGINS])
        self.assertTrue(any('超时' in value for value in self.messages))
    def test_error_is_visible_and_code_control_allows_retry_without_login(self):
        dialog=LanDialog(self.service,app_settings.SettingsStore(self.base/'settings'));dialog.show();APP.processEvents()
        try:
            with patch.object(self.service.socket,'errorString',return_value='Host not found'):
                self.service._socket_error()
            APP.processEvents();button=dialog.panel.send_code_value
            self.assertEqual(button.text(),'重试连接');self.assertTrue(button.isEnabled())
            self.assertIn('Host not found',dialog.panel.status_label.text())
            with patch.object(self.service,'reconnect') as retry:QTest.mouseClick(button,Qt.LeftButton);retry.assert_called_once()
            self.assertEqual(self.service._credential,(self.base/'wan-device.key').read_text())
        finally:dialog.close();dialog.deleteLater();APP.processEvents()
    def test_successful_identity_stops_deadline_and_resets_backoff(self):
        self.service._connected=True;self.service._connect_attempt=6;self.service._connection_error='previous error';self.service.connect_timeout.start()
        self.service._message(json.dumps(dict(schema_version=1,type='identity',code='ABCD2345',recent=[],serverNow=0,expires=300000,refreshAt=240000)))
        self.assertFalse(self.service.connect_timeout.isActive());self.assertEqual(self.service._connect_attempt,0)
        self.assertEqual(self.service.code_identity()['code'],'ABCD2345');self.assertFalse(self.service.code_identity()['error'])
    def test_repeated_failures_back_off_instead_of_consuming_a_shared_nat_hourly_allowance(self):
        waits=[]
        for _ in range(12):
            self.service.retry.stop();self.service._disconnected();waits.append(self.service.retry.interval())
        self.assertEqual(waits[0],250);self.assertGreaterEqual(waits[-1],300000)
    def test_untrusted_signal_origin_never_receives_anonymous_credential(self):
        service=WanTransferService(self.base/'invalid',origin='https://example.invalid',enabled=True)
        try:
            with patch.object(service.socket,'open') as opened:service._connect();opened.assert_not_called()
        finally:service.close();service.deleteLater();APP.processEvents()

if __name__=='__main__':unittest.main()
