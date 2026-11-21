"""At-least-once latest-head delivery with isolated simulated devices."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from clipboard_sync import ClipboardSync
from cloud_events import CloudEvents
from cloud_api import CloudError
from test_clipboard_sync_v1819 import Account,Clipboard,flush,until
from test_clipboard_fast_v1821 import InlineHub


class ClipboardReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.hub=InlineHub();self.clients=[];self.clips=[];self.accounts=[]
        for index in range(3):
            device=str(uuid4());api=self.hub.api(device);api.token='fixture-token'
            account=Account(api,device);account.account={'id':'account-fixture'}
            clip=Clipboard();events=CloudEvents(account,enabled=False);self.hub.events.append(events)
            client=ClipboardSync(account,events,self.root/str(index),clipboard=clip)
            self.accounts.append(account);self.clips.append(clip);self.clients.append(client)
        for client in self.clients:client.scheduler.set_connected(True)
        until(lambda:all(client.inbox.ready and not client._fetching for client in self.clients));flush()

    def tearDown(self):
        for event in self.hub.events:event.close()
        for client in self.clients:client.close()
        until(lambda:all(not client._jobs and not client.inbox._thread.is_alive() for client in self.clients))
        flush();self.temp.cleanup()

    def test_three_devices_receive_one_copy_and_duplicates_do_not_reapply(self):
        receiver=self.clients[1]
        self.clients[0].deliveries.set_connections({self.accounts[i].device_id:{'route':'cloud'} for i in (1,2)})
        with patch.object(self.clips[1],'setMimeData',wraps=self.clips[1].setMimeData) as write:
            self.clips[0].text('three devices agree · 中文')
            until(lambda:all(clip.mimeData().text()=='three devices agree · 中文' for clip in self.clips),seconds=3)
            version=self.hub.head['version'];self.assertEqual(receiver._applied_version,version)
            before=write.call_count
            receiver._pushed({'clipboard':dict(self.hub.head)})
            receiver._pushed({'clipboard':dict(self.hub.head)})
            QTest.qWait(100);flush();self.assertEqual(write.call_count,before)
            self.assertGreaterEqual(receiver._seen_version,version)
            self.assertGreaterEqual(receiver._fetched_version,version)
            until(lambda:self.clients[0].deliveries.counts(self.hub.head['id'])['applied']==2)
            counts=self.clients[0].deliveries.counts(self.hub.head['id'])
            self.assertEqual((counts['delivered'],counts['applied'],counts['total']),(2,2,2))

    def test_disabled_receiver_persists_head_and_applies_after_reenable(self):
        receiver=self.clients[1];receiver.set_enabled(False)
        self.clips[0].text('persist only metadata until enabled')
        until(lambda:self.hub.head is not None and receiver.inbox.snapshot('account-fixture')['pending'] is not None)
        until(lambda:receiver.inbox.saved_revision>=receiver.inbox._revision)
        stored=json.loads(receiver.inbox.path.read_text(encoding='utf8'))
        pending=stored['accounts']['account-fixture']['pending']
        self.assertEqual(pending['id'],self.hub.head['id']);self.assertEqual(pending['version'],self.hub.head['version'])
        self.assertNotIn('persist only metadata',json.dumps(stored))
        self.assertNotEqual(self.clips[1].mimeData().text(),'persist only metadata until enabled')
        receiver.set_enabled(True)
        until(lambda:self.clips[1].mimeData().text()=='persist only metadata until enabled',seconds=3)
        self.assertIsNone(receiver.inbox.snapshot('account-fixture')['pending'])

    def test_unavailable_native_access_persists_then_reconciles(self):
        receiver=self.clients[1]
        with patch.object(receiver,'_native_accessible',return_value=False):
            receiver.recheck_native_access()
            self.clips[0].text('arrived while native clipboard is unavailable')
            until(lambda:receiver.inbox.snapshot('account-fixture')['pending'] is not None)
            until(lambda:receiver.inbox.saved_revision>=receiver.inbox._revision)
            self.assertNotEqual(self.clips[1].mimeData().text(),'arrived while native clipboard is unavailable')
        receiver.recheck_native_access()
        until(lambda:self.clips[1].mimeData().text()=='arrived while native clipboard is unavailable',seconds=3)

    def test_push_while_signed_out_stays_scoped_and_is_applied_after_login(self):
        receiver=self.clients[1];account=self.accounts[1]
        account.signed_in=False;account.account_changed.emit(None)
        self.clips[0].text('head queued across sign-in')
        until(lambda:receiver.inbox.snapshot('account-fixture')['pending'] is not None)
        until(lambda:receiver.inbox.saved_revision>=receiver.inbox._revision)
        self.assertNotEqual(self.clips[1].mimeData().text(),'head queued across sign-in')
        account.signed_in=True;account.account_changed.emit(account.account)
        until(lambda:self.clips[1].mimeData().text()=='head queued across sign-in',seconds=3)

    def test_running_native_read_does_not_count_as_failed_writes(self):
        receiver=self.clients[1]
        reader=SimpleNamespace(process=object(),loading=False)
        def waiting():receiver._snapshot_available=False;return None
        with patch.object(receiver,'_native_reader',reader),patch.object(receiver,'_snapshot',side_effect=waiting):
            self.clips[0].text('slow native read')
            until(lambda:receiver._prepared_receive is not None,seconds=3)
            QTest.qWait(900);flush()
            pending=receiver.inbox.snapshot('account-fixture')['pending']
            self.assertEqual(pending['attempts'],0)
            self.assertNotEqual(pending['stage'],'needs_confirmation')
        until(lambda:self.clips[1].mimeData().text()=='slow native read',seconds=3)

    def test_same_version_hint_is_not_dropped_before_application(self):
        event=self.hub.events[1];notices=[]
        event.clipboard_changed.connect(lambda:notices.append(True))
        self.clips[0].text('the inline head may still be pending')
        until(lambda:bool(self.hub.head) and self.hub.head.get('ready'))
        head=dict(self.hub.head);notices.clear()
        event._message(json.dumps(dict(schema_version=1,type='clipboard_inline',clipboard=head)))
        event._message(json.dumps(dict(schema_version=1,type='clipboard',version=head['version'])))
        self.assertEqual(notices,[True])
        until(lambda:self.clips[1].mimeData().text()=='the inline head may still be pending',seconds=3)

    def test_pending_head_survives_restart_and_account_switch(self):
        receiver=self.clients[1];receiver.set_enabled(False)
        self.clips[0].text('head after app restart')
        until(lambda:receiver.inbox.snapshot('account-fixture')['pending'] is not None)
        until(lambda:receiver.inbox.saved_revision>=receiver.inbox._revision)
        receiver.close();until(lambda:not receiver.inbox._thread.is_alive())
        old=self.hub.events[1];old.close()
        event=CloudEvents(self.accounts[1],enabled=False);self.hub.events[1]=event
        receiver=ClipboardSync(self.accounts[1],event,self.root/'1',clipboard=self.clips[1],enabled=False)
        self.clients[1]=receiver;receiver.scheduler.set_connected(True)
        until(lambda:receiver.inbox.ready)
        self.assertEqual(receiver.inbox.snapshot('account-fixture')['pending']['id'],self.hub.head['id'])
        receiver.set_enabled(True)
        until(lambda:self.clips[1].mimeData().text()=='head after app restart',seconds=4)
        self.assertIsNone(receiver.inbox.snapshot('account-fixture')['pending'])

    def test_old_server_falls_back_to_applied_only_receipts(self):
        receiver=self.clients[1];original=receiver.api.request;rejected=[]
        def old_server(method,path,callback,**kwargs):
            if path.endswith('/delivery') and kwargs.get('data',{}).get('stage')=='received':
                rejected.append(True);QTimer.singleShot(0,lambda:callback(None,CloudError('old server',400,'INVALID_REQUEST')))
                return object()
            return original(method,path,callback,**kwargs)
        receiver.api.request=old_server
        self.clips[0].text('legacy receipt fallback')
        until(lambda:self.clips[1].mimeData().text()=='legacy receipt fallback')
        until(lambda:self.hub.delivery and self.accounts[1].device_id in self.hub.delivery.get('receipts',{}))
        self.assertTrue(rejected);self.assertTrue(receiver.deliveries.received_unsupported)

    def test_reconnect_fetches_latest_without_a_missed_push(self):
        receiver=self.clients[1];receiver.scheduler.set_connected(False)
        event=self.hub.events.pop(1)
        try:
            self.clips[0].text('latest after reconnect')
            until(lambda:self.hub.head and self.hub.head.get('ready'))
            self.assertNotEqual(self.clips[1].mimeData().text(),'latest after reconnect')
            start=time.monotonic();receiver.scheduler.set_connected(True)
            until(lambda:self.clips[1].mimeData().text()=='latest after reconnect',seconds=4)
            self.assertLess(time.monotonic()-start,4)
        finally:self.hub.events.insert(1,event)

    def test_native_write_failure_remains_pending_until_explicit_retry(self):
        receiver=self.clients[1]
        with patch.object(self.clips[1],'setMimeData',return_value=None):
            self.clips[0].text('retry after occupied clipboard')
            until(lambda:(receiver.inbox.snapshot('account-fixture').get('pending') or {}).get('stage')=='needs_confirmation',seconds=5)
            pending=receiver.inbox.snapshot('account-fixture')['pending']
            self.assertEqual(pending['attempts'],3);self.assertEqual(pending['error'],'NATIVE_WRITE_UNCONFIRMED')
            self.assertLess(receiver._applied_version,pending['version']);self.assertFalse(receiver.retry.isActive())
            until(lambda:self.hub.delivery and self.accounts[1].device_id in self.hub.delivery.get('received',{}))
            self.assertNotIn(self.accounts[1].device_id,self.hub.delivery.get('receipts',{}))
        receiver.retry_received()
        until(lambda:self.clips[1].mimeData().text()=='retry after occupied clipboard')
        self.assertEqual(receiver._applied_version,pending['version'])

    def test_push_during_file_transfer_uses_immediate_path(self):
        receiver=self.clients[1]
        receiver.schedule.configure(enabled=True,transferring=True)
        start=time.monotonic();self.clips[0].text('clipboard while transferring')
        until(lambda:self.clips[1].mimeData().text()=='clipboard while transferring',seconds=1)
        self.assertLess(time.monotonic()-start,1)


if __name__=='__main__':unittest.main()
