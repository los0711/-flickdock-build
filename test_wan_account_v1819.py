"""Account intent, reconnect and native LAN recommendation without OS hooks."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock
from PySide6.QtCore import QObject,Signal
from PySide6.QtWidgets import QApplication
from wan_transfer import WanTransferService
from lan_panel import LanDialog
from app_settings import SettingsStore
APP=QApplication.instance() or QApplication([])

class Account(QObject):
    account_changed=Signal(object);devices_changed=Signal(list)
    def __init__(self):
        super().__init__();self.signed_in=True;self.device_id='fixture-device'
        self.api=Mock(token='session');self.callbacks=[]
        self.api.request.side_effect=lambda method,path,callback,**kw:self.callbacks.append(callback) or 'pending-key'

class AccountTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.wan=WanTransferService(self.root/'wan',enabled=False);self.sent=[]
        self.wan._send_signal=lambda value:self.sent.append(value) or True
    def tearDown(self):self.wan.close();APP.processEvents();self.temp.cleanup()
    def ack(self):self.wan._message(json.dumps(dict(schema_version=1,type='account_state',nonce=self.wan._account_nonce)))
    def test_account_refresh_keeps_pending_bind_and_logout_rejects_its_callback(self):
        account=Account();self.wan.attach_account(account,lan_id='a'*32)
        self.wan.enabled=True;self.wan._connected=True;self.ack()
        self.assertEqual(account.api.request.call_count,1)
        account.account_changed.emit({});self.assertEqual(account.api.request.call_count,1)
        old=account.callbacks[0];account.signed_in=False;account.account_changed.emit(None)
        self.assertFalse(self.sent[-1]['active']);old({'bound':True},None)
        self.assertIsNone(self.wan._bound_identity)
    def test_anonymous_reconnect_always_sends_current_intent(self):
        self.wan._hello()
        self.assertEqual([row['type'] for row in self.sent],['hello','account_state'])
        self.assertFalse(self.sent[-1]['active']);first=self.sent[-1]['nonce']
        self.wan._hello();self.assertNotEqual(self.sent[-1]['nonce'],first)
    def test_first_remote_dialog_forwards_recommendation_without_dropping_draft(self):
        self.wan._peers={'b'*64:dict(id='b'*64,name='Mac',platform='macos',online=True,account_device=True,lan_id='c'*32)}
        self.wan.nearby_peers_provider=lambda:[dict(id='c'*32,name='Mac')]
        dialog=LanDialog(self.wan,SettingsStore(self.root/'settings'));received=[]
        dialog.lan_recommended.connect(lambda paths,peer:received.append((paths,peer)))
        try:
            source=self.root/'original.txt';source.write_text('original')
            panel=dialog.panel;panel.queue_paths([source]);panel._recommend_lan()
            self.assertEqual(received,[([str(source)],'c'*32)])
            self.assertEqual(panel._queued_paths,[str(source)])
            self.assertIn('同账号',panel.devices.itemText(0))
        finally:dialog.close();dialog.deleteLater();APP.processEvents()

if __name__=='__main__':unittest.main()
