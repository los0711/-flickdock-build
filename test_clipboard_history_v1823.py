"""History format conversions are observations, not new Copy intents."""
from contextlib import ExitStack
import tempfile
from pathlib import Path
import unittest
import time
import threading
import hashlib
import io
import zipfile
from types import SimpleNamespace
from uuid import uuid4
from PySide6.QtCore import QBuffer,QIODevice,QMimeData,QUrl
from PySide6.QtGui import QImage,QColor
from PySide6.QtTest import QTest
from unittest.mock import patch
from clipboard_sync import SYNC_MARKER,signature
import test_clipboard_fast_v1821 as fast_fixture
from test_clipboard_sync_v1819 import APP,until,flush


class HistoryTests(unittest.TestCase):
    def test_uu_control_tokens_never_replace_or_republish_an_oversize_video(self):
        from clipboard_sync import MAX_BYTES
        client=self.clients[0]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);video=root/'original-video.mp4'
            with video.open('wb') as stream:stream.truncate(MAX_BYTES+1)
            mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(video))]);self.clips[0].setMimeData(mime)
            until(lambda:client.view.get('phase')=='size_limit')
            original=client.view['value'];feedback=len(self.feedback)
            for index in range(12):
                token=root/('.uuremote_aeawy82762157'+str(index));token.touch()
                mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(token))]);self.clips[0].setMimeData(mime)
                self.settle()
            self.assertEqual(self.hub.version,0);self.assertEqual(client.view['value'],original)
            self.assertEqual(len(self.feedback),feedback);self.assertEqual(self.native_writes,[0,0])
            self.assertFalse(client.change_debounce.isActive());self.assertFalse(client.retry.isActive())
            real=root/'real-file.bin';real.write_bytes(b'actual user file')
            mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(real))]);self.clips[0].setMimeData(mime)
            until(lambda:self.clips[1].mimeData().hasUrls())
            received=Path(self.clips[1].mimeData().urls()[0].toLocalFile())
            self.assertEqual(received.name,real.name);self.assertEqual(received.read_bytes(),real.read_bytes())
            self.assertEqual(self.hub.version,1)

    def test_poisoned_cloud_archive_is_rejected_once_without_native_writes_or_receipt(self):
        receiver=self.clients[1];self.clips[0].text('keep the current usable content')
        until(lambda:self.clips[1].mimeData().text()=='keep the current usable content')
        before=list(self.native_writes);raw=io.BytesIO()
        with zipfile.ZipFile(raw,'w') as archive:archive.writestr('.uuremote_aeawy8276215748',b'')
        blob=raw.getvalue();identity=str(uuid4());self.hub.version+=1;self.hub.body=blob
        self.hub.head=dict(id=identity,device='0',ready=True,version=self.hub.version,kind='files',size=len(blob),
            sha256=hashlib.sha256(blob).hexdigest(),copied_at=round(time.time()*1000)+1)
        receiver.request_refresh();until(lambda:receiver._rejected_head is not None)
        count=sum(method=='GET' and path.endswith('/content') for method,path in self.hub.calls)
        for _ in range(4):receiver.request_refresh();self.settle()
        self.assertEqual(sum(method=='GET' and path.endswith('/content') for method,path in self.hub.calls),count)
        self.assertEqual(self.native_writes,before);self.assertEqual(self.clips[1].mimeData().text(),'keep the current usable content')
        self.assertNotEqual(receiver._seen,identity);self.assertFalse(receiver.retry.isActive())

    def test_real_local_file_wins_over_an_extra_remote_control_token(self):
        from clipboard_sync import _snapshot_mime,package,unpack
        from virtual_clipboard import VirtualClipboardError
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);real=root/'real.bin';real.write_bytes(b'user bytes')
            token=root/'.uuremote_aeawy8276215748';token.touch()
            mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(token)),QUrl.fromLocalFile(str(real))])
            value=_snapshot_mime(mime);self.assertEqual(value[0],'files');self.assertEqual([Path(ref) for ref in value[1]],[real])
            with self.assertRaisesRegex(VirtualClipboardError,'REMOTE_FILE_PLACEHOLDER'):package(('files',[str(token)]),root)
            archive=root/'bad.zip'
            with zipfile.ZipFile(archive,'w') as zipped:zipped.writestr(token.name,b'')
            with self.assertRaisesRegex(VirtualClipboardError,'REMOTE_FILE_PLACEHOLDER'):unpack('files',archive,root)
            self.assertFalse((root/'files').exists())

    def test_uu_token_does_not_mask_the_real_oversize_virtual_descriptor(self):
        from test_virtual_clipboard import fixture
        from virtual_clipboard import MAX_FILE_BYTES,CONTENT_MIME
        client=self.clients[0];client._is_native=True
        mime=fixture('original-video.mp4',b'',size=MAX_FILE_BYTES+1)
        mime.setUrls([QUrl.fromLocalFile(str(Path(self.temp.name)/'.uuremote_aeawy8276215748'))])
        with patch.object(self.clips[0],'mimeData',return_value=mime),patch.object(mime,'data',wraps=mime.data) as read:
            self.assertIsNone(client._snapshot())
            self.assertEqual(client._native_format_issue,'VIRTUAL_FILE_SIZE_LIMIT')
            self.assertNotIn(CONTENT_MIME,[call.args[0] for call in read.call_args_list])

    def test_rejected_native_sequence_is_not_read_over_and_over(self):
        from native_clipboard_reader import NativeClipboardReader
        with tempfile.TemporaryDirectory() as directory:
            reader=NativeClipboardReader(directory,lambda:10)
            try:
                reader._reject('REMOTE_FILE_PLACEHOLDER')
                with patch('native_clipboard_reader.foreign_clipboard_lock',side_effect=AssertionError('Rejected metadata must not reread the clipboard')):
                    for _ in range(5):self.assertEqual(reader.read(10),(False,None))
                    reader.cached_sequence=11;reader.value=('text',dict(text='next real copy',html=''))
                    self.assertEqual(reader.read(11),(True,reader.value))
                self.assertIsNone(reader.process);self.assertFalse(reader.timer.isActive())
            finally:reader.close();reader.deleteLater();flush()
    def test_file_url_filesystem_alias_is_the_same_clipboard_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'original.bin';source.write_bytes(b'original')
            alias=root/'alias.bin'
            try:alias.symlink_to(source)
            except OSError as error:self.skipTest('Filesystem links unavailable: '+str(error.errno))
            self.assertEqual(signature(('files',[str(source)])),signature(('files',[str(alias)])))
            other=root/'other.bin';other.write_bytes(source.read_bytes())
            self.assertNotEqual(signature(('files',[str(source)])),signature(('files',[str(other)])))
            before=signature(('files',[str(source)]));source.write_bytes(b'changed file')
            self.assertNotEqual(before,signature(('files',[str(alias)])))

    def setUp(self):
        fast_fixture.ClipboardFastTests.setUp(self)
        self.patches=ExitStack();self.sequences=[0,0];self.native_writes=[0,0]
        self.feedback=[];self.clients[0].copy_result.connect(lambda ok,context:self.feedback.append(ok))
        for index,(clip,client) in enumerate(zip(self.clips,self.clients)):
            original=clip.setMimeData
            def write(mime,index=index,original=original):
                self.sequences[index]+=1
                if mime.hasFormat(SYNC_MARKER):self.native_writes[index]+=1
                original(mime)
            self.patches.enter_context(patch.object(clip,'setMimeData',side_effect=write))
            client._sequence=lambda index=index:self.sequences[index]
            client._last_sequence=0
        until(lambda:all(not value._fetching for value in self.clients))

    def tearDown(self):
        self.patches.close();fast_fixture.ClipboardFastTests.tearDown(self)

    def settle(self):
        QTest.qWait(90);flush()

    def test_delayed_startup_clipboard_observation_never_exports_previous_session(self):
        from clipboard_identity import signature
        client=self.clients[0];value=('text',{'text':'existing session fixture','html':''})
        client._native_baseline=(10,20)
        with patch.object(client,'_read_copy_activity',return_value=(20,100,False)):
            client._native_read_ready(11,value,None)
        self.assertEqual(client._last_local_signature,signature(value))
        self.assertEqual(client._last_sequence,11)
        self.assertIsNone(client._local_retry)
        self.assertFalse(client.change_debounce.isActive())
        self.assertEqual(self.hub.version,0)

    def test_history_text_clone_without_marker_never_republishes_or_rewrites(self):
        self.clips[0].text('same actual copy')
        until(lambda:self.clips[1].mimeData().text()=='same actual copy')
        for _ in range(4):
            self.clips[1].text('same actual copy');self.settle()
            self.clips[0].text('same actual copy');self.settle()
        self.assertEqual(self.hub.version,1)
        self.assertEqual(self.feedback,[True])
        self.assertEqual(self.native_writes,[0,1])
        self.clips[1].text('a genuinely different copy')
        until(lambda:self.clips[0].mimeData().text()=='a genuinely different copy')
        self.assertEqual(self.hub.version,2)

    def test_verified_receive_retires_delayed_startup_baseline(self):
        client=self.clients[0];client._native_baseline=(10,20)
        client._adopt_native(signature(('text',{'text':'received fixture','html':''})),None,'received')
        self.assertIsNone(client._native_baseline)

    def test_png_and_native_bitmap_describe_the_same_content(self):
        image=QImage(160,90,QImage.Format_ARGB32);image.fill(QColor(20,40,70,160))
        buffer=QBuffer();buffer.open(QIODevice.WriteOnly);image.save(buffer,'PNG')
        raw=bytes(buffer.data())
        self.assertEqual(signature(('image',dict(image=image))),signature(('image',dict(raw=raw,mime='image/png'))))
        mime=QMimeData();mime.setImageData(image);self.clips[0].setMimeData(mime)
        until(lambda:self.clips[1].mimeData().hasImage())
        baseline=sum(method=='PUT' for method,_ in self.hub.calls)
        for _ in range(3):
            mime=QMimeData();mime.setImageData(image);mime.setData('image/png',raw)
            self.clips[1].setMimeData(mime);self.settle()
        self.assertEqual(sum(method=='PUT' for method,_ in self.hub.calls),baseline)
        self.assertEqual(self.native_writes,[0,1])

    def test_later_same_file_copy_replays_the_existing_confirmed_preview(self):
        client=self.clients[0];repeated=[];client.copy_repeated.connect(repeated.append)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'copy-again.bin';path.write_bytes(b'original file contents')
            def copy_file():
                mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(path))]);self.clips[0].setMimeData(mime)
            copy_file();until(lambda:client.view.get('backed_up'))
            identity=client.view['id'];version=self.hub.version
            client._last_copy_observation=time.monotonic()-60;copy_file();self.settle()
            self.assertEqual(len(repeated),1);self.assertEqual(repeated[0]['id'],identity)
            self.assertEqual(repeated[0]['value'][0],'files')
            self.assertEqual([Path(value) for value in repeated[0]['value'][1]],[path]);self.assertEqual(self.hub.version,version)

    def test_native_line_ending_conversion_is_not_a_second_copy(self):
        self.clips[0].text('first\nsecond')
        until(lambda:self.clips[1].mimeData().text()=='first\nsecond')
        self.clips[1].text('first\r\nsecond');self.settle()
        self.assertEqual(self.hub.version,1)
        self.assertEqual(self.clips[0].mimeData().text(),'first\nsecond')

    def test_later_same_copy_replays_feedback_without_duplicate_cloud_upload(self):
        client=self.clients[0];repeated=[];client.copy_repeated.connect(repeated.append)
        self.clips[0].text('copy this again later')
        until(lambda:client.view.get('backed_up'))
        identity=client.view['id'];version=self.hub.version
        self.clips[0].text('copy this again later');self.settle()
        self.assertFalse(repeated)
        client._last_copy_observation=time.monotonic()-60
        self.clips[0].text('copy this again later');self.settle()
        self.assertEqual([row['id'] for row in repeated],[identity])
        self.assertTrue(repeated[0]['backed_up']);self.assertEqual(self.hub.version,version)
        self.clips[0].text('different content')
        until(lambda:client.view['value'][1]['text']=='different content' and client.view['backed_up'])
        self.assertNotEqual(client.view['id'],identity)

    def test_remote_control_echo_does_not_replay_toast_but_real_same_copy_still_does(self):
        client=self.clients[0];self.clips[0].text('same content shared by remote control')
        until(lambda:client.view.get('backed_up'));version=self.hub.version
        client._is_native=True;client._copy_input_stamp=100;replayed=[];observed=[]
        client.copy_repeated.connect(replayed.append);client.copy_observed.connect(observed.append)
        with patch('clipboard_sync.copy_activity',return_value=(100,10,True)):
            client._last_copy_observation=time.monotonic()-60
            self.clips[0].text('same content shared by remote control');self.settle()
        self.assertFalse(replayed);self.assertFalse(observed)
        with patch('clipboard_sync.copy_activity',return_value=(101,10,False)):
            self.clips[0].text('same content shared by remote control');self.settle()
        self.assertFalse(replayed);self.assertFalse(observed)
        with patch('clipboard_sync.copy_activity',return_value=(102,10,True)):
            self.clips[0].text('same content shared by remote control');self.settle()
        self.assertEqual(len(replayed),1);self.assertEqual(len(observed),1)
        self.assertEqual(self.hub.version,version)

    def test_automatic_echoes_do_not_extend_the_real_repeat_copy_debounce(self):
        client=self.clients[0];self.clips[0].text('local copy')
        until(lambda:client.view.get('backed_up'));client._is_native=True;client._copy_input_stamp=10
        replayed=[];client.copy_repeated.connect(replayed.append)
        with patch('clipboard_sync.copy_activity',return_value=(10,5000,None)):
            self.clips[0].text('local copy');self.settle()
        with patch('clipboard_sync.copy_activity',return_value=(11,20,None)):
            self.clips[0].text('local copy');self.settle()
        self.assertEqual(len(replayed),1)

    def test_native_copy_notification_defers_reads_and_fences_old_remote_writes(self):
        client=self.clients[0];client._is_native=True
        previous=client._generation
        client._prepared_receive=dict(valid=lambda:client._generation==previous)
        with patch.object(client,'_snapshot',wraps=client._snapshot) as read:
            self.clips[0].text('new local copy while remote sharing is enabled')
            self.assertEqual(read.call_count,0)
            self.assertGreater(client._generation,previous)
            client._apply_received();self.assertIsNone(client._prepared_receive)
            self.assertEqual(self.clips[0].mimeData().text(),'new local copy while remote sharing is enabled')
            until(lambda:client.view.get('backed_up'))
            self.assertGreater(read.call_count,0)
        self.assertEqual(self.clips[0].mimeData().text(),'new local copy while remote sharing is enabled')

    def test_new_native_copy_cancels_a_remote_virtual_file_reader_before_any_read(self):
        client=self.clients[0];client._is_native=True
        capture=SimpleNamespace(cancelled=threading.Event())
        client._virtual_capture=((self.sequences[0],'old-descriptor'),capture)
        try:
            with patch.object(client,'_snapshot',wraps=client._snapshot) as read:
                self.clips[0].text('local copy replaces a pending remote virtual file')
                self.assertTrue(capture.cancelled.is_set());self.assertEqual(read.call_count,0)
        finally:client._virtual_capture=None
        until(lambda:client.view.get('backed_up'))

    def test_remote_sharing_virtual_descriptor_does_not_mask_a_ready_local_file(self):
        from virtual_clipboard import DESCRIPTOR_MIME
        client=self.clients[0];client._is_native=True
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'ordinary-local-file.bin';path.write_bytes(b'local user copy')
            mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(path))]);mime.setData(DESCRIPTOR_MIME,b'not a ready remote descriptor')
            self.clips[0].setMimeData(mime);until(lambda:client.view.get('backed_up'))
            self.assertEqual([Path(value) for value in client.view['value'][1]],[path])
            self.assertIsNone(client._virtual_capture);self.assertEqual(client._native_format_issue,'')
            self.assertEqual(path.read_bytes(),b'local user copy')

    def test_multiple_local_urls_are_unsupported_not_an_empty_clipboard(self):
        client=self.clients[0];client._is_native=True
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile('/one.bin'),QUrl.fromLocalFile('/two.bin')])
        with patch.object(self.clips[0],'mimeData',return_value=mime):
            self.assertIsNone(client._snapshot())
        self.assertFalse(client._snapshot_available)
        self.assertEqual(client._native_format_issue,'VIRTUAL_FILE_SINGLE_ONLY')

    def test_equivalent_remote_payload_does_not_replace_system_clipboard(self):
        self.clips[0].text('existing content')
        until(lambda:self.clips[1].mimeData().text()=='existing content')
        before=list(self.native_writes)
        self.assertTrue(self.clients[1]._apply(('text',dict(text='existing content',html='')),self.clients[1].cache,identity=str(uuid4())))
        self.assertEqual(self.native_writes,before)


if __name__=='__main__':unittest.main()
