"""Original local content is independent of the cloud limit; fake providers only."""
import gc,hashlib,os,sys,subprocess,tempfile,threading,time,unittest
import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from types import SimpleNamespace
from PySide6.QtCore import QBuffer,QIODevice
from PySide6.QtGui import QImage,QColor
from PySide6.QtWidgets import QWidget
from test_html_native_workspace import APP,until,flush
import test_clipboard_native_read_v1825 as native_tests
from test_win_clipboard_io_v1825 import Fake,frame
from win_clipboard_native import capture
from win_clipboard_io import decode_frame
import win_clipboard_cache as cache
import clipboard_sync as subject
import clipboard_preview as preview_module
from clipboard_preview import ClipboardPresenter,ClipboardPreview


class LocalOriginalTests(unittest.TestCase):
    def setUp(self):
        from test_win_clipboard_io_v1825 import ensure_owned_process_temp
        ensure_owned_process_temp()
        self.f=native_tests.NativeReadTests('test_first_copy_from_known_empty_is_async_and_still_uploads_once');self.f.setUp()
        self.cache_temp=tempfile.TemporaryDirectory(prefix='fd-owned-consumer-',ignore_cleanup_errors=True);self.base=Path(self.cache_temp.name).resolve(strict=True)/'owned-originals';self.base_patch=patch.object(cache,'cache_base',return_value=self.base);self.base_patch.start()
        self.model=ClipboardPresenter(self.f.client);self.results=[];self.f.client.copy_result.connect(lambda ok,ctx:self.results.append(ok))
    def tearDown(self):
        self.model.close();until(lambda:not self.model.jobs);self.model.state={}
        self.f.client._native_sample=None;self.f.client.view={}
        self.f.tearDown()
        # The nested fixture retires its client; this scope owns the presenter.
        self.model.deleteLater();flush()
        gc.collect();cache.schedule_cleanup();self.assertTrue(cache._IDLE.wait(2))
        self.base_patch.stop();self.cache_temp.cleanup()
    def inject(self,result):
        self.original_native=('local-provider-original',object());self.f.clip.value=self.original_native
        self.f.clip.sequence=result['sequence_after'];self.f.clip.dataChanged.emit()
        self.f.client.change_debounce.stop();self.f.client._changed()
        callback,*_=self.f.reader.active.pop(self.f.client._native_read_token);callback(result,None)
    def local_text(self,text):
        cap=cache.CacheCapability();sink=cache.CacheSink(cap.root,cap.token,cap.instance)
        raw=(text+'\0').encode('utf-16-le');provider=Fake({13:raw});provider.read_snapshot=lambda name:provider.formats[name]
        try:
            value=capture(provider,32,cache=sink);sink.close()
            with patch('win_clipboard_io.MAX_BYTES',32):value=decode_frame(frame(value),cache=cap)
            return value,raw
        finally:sink.close()
    def test_full_text_cache_drives_existing_preview_limit_without_upload_or_gui_read(self):
        text='original text '*25000;result,raw=self.local_text(text);content=result['local_content'];path=Path(content.parts[0]['path'])
        self.inject(result)
        until(lambda:self.model.text_preview is not None)
        self.assertEqual(self.model.text_preview,text[:262144]);self.assertTrue(self.model.text_truncated)
        self.assertEqual(path.read_bytes(),raw);self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),content.parts[0]['sha256'])
        self.assertEqual(self.f.client.view['phase'],'local');self.assertTrue(self.f.client._native_current())
        self.assertIs(self.f.clip.value,self.original_native);self.assertEqual(self.f.clip.reads,0);self.assertEqual(self.f.clip.writes,0)
        self.assertEqual(self.f.posts(),0);self.assertEqual(self.results,[]);self.assertIsNone(self.f.client._pending)
        owner=QWidget();manager=SimpleNamespace(shelf=owner,clipboard_presenter=self.model,settings=SimpleNamespace(theme='dark'))
        dialog=ClipboardPreview(manager)
        try:
            self.assertEqual(dialog.text.toPlainText(),text[:262144])
            self.assertIs(dialog.content.currentWidget(),dialog.text)
            self.assertIn('预览前 256K 字符，剪贴板保留完整内容',dialog.status.text())
            self.assertIn('100 MB',dialog.sync_limit_note.text())
        finally:dialog.deleteLater();owner.deleteLater();flush()
    def test_real_over_limit_file_reference_keeps_local_preview_and_newer_peer_recovers(self):
        source=Path(self.f.temp.name)/'native-original-large.bin'
        with source.open('wb') as target:target.truncate(subject.MAX_BYTES+1)
        provider=Fake({15:b''});provider.path=str(source);result=decode_frame(frame(capture(provider)))
        self.inject(result);self.assertEqual(self.f.client.view['value'][1],[str(source)])
        self.assertTrue(self.f.client._native_current());self.assertEqual(self.f.posts(),0);self.assertEqual(self.results,[])
        client=self.f.client;client.refresh();until(lambda:client._clock_anchor is not None and not client._fetching)
        fence=client._copy_timestamp(client._local_fence);self.assertIsNotNone(fence)
        def head(at,version):
            item=subject.inline_text(('text',dict(text='strict newer remote',html='')))
            return dict(id=str(uuid4()),version=version,ready=True,kind='text',size=item['size'],sha256=item['sha256'],inline=item['inline']['body'],copied_at=at)
        self.f.peer.head=head(fence,1);client.refresh();until(lambda:not client._fetching)
        self.assertEqual(self.f.clip.writes,0);self.assertIs(self.f.clip.value,self.original_native)
        self.f.peer.head=head(fence+1000,2);client.refresh();until(lambda:client._seen==self.f.peer.head['id'])
        self.assertEqual(self.f.clip.value[1]['text'],'strict newer remote');self.assertEqual(self.f.posts(),0)
        self.assertEqual(source.stat().st_size,subject.MAX_BYTES+1)
    def test_local_text_worker_error_is_visible_sanitized_and_close_stops_new_work(self):
        result,raw=self.local_text('complete original '*50)
        with patch.object(preview_module,'read_clip_text',side_effect=PermissionError(13,'PRIVATE_PATH_AND_TEXT')):
            self.inject(result);until(lambda:self.model.text_error!='')
        self.assertEqual(self.model.text_error,'无法读取剪贴板预览');self.assertNotIn('PRIVATE',repr(self.model.state))
        owner=QWidget();manager=SimpleNamespace(shelf=owner,clipboard_presenter=self.model,settings=SimpleNamespace(theme='dark'))
        dialog=ClipboardPreview(manager)
        try:
            self.assertEqual(dialog.info.text(),'无法读取剪贴板预览');self.assertIs(dialog.content.currentWidget(),dialog.info)
            old_value=self.model.state['value'];self.f.copy_and_read('new actual copy recovers preview failure')
            until(lambda:self.f.client.view['phase']=='synced')
            self.assertEqual(dialog.text.toPlainText(),'new actual copy recovers preview failure');self.assertEqual(self.model.text_error,'')
            self.model.close();state=dict(self.model.state,value=old_value,revision=self.model.state['revision']+1)
            with patch.object(preview_module,'DiskWork',side_effect=AssertionError('closed presenter scheduled work')):
                self.model._update(state)
        finally:dialog.deleteLater();owner.deleteLater();flush()
        self.assertEqual(self.f.posts(),1);self.assertEqual(self.results,[True]);self.assertEqual(self.f.clip.value[1]['text'],'new actual copy recovers preview failure')
    def test_late_local_text_failure_cannot_replace_new_copy_or_show_stale_error(self):
        result,_=self.local_text('original local cache '*50);started=threading.Event();release=threading.Event()
        def delayed(content,cancel):started.set();release.wait(2);raise PermissionError(13,'PRIVATE_OLD_CACHE')
        try:
            with patch.object(preview_module,'read_clip_text',side_effect=delayed):
                self.inject(result);until(started.is_set)
                self.f.copy_and_read('new copy while old local preview is reading');until(lambda:self.f.client.view['phase']=='synced')
                release.set();until(lambda:not self.model.jobs)
            self.assertEqual(self.model.text_error,'');self.assertEqual(self.model.state['value'][1]['text'],'new copy while old local preview is reading')
            self.assertEqual(self.f.posts(),1);self.assertEqual(self.results,[True])
        finally:release.set()
    def test_exact_preview_budget_excludes_native_terminator_from_truncation(self):
        result,raw=self.local_text('x'*262144);self.inject(result);until(lambda:self.model.text_preview is not None)
        self.assertEqual(self.model.text_preview,'x'*262144);self.assertFalse(self.model.text_truncated)
        self.assertEqual(raw[-2:],b'\0\0');self.assertEqual(self.f.posts(),0);self.assertEqual(self.results,[])
    def test_original_cached_image_uses_existing_decoder_and_late_result_cannot_replace_new_text(self):
        image=QImage(96,48,QImage.Format_RGB32);image.fill(QColor('#2465db'));buffer=QBuffer();buffer.open(QIODevice.WriteOnly);self.assertTrue(image.save(buffer,'PNG'));raw=bytes(buffer.data())
        cap=cache.CacheCapability();sink=cache.CacheSink(cap.root,cap.token,cap.instance);provider=Fake({'PNG':raw});provider.read_snapshot=lambda name:provider.formats[name]
        try:
            result=capture(provider,32,cache=sink);sink.close()
            with patch('win_clipboard_io.MAX_BYTES',32):result=decode_frame(frame(result),cache=cap)
            content=result['local_content'];self.inject(result);self.model.request_large();until(lambda:not self.model.large.isNull())
            self.assertEqual(self.model.large.size(),image.size().scaled(1400,1400,__import__('PySide6.QtCore',fromlist=['Qt']).Qt.KeepAspectRatio))
            self.assertEqual(Path(content.parts[0]['path']).read_bytes(),raw);self.assertEqual(self.f.posts(),0)
            started=threading.Event();release=threading.Event()
            def delayed(value,size,cancel):
                started.set();release.wait(2)
                # Deliberately emulate a decoder which produced a result just
                # as its cancellation arrived. The generation still owns UI.
                return image
            self.model.large=QImage();self.model._decoding.clear()
            with patch.object(preview_module,'read_clip_image',side_effect=delayed):
                self.model.request_large();until(started.is_set)
                self.f.copy_and_read('new actual local text');until(lambda:self.f.client.view['phase']=='synced')
                release.set();until(lambda:not self.model.jobs)
            self.assertEqual(self.model.state['value'][1]['text'],'new actual local text');self.assertTrue(self.model.large.isNull())
        finally:sink.close()
    def test_real_owned_child_retains_and_previews_complete_100m_plus_one_png(self):
        from win_clipboard_io import WindowsClipboardReader
        image=QImage(96,48,QImage.Format_RGB32);image.fill(QColor('#2465db'))
        buffer=QBuffer();buffer.open(QIODevice.WriteOnly);self.assertTrue(image.save(buffer,'PNG'))
        source=Path(self.f.temp.name)/'complete-native-original.png'
        with source.open('wb') as target:
            target.write(bytes(buffer.data()));target.truncate(subject.MAX_BYTES+1)
        with source.open('rb') as stream:original_sha=hashlib.file_digest(stream,'sha256').hexdigest()
        # This owned child uses a fake native provider, but the actual reader,
        # pipe, operation deadline, parser, cache verification and Qt preview.
        program='''import sys,argparse,mmap
from pathlib import Path
sys.path.insert(0,sys.argv[1]);sys.argv.pop(1)
import win_clipboard_cache as cache
from win_clipboard_native import capture,encode_frame
p=argparse.ArgumentParser();p.add_argument('--source');p.add_argument('--native-cache-root');p.add_argument('--native-cache-token');p.add_argument('--native-cache-instance');a=p.parse_args()
cache.cache_base=lambda:Path(a.native_cache_root).parent
class Provider:
 def sequence(self):return 11
 def open(self):return True
 def close(self):pass
 def count(self):return 1
 def available(self,k):return k=='PNG'
 def read_snapshot(self,k):return raw
 def read(self,k,limit):raise AssertionError('original must use snapshot')
with open(a.source,'rb') as f,mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as raw:
 sink=cache.CacheSink(Path(a.native_cache_root),a.native_cache_token,a.native_cache_instance)
 try:
  result=capture(Provider(),cache=sink);sink.close()
  for chunk in encode_frame(result):sys.stdout.buffer.write(chunk)
  sys.stdout.buffer.flush()
 finally:sink.close()
'''
        reader=WindowsClipboardReader(command=[sys.executable,'-c',program,str(Path(subject.__file__).parent),'--source',str(source)],
            process_factory=lambda *args,**options:subprocess.Popen(*args,**options))
        self.f.client._native_reader=reader
        self.original_native=('complete-original-native-provider',object());self.f.clip.value=self.original_native
        self.f.clip.sequence=11;self.f.clip.dataChanged.emit();self.f.client.change_debounce.stop();self.f.client._changed()
        self.model.request_large();until(lambda:not self.model.large.isNull(),seconds=5)
        content=self.f.client.view['value'][1]['local_content'];part=next(p for p in content.parts if p['name']=='image')
        self.assertEqual(part['size'],subject.MAX_BYTES+1);self.assertEqual(part['sha256'],original_sha)
        self.assertEqual(self.model.large.size(),image.size().scaled(1400,1400,__import__('PySide6.QtCore',fromlist=['Qt']).Qt.KeepAspectRatio))
        self.assertEqual(self.f.client.view['phase'],'local');self.assertEqual(self.f.posts(),0);self.assertEqual(self.results,[])
        self.assertIs(self.f.clip.value,self.original_native);self.assertEqual(self.f.clip.reads,0);self.assertEqual(self.f.clip.writes,0)
        self.assertIsNone(reader._active);self.assertFalse(reader._clock.isActive())
        with source.open('rb') as stream:self.assertEqual(hashlib.file_digest(stream,'sha256').hexdigest(),original_sha)
    def test_real_100m_plus_one_logical_text_keeps_full_utf16_under_default_deadline(self):
        from win_clipboard_io import WindowsClipboardReader
        source=Path(self.f.temp.name)/'complete-native-original.utf16';characters=subject.MAX_BYTES+1
        with source.open('wb') as target:
            chunk=b'x\0'*1048576;remaining=characters
            while remaining:
                count=min(1048576,remaining);target.write(chunk[:count*2]);remaining-=count
            target.write(b'\0\0')
        with source.open('rb') as stream:original_sha=hashlib.file_digest(stream,'sha256').hexdigest()
        program='''import sys,argparse,mmap
from pathlib import Path
sys.path.insert(0,sys.argv[1]);sys.argv.pop(1)
import win_clipboard_cache as cache
from win_clipboard_native import capture,encode_frame
p=argparse.ArgumentParser();p.add_argument('--source');p.add_argument('--native-cache-root');p.add_argument('--native-cache-token');p.add_argument('--native-cache-instance');a=p.parse_args()
cache.cache_base=lambda:Path(a.native_cache_root).parent
class Provider:
 def sequence(self):return 11
 def open(self):return True
 def close(self):pass
 def count(self):return 1
 def available(self,k):return k==13
 def read_snapshot(self,k):return raw
 def read(self,k,limit):raise AssertionError('original must use snapshot')
with open(a.source,'rb') as f,mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as raw:
 sink=cache.CacheSink(Path(a.native_cache_root),a.native_cache_token,a.native_cache_instance)
 try:
  result=capture(Provider(),cache=sink);sink.close()
  for chunk in encode_frame(result):sys.stdout.buffer.write(chunk)
  sys.stdout.buffer.flush()
 finally:sink.close()
'''
        reader=WindowsClipboardReader(command=[sys.executable,'-c',program,str(Path(subject.__file__).parent),'--source',str(source)],
            process_factory=lambda *args,**options:subprocess.Popen(*args,**options))
        completions=[];original_read=reader.read;started=time.monotonic()
        def read(callback,**options):
            def done(result,error):
                completions.append((result.get('status'),result.get('code'),time.monotonic()-started));callback(result,error)
            return original_read(done,**options)
        reader.read=read;self.f.client._native_reader=reader
        self.original_native=('complete-original-native-provider',object());self.f.clip.value=self.original_native
        self.f.clip.sequence=11;self.f.clip.dataChanged.emit();self.f.client.change_debounce.stop();self.f.client._changed()
        until(lambda:bool(completions),seconds=5);self.f.client._native_retry.stop()
        print(json.dumps(dict(case='actual_logical_text_over100m',logical_bytes=characters,raw_utf16_bytes=source.stat().st_size,
            completion=completions[0],deadline_ms=reader.deadline_ms,backend='synthetic Popen, no native clipboard/Job API')))
        self.assertEqual(completions[0][0],'ready');until(lambda:self.model.text_preview is not None)
        content=self.f.client.view['value'][1]['local_content'];part=next(p for p in content.parts if p['name']=='text')
        self.assertEqual(content.logical_size,characters);self.assertEqual(part['size'],(characters+1)*2);self.assertEqual(part['sha256'],original_sha)
        self.assertEqual(self.model.text_preview,'x'*262144);self.assertTrue(self.model.text_truncated)
        self.assertEqual(self.f.client.view['phase'],'local');self.assertEqual(self.f.posts(),0);self.assertEqual(self.results,[])
        self.assertIs(self.f.clip.value,self.original_native);self.assertEqual(self.f.clip.reads,0);self.assertEqual(self.f.clip.writes,0)

if __name__=='__main__':unittest.main()
