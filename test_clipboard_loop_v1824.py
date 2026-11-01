"""Logical copy events and local MIME usability under stalled cloud transport."""
import json,tempfile,time,unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch
from PySide6.QtCore import QMimeData,QUrl
from clipboard_sync import ClipboardSync,inline_text,snapshot
from test_clipboard_sync_v1819 import APP,Account,Clipboard,Events,Hub,until,flush


class SequencedClipboard(Clipboard):
    def __init__(self):super().__init__();self.sequence=0;self.writes=0;self.reads=0
    def mimeData(self):self.reads+=1;return super().mimeData()
    def setMimeData(self,mime):
        self.sequence+=1;self.writes+=1;super().setMimeData(mime)
    def reannounce(self):self.sequence+=1;self.dataChanged.emit()


class HeldHub(Hub):
    def __init__(self):super().__init__();self.release=None
    def api(self,device):
        classic=super().api(device);hub=self
        class Api:
            def cancel(self,key):classic.cancel(key)
            def request(self,method,path,callback,**options):
                if method!='PUT':return classic.request(method,path,callback,**options)
                key=object()
                hub.release=lambda:classic.request(method,path,callback,**options)
                return key
        return Api()


class ClipboardLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-copy-loop-')
        self.base=Path(self.temp.name);self.clients=[]
    def tearDown(self):
        for client in self.clients:client.close()
        until(lambda:all(not client._jobs for client in self.clients));flush();self.temp.cleanup()
    def make(self,name,hub=None,mime=None):
        hub=hub or Hub();clip=SequencedClipboard()
        if mime is not None:clip.setMimeData(mime)
        events=Events();hub.events.append(events)
        client=ClipboardSync(Account(hub.api(name),name),events,self.base/name,enabled=False,clipboard=clip)
        client._sequence=lambda:clip.sequence;client._last_sequence=clip.sequence
        client.enabled=True;self.clients.append(client)
        return client,clip,hub
    def settle(self,seconds=.13):
        until_at=time.monotonic()+seconds
        while time.monotonic()<until_at:APP.processEvents();time.sleep(.002)
    def test_same_content_sequence_notifications_publish_once_and_emit_one_result(self):
        client,clip,hub=self.make('copy');results=[];starts=[]
        client.copy_result.connect(lambda ok,context:results.append(ok))
        client.copy_started.connect(lambda:starts.append(True))
        clip.text('one actual logical content')
        until(lambda:hub.head is not None and client._pending is None)
        identity=hub.head['id'];before=len(hub.calls)
        for _ in range(15):clip.reannounce();self.settle(.07)
        self.assertEqual(hub.head['id'],identity)
        self.assertEqual(len(hub.calls),before,'unchanged native content causes no network requests')
        self.assertEqual(results,[True]);self.assertEqual(starts,[True]);self.assertEqual(clip.writes,1)
    def test_same_content_during_stalled_upload_does_not_restart_or_lose_completion(self):
        client,clip,hub=self.make('held',HeldHub());results=[]
        client.copy_result.connect(lambda ok,context:results.append(ok))
        clip.text('local paste remains immediately usable')
        until(lambda:hub.release is not None)
        task=client._pending;generation=client._generation;native=clip.mimeData()
        for _ in range(8):clip.reannounce();self.settle(.07)
        self.assertIs(client._pending,task);self.assertEqual(client._generation,generation)
        self.assertIs(clip.mimeData(),native);self.assertEqual(native.text(),'local paste remains immediately usable')
        self.assertEqual(results,[])
        hub.release();until(lambda:client._pending is None)
        self.assertEqual(results,[True]);self.assertEqual(clip.writes,1)
    def test_local_file_mime_and_original_bytes_do_not_wait_for_cloud(self):
        path=self.base/'original.bin';path.write_bytes(b'original bytes'*8192)
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(path))]);mime.setData('application/x-local-editor',b'local metadata')
        client,clip,hub=self.make('file',HeldHub())
        clip.setMimeData(mime);until(lambda:hub.release is not None)
        self.assertIs(clip.mimeData(),mime)
        self.assertTrue(Path(clip.mimeData().urls()[0].toLocalFile()).samefile(path))
        self.assertTrue(clip.mimeData().hasFormat('application/x-local-editor'))
        self.assertEqual(path.read_bytes(),b'original bytes'*8192)
        self.assertEqual(clip.writes,1)
        hub.release();until(lambda:client._pending is None)
        self.assertIs(clip.mimeData(),mime);self.assertEqual(clip.writes,1)
    def test_equal_verified_remote_text_preserves_native_formats_without_a_write(self):
        mime=QMimeData();mime.setText('same native content');mime.setData('application/x-editor-private',b'private')
        client,clip,hub=self.make('equal',mime=mime)
        encoded=inline_text(('text',dict(text='same native content',html='')))
        hub.head=dict(id=str(uuid4()),version=1,ready=True,kind='text',size=encoded['size'],sha256=encoded['sha256'],inline=encoded['inline']['body'],copied_at=round(time.time()*1000))
        before=clip.writes;client.refresh();until(lambda:not client._fetching and client._seen==hub.head['id'])
        self.assertIs(clip.mimeData(),mime);self.assertEqual(clip.writes,before)
        self.assertTrue(mime.hasFormat('application/x-editor-private'))
        self.assertEqual(client.view['phase'],'synced')
        count=len(hub.calls);clip.reannounce();self.settle()
        self.assertEqual(len(hub.calls),count)
    def test_empty_and_fresh_cache_cleanup_never_reads_native_payload(self):
        client,clip,hub=self.make('cleanup')
        directory=client._directory()
        with patch.object(clip,'mimeData',side_effect=AssertionError('cleanup asks native provider')):
            client._cleanup()
            client._directories=[];client._cleanup()
        self.assertTrue(directory.exists())
    def test_expired_native_file_cache_is_not_removed_while_still_pasteable(self):
        client,clip,hub=self.make('expiry');directory=client._directory()
        file=directory/'still-pasteable.txt';file.write_text('clipboard bytes')
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(file))])
        clip.mime=mime
        client._directories=[(directory,time.time()-90000)]
        client._cleanup();self.settle()
        self.assertEqual(file.read_text(),'clipboard bytes')
    def test_text_snapshot_materializes_text_only_once(self):
        class CountMime(QMimeData):
            def __init__(self):super().__init__();self.reads=0;self.setText('one read')
            def text(self):self.reads+=1;return super().text()
        clip=SequencedClipboard();mime=CountMime();clip.mime=mime
        self.assertEqual(snapshot(clip)[1]['text'],'one read');self.assertEqual(mime.reads,1)
    def test_native_notification_never_reads_delayed_payload_synchronously(self):
        client,clip,hub=self.make('notify')
        with patch.object(clip,'mimeData',side_effect=AssertionError('native callback rendered payload')):
            clip.text('local copy')
        until(lambda:hub.head is not None and client._pending is None)
        self.assertEqual(clip.mimeData().text(),'local copy')
    def test_native_value_changed_during_render_is_not_stamped_with_new_sequence(self):
        client,clip,hub=self.make('render');clip.text('stale sample')
        original=clip.mimeData;replaced=False
        def replacing():
            nonlocal replaced
            value=original()
            if not replaced:replaced=True;clip.text('newer actual copy')
            return value
        with patch.object(clip,'mimeData',side_effect=replacing):client._changed()
        self.assertIsNone(client._pending);self.assertIsNone(hub.head)
        until(lambda:hub.head is not None and client._pending is None)
        self.assertEqual(sum(method=='POST' for method,path in hub.calls),1)
        self.assertEqual(client.view['value'][1]['text'],'newer actual copy')
    def test_pending_remote_head_allows_a_later_same_content_copy(self):
        client,clip,hub=self.make('known-head');clip.text('A')
        until(lambda:hub.head is not None and client._pending is None)
        previous=hub.head['id'];self.settle(.03)
        remote=str(uuid4());hub.head=dict(id=remote,version=2,ready=False,copied_at=round(time.time()*1000))
        client.refresh();until(lambda:not client._fetching);self.settle(.03)
        clip.text('A');until(lambda:hub.head['id'] not in (previous,remote) and client._pending is None)
        self.assertEqual(sum(method=='POST' for method,path in hub.calls),2)
        self.assertEqual(clip.mimeData().text(),'A')
    def test_disable_during_upload_discards_late_completion_and_preserves_local_copy(self):
        client,clip,hub=self.make('disable',HeldHub());results=[]
        client.copy_result.connect(lambda ok,context:results.append(ok))
        clip.text('local native content');until(lambda:hub.release is not None)
        client.set_enabled(False);count=len(hub.calls);hub.release();self.settle()
        self.assertEqual(results,[]);self.assertEqual(clip.mimeData().text(),'local native content')
        after=len(hub.calls);clip.text('offline local content');self.settle()
        self.assertEqual(len(hub.calls),after);self.assertIsNone(client._pending)
        client.set_enabled(True);self.settle();hub.release=None;clip.text('new copy after enabling')
        until(lambda:hub.release is not None);hub.release();until(lambda:client._pending is None)
        self.assertEqual(results,[True])
    def test_new_content_is_not_deduplicated_and_paste_never_publishes(self):
        client,clip,hub=self.make('changes');clip.text('A')
        until(lambda:hub.head is not None and client._pending is None)
        first=hub.head['id'];clip.text('B')
        until(lambda:hub.head['id']!=first and client._pending is None)
        self.settle()
        count=len(hub.calls)
        for _ in range(100):self.assertEqual(clip.mimeData().text(),'B')
        self.settle();self.assertEqual(len(hub.calls),count)
        self.assertEqual(sum(method=='POST' for method,path in hub.calls),2)
