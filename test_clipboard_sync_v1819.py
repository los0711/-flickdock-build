"""Isolated clipboards, original bytes, screenshot pixels and replacement races."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import hashlib,json,tempfile,unittest,zipfile,time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PySide6.QtCore import QObject,Signal,QMimeData,QTimer,QUrl
from PySide6.QtGui import QImage,QColor
from test_html_native_workspace import APP,until,flush
from clipboard_sync import ClipboardSync,package,unpack,snapshot,MAX_BYTES


class Clipboard(QObject):
    dataChanged=Signal()
    def __init__(self):super().__init__();self.mime=QMimeData()
    def mimeData(self):return self.mime
    def setMimeData(self,mime):self.mime=mime;self.dataChanged.emit()
    def clear(self):self.setMimeData(QMimeData())
    def text(self,text):mime=QMimeData();mime.setText(text);self.setMimeData(mime)


class Account(QObject):
    account_changed=Signal(object)
    def __init__(self,api,device):super().__init__();self.api=api;self.device_id=device;self.signed_in=True


class Events(QObject):
    clipboard_changed=Signal()
    connected=Signal()
    def __init__(self):super().__init__();self.socket=self


class Hub:
    def __init__(self):self.head=None;self.pending=None;self.body=None;self.events=[];self.calls=[]
    def api(self,device):
        hub=self
        class Api:
            def request(self,method,path,callback,**kw):
                key=object();hub.calls.append((method,path));
                def deliver():
                    result=None;error=None
                    if path=='/v1/clipboard' and method=='POST':
                        hub.pending=dict(id=kw['data']['id'],device=device,ready=False,copied_at=kw['data']['copied_at']);result={'clipboard':dict(hub.pending)}
                    elif path=='/v1/clipboard':result={'clipboard':dict(hub.head) if hub.head else None}
                    elif method=='PUT':
                        if not hub.pending or hub.pending['id'] not in path:error=RuntimeError('replaced')
                        else:
                            hub.head=hub.pending;hub.pending=None
                            hub.body=Path(kw['source']).read_bytes();hub.head.update(ready=True,kind=kw['headers']['X-Clipboard-Kind'],size=len(hub.body),sha256=hashlib.sha256(hub.body).hexdigest());result={'clipboard':dict(hub.head)}
                    else:
                        if not hub.head or hub.head['id'] not in path or hub.body is None:error=RuntimeError('replaced')
                        else:
                            assert len(hub.body)==kw['expected']['size'] and hashlib.sha256(hub.body).hexdigest()==kw['expected']['sha256']
                            Path(kw['destination']).write_bytes(hub.body);result=str(kw['destination'])
                    if isinstance(result,dict):result['server_time']=round(time.time()*1000)
                    callback(result,error)
                    if method in ('POST','PUT'):
                        for event in hub.events:event.clipboard_changed.emit()
                QTimer.singleShot(0,deliver);return key
            def cancel(self,key):pass # Deliberately deliver late: generation guards must protect the slot.
        return Api()


class ClipboardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.hub=Hub();self.clients=[];self.clips=[]
        for i in range(3):
            clip=Clipboard();events=Events();self.hub.events.append(events);account=Account(self.hub.api(str(i)),str(i))
            self.clients.append(ClipboardSync(account,events,self.base/str(i),enabled=True,clipboard=clip));self.clips.append(clip)
        flush()
    def tearDown(self):
        for client in self.clients:client.close()
        until(lambda:all(not client._jobs for client in self.clients));flush();self.temp.cleanup()

    def test_latest_copy_from_any_device_replaces_and_paste_never_consumes(self):
        self.clips[0].text('first 中文\r\n');until(lambda:self.clips[1].mimeData().text()=='first 中文\r\n')
        first=self.hub.head['id'];self.clips[1].text('second https://example.invalid')
        until(lambda:all(clip.mimeData().text()=='second https://example.invalid' for clip in self.clips))
        self.assertNotEqual(self.hub.head['id'],first);identity=self.hub.head['id'];body=self.hub.body
        for _ in range(20):
            for clip in self.clips:self.assertEqual(clip.mimeData().text(),'second https://example.invalid')
        self.assertEqual(self.hub.head['id'],identity);self.assertEqual(self.hub.body,body)
        self.assertEqual(sum(method=='PUT' for method,_ in self.hub.calls),2) # no echo upload loop

    def test_screenshot_keeps_resolution_and_every_pixel(self):
        image=QImage(640,480,QImage.Format_ARGB32);image.fill(QColor(20,40,70,160));image.setPixelColor(2,3,QColor(1,222,33,255))
        mime=QMimeData();mime.setImageData(image);self.clips[0].setMimeData(mime)
        until(lambda:all(clip.mimeData().hasImage() for clip in self.clips))
        result=self.clips[1].mimeData().imageData();self.assertEqual(result.size(),image.size())
        self.assertEqual(result.convertToFormat(QImage.Format_RGBA8888),image.convertToFormat(QImage.Format_RGBA8888))
        self.assertTrue(self.clips[2].mimeData().hasImage())

    def test_copied_original_file_and_native_image_bytes_are_unchanged(self):
        original=self.base/'原图.png';image=QImage(800,600,QImage.Format_RGB32);image.fill(QColor('#abcdef'));image.save(str(original),'PNG')
        expected=original.read_bytes();mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(original))]);self.clips[0].setMimeData(mime)
        until(lambda:self.clips[1].mimeData().hasUrls())
        path=Path(self.clips[1].mimeData().urls()[0].toLocalFile());self.assertEqual(path.name,original.name);self.assertEqual(path.read_bytes(),expected)
        self.assertEqual(original.read_bytes(),expected)
        mime=QMimeData();mime.setImageData(image);mime.setData('image/png',expected);self.clips[1].setMimeData(mime)
        until(lambda:bytes(self.clips[0].mimeData().data('image/png'))==expected)
        self.assertEqual(self.hub.body,expected)

    def test_new_copy_during_preparation_cannot_restore_old_data_and_limit_preserves_slot(self):
        self.clips[0].text('old');self.clips[0].text('latest')
        until(lambda:self.clips[1].mimeData().text()=='latest')
        previous=self.hub.head['id']
        previous_fence=self.clients[0]._local_fence
        self.clips[0].text('x'*MAX_BYTES)
        until(lambda:self.clients[0]._local_fence is not previous_fence and self.clients[0]._pending is None)
        self.assertEqual(self.hub.head['id'],previous)
        self.assertEqual(self.clips[0].mimeData().text(),'x'*MAX_BYTES)
        self.assertEqual(self.clips[1].mimeData().text(),'latest')
        self.assertTrue(self.hub.head['ready'])

    def test_untrusted_archive_cannot_escape_owned_download_directory(self):
        path=self.base/'unsafe.zip'
        with zipfile.ZipFile(path,'w') as archive:archive.writestr('../escape.txt','bad')
        with self.assertRaises(ValueError):unpack('files',path,self.base)
        self.assertFalse((self.base.parent/'escape.txt').exists())

    def test_modifying_and_copying_the_same_file_path_replaces_its_bytes(self):
        source=self.base/'same.txt';source.write_bytes(b'old')
        def copy_file():
            mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))]);self.clips[0].setMimeData(mime)
        copy_file();until(lambda:self.clips[1].mimeData().hasUrls());first=self.hub.head['id']
        source.write_bytes(b'changed original');copy_file()
        until(lambda:self.hub.head['id']!=first and self.clips[1].mimeData().hasUrls() and Path(self.clips[1].mimeData().urls()[0].toLocalFile()).read_bytes()==b'changed original')

    def test_startup_reaps_expired_owned_cache_and_preserves_unmarked_directories(self):
        base=self.base/'restart';old=base/'clipboard-cache'/'clip-owned';old.mkdir(parents=True)
        (old/'.lease.json').write_text(json.dumps({'flickdock_clipboard':1,'created_at':time.time()-90000}))
        (old/'private.bin').write_bytes(b'old private copy');unmarked=old.parent/'clip-unrelated';unmarked.mkdir();(unmarked/'keep').write_bytes(b'original')
        account=Account(self.hub.api('restart'),'restart');account.signed_in=False
        client=ClipboardSync(account,Events(),base,enabled=True,clipboard=Clipboard());self.clients.append(client)
        until(lambda:not old.exists());self.assertTrue((unmarked/'keep').exists())

    def test_a_file_just_below_20m_keeps_its_bytes_despite_archive_headers(self):
        source=self.base/'near-limit.bin';source.write_bytes(b'x'*(MAX_BYTES-1));out=self.base/'pack';out.mkdir()
        item=package(('files',[str(source)]),out);self.assertIsNotNone(item)
        extracted=unpack('files',item['path'],out)[1][0]
        self.assertEqual(Path(extracted).read_bytes(),source.read_bytes())

    def test_old_pending_copy_is_not_replayed_after_a_long_sleep(self):
        client=self.clients[0];client._pending=dict(id='old',created_at=time.time()-90000,staged=False,prepared=False)
        before=sum(method=='POST' for method,_ in self.hub.calls);client._retry();flush()
        self.assertIsNone(client._pending);self.assertEqual(sum(method=='POST' for method,_ in self.hub.calls),before)

    def test_same_session_profile_refresh_cannot_cancel_a_copy_in_progress(self):
        self.clips[0].text('copy while profile is loading')
        self.clients[0].account.account_changed.emit({'id':'same-session-profile'})
        until(lambda:self.clips[1].mimeData().text()=='copy while profile is loading')
        self.assertTrue(self.hub.head['ready'])

    def test_duplicate_file_notifications_create_one_upload_without_touching_source_clipboard(self):
        source=self.base/'original.txt';source.write_text('original bytes',encoding='utf8')
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))]);self.clips[0].setMimeData(mime)
        until(lambda:self.clips[1].mimeData().hasUrls())
        before=len(self.hub.calls)
        for _ in range(8):self.clips[0].dataChanged.emit()
        flush();time.sleep(.08);flush()
        self.assertEqual(len([m for m,p in self.hub.calls[before:] if m in ('POST','PUT')]),0)
        self.assertIs(self.clips[0].mimeData(),mime)
        self.assertEqual(Path(mime.urls()[0].toLocalFile()).read_text(), 'original bytes')

    def test_delayed_received_image_notifications_do_not_echo(self):
        image=QImage(33,22,QImage.Format_ARGB32);image.fill(QColor('red'))
        mime=QMimeData();mime.setImageData(image);self.clips[0].setMimeData(mime)
        until(lambda:self.clips[1].mimeData().hasImage())
        before=sum(m=='PUT' for m,p in self.hub.calls)
        for _ in range(4):self.clips[1].dataChanged.emit()
        time.sleep(.08);flush()
        self.assertEqual(sum(m=='PUT' for m,p in self.hub.calls),before)

    def test_disabling_sync_preserves_native_copy_and_stops_uploads(self):
        self.clients[0].set_enabled(False);self.clips[0].text('local only')
        time.sleep(.08);flush()
        self.assertEqual(self.clips[0].mimeData().text(),'local only');self.assertIsNone(self.hub.head)

    def test_failed_local_staging_preserves_copy_against_older_cloud_and_can_retry(self):
        self.clips[1].text('older remote');until(lambda:self.hub.head and self.hub.head['ready'])
        client=self.clients[0];client._seen=''
        for failure in ('_next_sequence','_directory'):
            expected='new local '+failure+('x'*33000 if failure=='_directory' else '')
            with patch.object(client,failure,side_effect=OSError('temporary disk failure')):
                self.clips[0].text(expected)
                until(lambda:client._local_retry is not None)
                self.assertIsNone(client._pending);self.assertFalse(client._priority_active())
                client.refresh();until(lambda:not client._fetching)
                self.assertEqual(self.clips[0].mimeData().text(),expected)
            client._retry();until(lambda:client._pending is None and client._local_retry is None)
            until(lambda:self.clips[2].mimeData().text()==expected)

    def test_metadata_poll_never_preempts_bulk_and_received_file_survives_disable_expiry(self):
        client=self.clients[0];changes=[];client.activity_changed.connect(changes.append)
        client.refresh();until(lambda:not client._fetching)
        self.assertEqual(changes,[])
        source=self.base/'native.dat';source.write_bytes(b'keep pasteable')
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))]);self.clips[1].setMimeData(mime)
        until(lambda:self.clips[0].mimeData().hasUrls())
        path=Path(self.clips[0].mimeData().urls()[0].toLocalFile());client.set_enabled(False)
        with patch('clipboard_sync.time.time',return_value=time.time()+90000):client._cleanup()
        until(lambda:not client._jobs)
        self.assertEqual(path.read_bytes(),b'keep pasteable')

    def test_near_limit_control_text_is_not_inflated_by_json_escaping(self):
        folder=self.base/'control';folder.mkdir();text='\x00'*(MAX_BYTES-1)
        item=package(('text',dict(text=text,html='')),folder)
        self.assertIsNotNone(item);self.assertEqual(unpack('text',item['path'],folder)[1]['text'],text)

    def test_receive_cache_failure_releases_fetch_and_retries_without_changing_native_copy(self):
        client=self.clients[0]
        with patch.object(client,'_directory',side_effect=OSError('temporary cache permission')):
            self.clips[1].text('remote copy after cache recovery')
            until(lambda:self.hub.head and self.hub.head['ready'])
            until(lambda:client.retry.isActive())
            self.assertFalse(client._fetching);self.assertFalse(client._priority_active())
            self.assertFalse(self.clips[0].mimeData().hasText())
        client._retry();until(lambda:self.clips[0].mimeData().text()=='remote copy after cache recovery')

    def test_observed_remote_clock_is_floor_for_only_future_copy(self):
        client=self.clients[0];mono=time.monotonic()
        client._clock_anchor=(mono,10000);client._observed_clock=20000
        captured=dict(mono=mono,at=None,floor=9000)
        client._calibrate(dict(server_time=10001,clipboard={'copied_at':30000}),mono)
        self.assertLess(client._copy_timestamp(captured),30000)
        new=dict(mono=mono,at=None,floor=client._observed_clock)
        self.assertEqual(client._copy_timestamp(new),30001)

    def test_bulk_payload_yields_to_clipboard_and_cancellation_still_works(self):
        import threading
        from transfer_priority import set_clipboard_priority,yield_bulk_payload
        started=threading.Event();finished=threading.Event();cancel=threading.Event()
        def bulk():started.set();yield_bulk_payload(cancel);finished.set()
        set_clipboard_priority(True);thread=threading.Thread(target=bulk);thread.start()
        try:
            self.assertTrue(started.wait(.5));self.assertFalse(finished.wait(.08))
            set_clipboard_priority(False);self.assertTrue(finished.wait(.5))
            set_clipboard_priority(True);cancel.set()
            with self.assertRaises(InterruptedError):yield_bulk_payload(cancel)
        finally:set_clipboard_priority(False);thread.join(1)


if __name__=='__main__':unittest.main()
