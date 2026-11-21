"""Account intent, reconnect and native LAN recommendation without OS hooks."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock
from types import SimpleNamespace
from PySide6.QtCore import QObject,Signal,Qt
from PySide6.QtWidgets import QApplication
from wan_transfer import WanTransferService
from lan_panel import LanDialog
from app_settings import SettingsStore
APP=QApplication.instance() or QApplication([])

class Account(QObject):
    account_changed=Signal(object);devices_changed=Signal(list)
    def __init__(self):
        super().__init__();self.signed_in=True;self.device_id='fixture-device'
        self.account={'id':'fixture-account'}
        self.api=Mock(token='session');self.callbacks=[]
        self.api.request.side_effect=lambda method,path,callback,**kw:self.callbacks.append(callback) or 'pending-key'

class AccountTransferTests(unittest.TestCase):
    def offer(self,identifier,total):
        return dict(peer=dict(id='a'*64,name='Sender',features=[]),total=str(total),count=1,
            names=['file.bin'],digest='b'*64,token='c'*64,account_device=True,account_id='fixture-account')

    def test_same_account_small_and_large_offers_wait_for_bind_ack_then_auto_accept_once(self):
        from flickdock import ShelfManager
        for index,total in enumerate((1024,5*1024**3)):
            account=Account();self.wan.attach_account(account,lan_id='a'*32)
            self.wan._account_changed(force=True)
            self.wan.enabled=True;self.wan._connected=True;self.ack()
            manager=SimpleNamespace(account=account,wan=self.wan,settings=SimpleNamespace(lan_receive_dir=str(self.root)),
                receive_notice=SimpleNamespace(begin=Mock()),show_wan_dialog=Mock(),wan_dialog=SimpleNamespace(panel=SimpleNamespace(show_offer=Mock())))
            handler=lambda offer: ShelfManager._wan_offer(manager,offer)
            self.wan.offer_received.connect(handler)
            try:
                identifier=str(index+1)*32;self.wan._message(json.dumps(dict(schema_version=1,type='request',id=identifier,**self.offer(identifier,total))))
                self.assertTrue(self.wan._pending[identifier]['awaiting_binding'])
                manager.show_wan_dialog.assert_not_called();manager.receive_notice.begin.assert_not_called()
                account.callbacks[-1]({'bound':True},None)
                self.assertEqual(self.wan._pending[identifier]['destination'],self.root.resolve())
                manager.show_wan_dialog.assert_not_called();manager.receive_notice.begin.assert_called_once()
                self.wan._present_offer(identifier);manager.receive_notice.begin.assert_called_once()
            finally:self.wan.offer_received.disconnect(handler)

    def test_logout_rejects_a_deferred_same_account_offer_and_late_bind_ack(self):
        account=Account();self.wan.attach_account(account);self.wan.enabled=True;self.wan._connected=True;self.ack()
        offers=[];self.wan.offer_received.connect(offers.append);identifier='9'*32
        self.wan._message(json.dumps(dict(schema_version=1,type='request',id=identifier,**self.offer(identifier,5*1024**3))));callback=account.callbacks[-1]
        account.signed_in=False;account.account_changed.emit(None);callback({'bound':True},None)
        self.assertNotIn(identifier,self.wan._pending);self.assertFalse(offers)

    def test_cancel_wakes_original_stream_connect_waiter(self):
        from lan_transfer import _Transfer
        import threading
        identifier='d'*32;transfer=_Transfer(identifier,'send','Mac',active=True)
        state=dict(role='send',peer=dict(name='Mac'),ready=threading.Event())
        self.wan._pending[identifier]=state;self.wan._transfers[identifier]=transfer
        self.wan.cancel(identifier)
        self.assertTrue(transfer.cancel.is_set());self.assertTrue(state['ready'].is_set())
        self.assertNotIn(identifier,self.wan._pending)
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

if __name__=='__main__':unittest.main()
