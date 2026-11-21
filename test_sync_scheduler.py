"""Push-only deadlines, coalesced reconnects and real clipboard/shelf integration."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QMimeData,QUrl,QTimer
from sync_scheduler import SyncScheduler,sync_decision
from clipboard_sync import ClipboardSync
from shelf_sync import ShelfSync
from test_html_native_workspace import APP,flush,until
import test_clipboard_fast_v1821 as fast

class Clock:
    def __init__(self):self.now=0.
    def __call__(self):return self.now
    def advance(self,milliseconds,scheduler):self.now+=milliseconds/1000;scheduler._run_due()

class SchedulerTests(unittest.TestCase):
    def setUp(self):self.clock=Clock();self.scheduler=SyncScheduler(clock=self.clock)
    def tearDown(self):self.scheduler.close();self.scheduler.deleteLater();flush()

    def test_idle_day_has_no_deadline_or_checks_for_either_sync_type(self):
        calls=[]
        for kind in ('shelf','clipboard'):
            self.scheduler.register(kind,lambda kind=kind:calls.append(kind)).configure(enabled=True)
            self.assertIsNone(sync_decision(kind=kind,enabled=True,connected=True).delay_ms)
        self.clock.advance(86_400_000,self.scheduler)
        self.assertEqual(calls,[]);self.assertFalse(self.scheduler.timer.isActive())
        self.assertEqual(len(self.scheduler.findChildren(QTimer)),1)

    def test_notifications_and_reconnect_coalesce_and_offline_never_polls(self):
        calls=[];entries=[]
        for kind in ('shelf','clipboard'):
            entry=self.scheduler.register(kind,lambda kind=kind:calls.append(kind));entry.configure(enabled=True);entries.append(entry)
        self.scheduler.set_connected(False)
        for _ in range(20):
            for entry in entries:entry.request()
        self.clock.advance(600_000,self.scheduler)
        self.assertEqual(calls,[]);self.assertFalse(self.scheduler.timer.isActive())
        self.scheduler.set_connected(True);self.scheduler.set_connected(True)
        for entry in entries:entry.request()
        self.clock.advance(0,self.scheduler)
        self.assertCountEqual(calls,['shelf','clipboard']);self.assertFalse(self.scheduler.timer.isActive())

    def test_progress_only_uses_four_and_one_point_five_seconds_then_stops(self):
        calls=[];entry=self.scheduler.register('shelf',lambda:calls.append(self.clock.now));entry.configure(enabled=True,transferring=True)
        self.clock.advance(3999,self.scheduler);self.assertEqual(calls,[])
        entry.configure(enabled=True,transferring=True)
        self.clock.advance(1,self.scheduler);self.assertEqual(calls,[4.])
        entry.configure(enabled=True,transferring=True,finishing=True)
        self.clock.advance(1499,self.scheduler);self.assertEqual(len(calls),1)
        self.clock.advance(1,self.scheduler);self.assertEqual(len(calls),2)
        entry.configure(enabled=True);self.clock.advance(600_000,self.scheduler)
        self.assertEqual(len(calls),2);self.assertFalse(self.scheduler.timer.isActive())

    def test_logout_and_server_backoff_do_not_leak_or_spin(self):
        calls=[];entry=self.scheduler.register('clipboard',lambda:calls.append(True));entry.configure(enabled=True)
        entry.defer(4000);entry.request();self.clock.advance(3999,self.scheduler);self.assertEqual(calls,[])
        self.clock.advance(1,self.scheduler);self.assertEqual(calls,[True])
        entry.request();entry.close();self.clock.advance(0,self.scheduler)
        self.assertEqual(calls,[True]);self.assertFalse(self.scheduler.timer.isActive())

class ClipboardRecoveryTests(unittest.TestCase):
    def setUp(self):fast.ClipboardFastTests.setUp(self)
    def tearDown(self):fast.ClipboardFastTests.tearDown(self)

    def test_missed_short_text_is_recovered_by_exactly_one_reconnect_head_read(self):
        receiver=self.clients[1];events=self.hub.events[1]
        self.clips[0].text('first copy');until(lambda:self.clips[1].mimeData().text()=='first copy')
        until(lambda:all(not client._fetching for client in self.clients))
        receiver.scheduler.set_connected(False)
        with patch.object(events,'_message'):
            self.clips[0].text('missed short text');until(lambda:self.hub.head and self.hub.head.get('inline')
                and self.clips[0].mimeData().text()=='missed short text' and self.clients[0]._pending is None)
        self.assertEqual(self.clips[1].mimeData().text(),'first copy')
        before=sum(method=='GET' and path=='/v1/clipboard' for method,path in self.hub.calls)
        receiver.scheduler.set_connected(True)
        until(lambda:self.clips[1].mimeData().text()=='missed short text');flush()
        after=sum(method=='GET' and path=='/v1/clipboard' for method,path in self.hub.calls)
        self.assertEqual(after-before,1);self.assertFalse(receiver.scheduler.timer.isActive())

    def test_progress_reads_versions_without_starting_a_second_content_download(self):
        receiver=self.clients[1];clock=Clock();receiver.scheduler.clock=clock
        original=receiver.api.request;held=[];content=[]
        def request(method,path,callback,**options):
            if method=='GET' and path.endswith('/content'):
                content.append(path);held.append(lambda:original(method,path,callback,**options));return object()
            return original(method,path,callback,**options)
        receiver.api.request=request
        path=Path(self.temp.name)/'one original.bin';path.write_bytes(b'original bytes'*4096)
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(path))]);self.clips[0].setMimeData(mime)
        until(lambda:bool(held));self.assertEqual(receiver.schedule.decision.delay_ms,4000)
        before=sum(method=='GET' and path=='/v1/clipboard' for method,path in self.hub.calls)
        clock.advance(4000,receiver.scheduler);flush()
        after=sum(method=='GET' and path=='/v1/clipboard' for method,path in self.hub.calls)
        self.assertEqual(after-before,1);self.assertEqual(len(content),1)
        held[0]();until(lambda:receiver.view['phase']=='synced' and self.clips[1].mimeData().hasUrls());flush()
        received=Path(self.clips[1].mimeData().urls()[0].toLocalFile());self.assertEqual(received.read_bytes(),path.read_bytes())
        self.assertIsNone(receiver.schedule.decision.delay_ms);self.assertFalse(receiver.scheduler.timer.isActive())

class SharedRegistrationTests(unittest.TestCase):
    def test_actual_clients_register_once_and_upload_receipt_changes_the_shared_deadline(self):
        from test_shared_clipboard_files_v1821 import Peer,Adapter,Clipboard,Events
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);peer=Peer();events=Events();clip=Clipboard();scheduler=SyncScheduler()
            client=ClipboardSync(peer,events,root/'profile',clipboard=clip,scheduler=scheduler)
            engine=ShelfSync(root/'profile',peer,Adapter(),scheduler=scheduler)
            engine.select_account(peer.account);engine.attach_clipboard(client)
            try:
                until(lambda:not client._fetching and not engine.running)
                self.assertIs(client.scheduler,engine.scheduler);self.assertEqual(set(scheduler.registrations),{'shelf','clipboard'})
                source=root/'payload.bin';source.write_bytes(b'original'*8192)
                mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))]);clip.setMimeData(mime)
                until(lambda:bool(peer.held));identity=client.view['id']
                peer.progress(identity,.6);self.assertEqual(client.schedule.decision.delay_ms,4000)
                peer.progress(identity,1.);self.assertEqual(client.schedule.decision.delay_ms,1500)
                peer.finish(identity);until(lambda:client._pending is None)
                self.assertIsNone(client.schedule.decision.delay_ms)
                self.assertFalse(scheduler.timer.isActive())
            finally:
                engine.close();client.close();scheduler.close()
                until(lambda:not client._jobs and not engine.jobs);flush()

if __name__=='__main__':unittest.main()
