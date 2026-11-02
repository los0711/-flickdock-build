"""Original MIME priority and real capture/framing after a verified peer apply.

Only fake native providers/clipboard plus real Qt workers and a local peer.
No system clipboard, hook, native process or desktop access.
"""
import ctypes,hashlib,struct,tempfile,time,unittest,zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from uuid import uuid4
from PySide6.QtCore import QBuffer,QIODevice
from PySide6.QtGui import QImage
import clipboard_sync as sync
import win_clipboard_native as native
from win_clipboard_io import decode_frame
from test_win_clipboard_io_v1825 import Fake
from test_clipboard_native_read_v1825 import NativeClipboard,Reader
from test_clipboard_empty_bootstrap_v1825 import MetadataProvider
from test_clipboard_sync_v1819 import Account,Events,Hub
from test_html_native_workspace import APP,flush,until

def encoded(format='PNG'):
    image=QImage(16,12,QImage.Format.Format_RGBA8888);image.fill(0xaa3355dd)
    buffer=QBuffer();buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer,format):raise AssertionError('Required Qt image encoder unavailable: '+format)
    result=bytes(buffer.data());buffer.close();return result

def original_png(note=b'Original metadata preserved'):
    rendered=encoded();kind=b'tEXt';data=b'Original\0'+note
    chunk=struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
    return rendered[:-12]+chunk+rendered[-12:]

class OriginalFormatTests(unittest.TestCase):
    def test_same_pixels_different_png_encoding_keeps_explicit_original(self):
        original=original_png();rendered=encoded()
        self.assertEqual(QImage.fromData(original),QImage.fromData(rendered));self.assertNotEqual(original,rendered)
        provider=Fake({'PNG':rendered,'image/png':original})
        result=decode_frame(b''.join(native.encode_frame(native.capture(provider))))
        self.assertEqual(result['value'],('image',dict(raw=original,mime='image/png')))
        self.assertEqual(provider.reads,['image/png']);self.assertEqual(provider.closed,1)
    def test_all_original_mime_formats_precede_native_aliases(self):
        aliases={'PNG':encoded(),'JFIF':encoded('JPEG'),'WebP':encoded('WEBP'),'TIFF':encoded('TIFF')}
        for mime,format in (('image/png','PNG'),('image/jpeg','JPEG'),('image/webp','WEBP'),('image/bmp','BMP'),('image/tiff','TIFF')):
            with self.subTest(mime=mime):
                original=original_png() if mime=='image/png' else encoded(format)
                self.assertFalse(QImage.fromData(original).isNull())
                provider=Fake(dict(aliases,**{mime:original}));result=native.capture(provider)
                self.assertEqual(result['value'],('image',dict(raw=original,mime=mime)));self.assertEqual(provider.reads,[mime])
    def test_native_only_aliases_remain_available(self):
        for alias,mime,format in (('PNG','image/png','PNG'),('JFIF','image/jpeg','JPEG'),('WebP','image/webp','WEBP'),('TIFF','image/tiff','TIFF')):
            with self.subTest(alias=alias):
                raw=encoded(format);provider=Fake({alias:raw});result=native.capture(provider)
                self.assertEqual(result['value'],('image',dict(raw=raw,mime=mime)));self.assertEqual(provider.reads,[alias])
    def test_original_trailing_bytes_are_not_stripped_or_reencoded(self):
        raw=original_png()+b'\0\0exact original trailing bytes\0';provider=Fake({'PNG':encoded(),'image/png':raw})
        result=native.capture(provider);self.assertEqual(result['value'][1]['raw'],raw)
    def test_smaller_alias_cannot_bypass_original_size_policy(self):
        original=original_png(b'x'*300);rendered=encoded();self.assertLess(len(rendered),len(original))
        provider=Fake({'PNG':rendered,'image/png':original});result=native.capture(provider,limit=len(rendered))
        self.assertEqual(result['status'],'overlimit');self.assertEqual(provider.reads,['image/png'])

class CapturingReader(Reader):
    def __init__(self,clip):super().__init__(clip);self.captures=[]
    def deliver(self,token=None):
        token=next(reversed(self.active)) if token is None else token
        callback,sequence,value,marker=self.active.pop(token)
        if token in self.cancelled:return
        formats={}
        if self.clip.value is not None:
            kind,data=self.clip.value
            if kind!='image':raise AssertionError('Synthetic peer fixture requires image')
            formats={'image/png':data['raw'],'PNG':encoded()}
        if self.clip.marker:formats[native.SYNC_MARKER]=self.clip.marker+b'\0'
        provider=Fake(formats);provider.sequence=lambda:self.clip.sequence
        result=decode_frame(b''.join(native.encode_frame(native.capture(provider))))
        self.captures.append((result,tuple(provider.reads)));callback(result,None)

class ImageReceiveEchoTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-image-fidelity-');self.clip=NativeClipboard();self.reader=CapturingReader(self.clip);self.hub=Hub();self.events=Events();self.hub.events.append(self.events)
        self.api=self.hub.api('local');self.account=Account(self.api,'local');self.provider=MetadataProvider(self.clip)
        self.patches=[patch.object(sync,'_windows_clipboard_reader',return_value=self.reader),patch.object(sync,'native_input_available',return_value=True),patch.object(sync.sys,'platform','win32'),patch.object(ctypes,'windll',SimpleNamespace(user32=SimpleNamespace(GetClipboardSequenceNumber=Mock(side_effect=lambda:self.clip.sequence))),create=True),patch.object(sync.QApplication,'clipboard',return_value=self.clip),patch.object(sync,'_windows_clipboard_metadata',side_effect=lambda:native.native_metadata(self.provider))]
        for item in self.patches:item.start()
        self.client=sync.ClipboardSync(self.account,self.events,self.temp.name,clipboard=self.clip);flush()
    def tearDown(self):
        self.client.close();until(lambda:not self.client._jobs);flush()
        for item in reversed(self.patches):item.stop()
        self.temp.cleanup()
    def posts(self):return sum(method=='POST' for method,path in self.hub.calls)
    def receive(self):
        raw=original_png();identity=str(uuid4());self.hub.body=raw
        self.hub.head=dict(id=identity,device='peer',version=1,ready=True,kind='image',size=len(raw),sha256=hashlib.sha256(raw).hexdigest(),copied_at=round(time.time()*1000)+1000)
        self.client.refresh();until(lambda:self.client._seen==identity and self.clip.writes==1 and self.client.view['phase']=='synced')
        self.assertEqual(self.clip.value,('image',dict(raw=raw,mime='image/png')));self.assertTrue(self.clip.marker)
        self.assertEqual(self.posts(),0);return raw
    def reannounce_and_read(self):
        self.clip.sequence+=1;self.clip.dataChanged.emit();self.client.change_debounce.stop();self.client._changed();self.reader.deliver()
        until(lambda:not self.client._jobs and not self.client._keys and self.client._pending is None)
    def test_peer_apply_then_real_dual_format_capture_preserves_signature_and_no_echo(self):
        original=self.receive();marker=self.clip.marker;expected=sync.signature(self.clip.value)
        for unused in range(3):
            self.reannounce_and_read();self.assertEqual(self.posts(),0)
            captured,reads=self.reader.captures[-1];self.assertEqual(captured['marker'],marker)
            self.assertEqual(sync.signature(captured['value']),expected);self.assertEqual(reads,(native.SYNC_MARKER,'image/png'))
            self.assertEqual(captured['value'][1]['raw'],original);self.assertEqual(self.clip.writes,1)
        self.assertEqual(self.clip.reads,0)
    def test_same_marker_with_different_original_payload_is_not_ignored(self):
        self.receive();old_marker=self.clip.marker;different=original_png(b'Different real copied original')
        self.clip.value=('image',dict(raw=different,mime='image/png'));self.clip.owned=False
        self.reannounce_and_read();until(lambda:self.posts()==1)
        self.assertEqual(self.clip.marker,old_marker);self.assertEqual(self.hub.body,different)
        self.assertEqual(self.posts(),1);self.assertEqual(self.clip.writes,1);self.assertEqual(self.clip.reads,0)

if __name__=='__main__':unittest.main(verbosity=2)
