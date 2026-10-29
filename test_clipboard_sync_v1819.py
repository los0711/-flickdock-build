"""Isolated clipboards, original bytes, screenshot pixels and replacement races."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import hashlib,json,tempfile,unittest,zipfile,time
from pathlib import Path
from types import SimpleNamespace
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
    def __init__(self):self.head=None;self.body=None;self.events=[];self.calls=[]
    def api(self,device):
        hub=self
        class Api:
            def request(self,method,path,callback,**kw):
                key=object();hub.calls.append((method,path));
                def deliver():
                    result=None;error=None
                    if path=='/v1/clipboard' and method=='POST':
                        hub.head=dict(id=kw['data']['id'],device=device,ready=False);hub.body=None;result={'clipboard':dict(hub.head)}
                    elif path=='/v1/clipboard':result={'clipboard':dict(hub.head) if hub.head else None}
                    elif method=='PUT':
                        if not hub.head or hub.head['id'] not in path:error=RuntimeError('replaced')
                        else:
                            hub.body=Path(kw['source']).read_bytes();hub.head.update(ready=True,kind=kw['headers']['X-Clipboard-Kind'],size=len(hub.body),sha256=hashlib.sha256(hub.body).hexdigest());result={'clipboard':dict(hub.head)}
                    else:
                        if not hub.head or hub.head['id'] not in path or hub.body is None:error=RuntimeError('replaced')
                        else:
                            assert len(hub.body)==kw['expected']['size'] and hashlib.sha256(hub.body).hexdigest()==kw['expected']['sha256']
                            Path(kw['destination']).write_bytes(hub.body);result=str(kw['destination'])
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

    def test_new_copy_during_preparation_cannot_restore_old_data_and_limit_clears_slot(self):
        self.clips[0].text('old');self.clips[0].text('latest')
        until(lambda:self.clips[1].mimeData().text()=='latest')
        self.clips[0].text('x'*MAX_BYTES)
        until(lambda:self.hub.head is not None and not self.hub.head['ready'] and self.clients[0]._pending is None)
        until(lambda:not self.clips[1].mimeData().hasText())
        self.assertIsNone(self.hub.body)

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


if __name__=='__main__':unittest.main()
