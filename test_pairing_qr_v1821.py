"""Existing temporary code roundtrip, strict payloads and actual panel action."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PySide6.QtCore import Signal,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from app_settings import SettingsStore
from lan_panel import LanPanel
from pairing_qr import decode_qr,parse_code,qr_image
from test_html_native_lan import FakeLan

APP=QApplication.instance() or QApplication([])


class Wan(FakeLan):
    mode='wan';identity_changed=Signal(dict)
    def code_identity(self):return dict(code='ABCD2345',remaining=250,refresh_remaining=190,online=True)


class PairingQRTests(unittest.TestCase):
    def test_roundtrip_original_code_and_strict_url(self):
        for code in ('ABCD2345','Z9X8W7V6'):
            self.assertEqual(decode_qr(qr_image(code),threading.Event()),code)
        for payload in ('https://example.com/?code=ABCD2345','flickdock://pair?code=ABCD2345&code=EFGH6789',
                        'flickdock://pair?code=ABCD2345&next=anything','flickdock://pair?code=00000000','123456'):
            with self.assertRaises(ValueError):parse_code(payload)

    def test_explicit_recognize_action_fills_input_without_sending(self):
        with tempfile.TemporaryDirectory() as root:
            service=Wan();panel=LanPanel(service,settings=SettingsStore(Path(root)))
            panel.show();APP.processEvents()
            try:
                image=qr_image('EFGH6789')
                with patch.object(QApplication,'clipboard',return_value=SimpleNamespace(image=lambda:image)):
                    QTest.mouseClick(panel.qr_clipboard_button,Qt.LeftButton)
                deadline=time.monotonic()+3
                while panel._qr_reading and time.monotonic()<deadline:APP.processEvents();QTest.qWait(5)
                self.assertEqual(panel.peer_code_input.text(),'EFGH6789')
                self.assertEqual(service.calls,[])
                QTest.mouseClick(panel.qr_share_button,Qt.LeftButton);APP.processEvents()
                self.assertTrue(panel._qr_dialog.isVisible())
                self.assertEqual(panel._qr_code_label.text(),'ABCD2345')
                panel._qr_dialog.reject()
            finally:panel.close();panel.deleteLater();APP.processEvents()


if __name__=='__main__':unittest.main()
