"""Isolated asynchronous native-reader contract; never reads system clipboard."""
import os,tempfile,time,unittest,threading
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from PySide6.QtCore import QObject,Signal,QMimeData,QTimer
from test_html_native_workspace import APP,until,flush
from test_shared_clipboard_files_v1821 import Peer
from test_clipboard_sync_v1819 import Events,retire_clipboard_clients
import clipboard_sync as subject


class NativeClipboard(QObject):
    dataChanged=Signal()
    def __init__(self):
        super().__init__();self.sequence=1;self.value=None;self.marker=b'';self.owned=False;self.writes=0;self.reads=0
    def mimeData(self):
        self.reads+=1
        raise AssertionError('GUI requested the external delayed-rendering provider')
    def ownsClipboard(self):return self.owned
    def setMimeData(self,mime):
        self.value=subject.snapshot(type('Local',(),{'mimeData':lambda _:mime})())
        self.marker=bytes(mime.data(subject.SYNC_MARKER));self.owned=True;self.writes+=1
        self.sequence+=1;self.dataChanged.emit()
    def copy(self,text):
        self.value=('text',dict(text=text,html=''));self.marker=b'';self.owned=False
        self.sequence+=1;self.dataChanged.emit()


class Reader:
    def __init__(self,clip):self.clip=clip;self.active={};self.cancelled=[];self.closed=False;self.serial=0
    def read(self,callback):
        self.serial+=1;self.active[self.serial]=(callback,self.clip.sequence,self.clip.value,self.clip.marker)
        return self.serial
    def cancel(self,token):self.cancelled.append(token)
    def close(self):self.closed=True
    def deliver(self,token=None,*,status='ready',even_cancelled=False):
        token=next(reversed(self.active)) if token is None else token
        callback,seq,value,marker=self.active.pop(token)
        if token in self.cancelled and not even_cancelled:return
        result=dict(status=status,valid=status=='ready',value=value,known_empty=value is None,
            sequence=seq,sequence_before=seq,sequence_after=seq,marker=marker)
        if value and value[0]=='files':result.update(self.clip.file_info)
        callback(result,None)


class NativeReadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.clip=NativeClipboard();self.peer=Peer();self.reader=Reader(self.clip)
        self.factory=patch.object(subject,'_windows_clipboard_reader',return_value=self.reader);self.factory.start()
        self.native=patch.object(subject,'native_sequence',side_effect=lambda:self.clip.sequence);self.native.start()
        self.application=patch.object(subject.QApplication,'clipboard',return_value=self.clip);self.application.start()
        self.metadata=patch.object(subject,'_windows_clipboard_metadata',side_effect=lambda:dict(
            status='ready',valid=True,known_empty=self.clip.value is None,sequence_before=self.clip.sequence,sequence_after=self.clip.sequence))
        self.metadata.start();self.client=subject.ClipboardSync(self.peer,Events(),self.temp.name,clipboard=self.clip)
        flush()
    def tearDown(self):
        retire_clipboard_clients([self.client])
        self.metadata.stop();self.application.stop();self.native.stop();self.factory.stop();self.temp.cleanup()
    def posts(self):return sum(method=='POST' for method,path,data in self.peer.calls)
    def settle(self):
        stop=time.monotonic()+.1
        while time.monotonic()<stop:APP.processEvents();time.sleep(.002)
    def copy_and_read(self,text):
        self.clip.copy(text);self.client.change_debounce.stop();self.client._changed()
        self.reader.deliver();self.settle()
    def test_first_copy_from_known_empty_is_async_and_still_uploads_once(self):
        self.clip.copy('synthetic actual Copy');self.client.change_debounce.stop();self.client._changed()
        self.assertEqual(self.posts(),0);self.assertEqual(self.clip.reads,0)
        self.reader.deliver();until(lambda:self.posts()==1 and self.client.view.get('phase')=='synced')
        self.assertEqual(self.clip.writes,0);self.assertEqual(self.clip.reads,0)
    def test_invalid_empty_metadata_is_not_a_safe_baseline(self):
        for state in (dict(status='ready',valid=False,known_empty=True,sequence_before=1,sequence_after=1),
                dict(status='unknown',valid=True,known_empty=True,sequence_before=1,sequence_after=1),
                dict(status='ready',valid=True,known_empty=True,sequence_before=None,sequence_after=None),
                dict(status='ready',valid=True,known_empty=True,sequence_before=0,sequence_after=0)):
            with self.subTest(state=state),patch.object(subject,'_windows_clipboard_metadata',return_value=state), \
                    patch.object(self.client,'_sequence',return_value=state['sequence_after']):
                self.client._native_sample=None;self.client._sample_empty_native_metadata()
                self.assertIsNone(self.client._native_sample)
    def test_slow_provider_does_not_block_event_loop_or_local_paste(self):
        self.clip.copy('synthetic paste remains immediate');self.client.change_debounce.stop();self.client._changed()
        times=[];timer=QTimer();timer.setInterval(5);timer.timeout.connect(lambda:times.append(time.perf_counter()));timer.start()
        QTimer.singleShot(250,self.reader.deliver)
        until(lambda:self.posts()==1 and len(times)>20);timer.stop()
        self.assertLess(max(b-a for a,b in zip(times,times[1:])),.15)
        self.assertEqual(self.clip.value[1]['text'],'synthetic paste remains immediate')
        self.assertEqual(self.clip.writes,0);self.assertEqual(self.clip.reads,0)
    def test_new_copy_cancels_old_read_and_late_result_cannot_upload(self):
        self.clip.copy('old');self.client.change_debounce.stop();self.client._changed();old=self.client._native_read_token
        self.clip.copy('new');self.client.change_debounce.stop();self.client._changed()
        self.assertIn(old,self.reader.cancelled)
        self.reader.deliver(old,even_cancelled=True);self.assertEqual(self.posts(),0)
        self.reader.deliver();until(lambda:self.posts()==1 and self.client.view.get('phase')=='synced')
        self.assertEqual(self.client.view['value'][1]['text'],'new')
    def test_off_cancels_reader_and_never_reports_success(self):
        notices=[];self.client.copy_result.connect(lambda ok,context:notices.append(ok))
        self.clip.copy('original local copy');self.client.change_debounce.stop();self.client._changed();token=self.client._native_read_token
        self.client.set_enabled(False);self.reader.deliver(token,even_cancelled=True);self.settle()
        self.assertEqual(self.posts(),0);self.assertEqual(notices,[]);self.assertEqual(self.clip.writes,0)
    def test_account_switch_makes_inflight_payload_a_private_baseline(self):
        self.clip.copy('old account private fixture');self.client.change_debounce.stop();self.client._changed();token=self.client._native_read_token
        self.peer.account={'id':str(uuid4())};self.peer.token='other-session';self.peer.account_changed.emit(self.peer.account)
        self.reader.deliver(token,even_cancelled=True);self.assertEqual(self.posts(),0)
        self.reader.deliver();self.settle();self.assertEqual(self.posts(),0)
        self.assertEqual(self.clip.value[1]['text'],'old account private fixture')
        self.copy_and_read('genuine new Copy after baseline');until(lambda:self.posts()==1)
    def test_unknown_busy_result_never_publishes_or_applies(self):
        self.clip.copy('still local');self.client.change_debounce.stop();self.client._changed()
        self.reader.deliver(status='unknown');self.settle()
        self.assertEqual(self.posts(),0);self.assertEqual(self.clip.writes,0);self.assertEqual(self.clip.reads,0)
        self.assertFalse(self.client._native_current())
    def test_invalid_ready_file_reenters_the_single_retry_transition_and_can_recover(self):
        path=Path(self.temp.name)/'original.bin';path.write_bytes(b'original native bytes')
        self.clip.value=('files',[str(path)]);self.clip.sequence+=1;self.clip.dataChanged.emit()
        self.client.change_debounce.stop();self.client._changed()
        self.clip.file_info=dict(signature=('files',(('WRONG',path.stat().st_size,123),)),source_stat=[1,1,path.stat().st_size,123])
        self.reader.deliver()
        self.assertIsNone(self.client._native_sample);self.assertTrue(self.client._native_retry.isActive())
        self.assertEqual(self.client._native_failures[1],1);self.assertEqual(self.posts(),0)
        self.assertTrue(self.client._native_waiters);self.assertEqual(self.clip.writes,0)
        info=path.stat();self.clip.file_info=dict(signature=('files',((os.path.normcase(os.path.abspath(path)),info.st_size,info.st_mtime_ns),)),
            source_stat=[info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns])
        self.client._native_retry.stop();self.client._start_native_read();self.reader.deliver()
        until(lambda:bool(self.peer.held));self.assertFalse(self.client._native_waiters)
        self.assertEqual(self.client._native_failures[1],0);self.assertEqual(path.read_bytes(),b'original native bytes')
    def test_owned_full_payload_without_marker_keeps_real_marker_and_never_echoes(self):
        previous=str(uuid4()).encode('ascii');self.client._owned_marker=previous
        prior_signature=subject.signature(('text',dict(text='previous owned marker payload',html='')))
        self.client._ignored=prior_signature
        value=('text',dict(text='verified peer payload without marker',html=''));item=subject.inline_text(value);identity=str(uuid4())
        self.peer.head=dict(id=identity,version=1,ready=True,kind='text',size=item['size'],sha256=item['sha256'],
            inline=item['inline']['body'],copied_at=round(time.time()*1000)+1000)
        original=self.clip.setMimeData
        def stripped(mime):mime.removeFormat(subject.SYNC_MARKER);original(mime)
        with patch.object(self.clip,'setMimeData',side_effect=stripped):
            self.client.refresh();until(lambda:self.client._seen==identity and self.client.view['phase']=='synced')
        self.assertEqual(self.clip.value,value);self.assertEqual(self.clip.marker,b'')
        self.assertEqual(self.client._native_sample['marker'],b'');self.assertEqual(self.client._owned_marker,previous)
        self.assertEqual(self.client._ignored,prior_signature)
        for _ in range(3):
            self.clip.sequence+=1;self.clip.dataChanged.emit();self.client.change_debounce.stop();self.client._changed()
            self.reader.deliver();self.settle()
        self.assertEqual(self.posts(),0);self.assertEqual(self.clip.writes,1);self.assertEqual(self.clip.reads,0)
        # A genuinely different known head invalidates content-only suppression.
        # Reasserting A with the OLD marker must not borrow that marker as proof
        # that A was written by the old owned write.
        pending=dict(id=str(uuid4()),version=2,ready=False,copied_at=round(time.time()*1000)+2000)
        self.peer.staged[identity]=self.peer.head;self.peer.staged[pending['id']]=pending;self.peer.head=pending
        self.client.refresh();until(lambda:not self.client._fetching)
        self.clip.marker=previous;self.clip.sequence+=1;self.clip.dataChanged.emit()
        self.client.change_debounce.stop();self.client._changed();self.reader.deliver()
        until(lambda:self.posts()==1 and self.client.view['phase']=='synced')
        self.assertEqual(self.clip.value,value);self.assertNotEqual(self.peer.head['id'],pending['id'])
    def test_owned_write_verifies_submitted_payload_without_external_readback(self):
        value=('text',dict(text='verified peer fixture',html='<b>verified peer fixture</b>'))
        self.assertTrue(self.client._apply(value,Path(self.temp.name),identity=str(uuid4())))
        self.assertEqual(self.clip.value,value);self.assertEqual(self.clip.reads,0)
        self.assertEqual(self.client._snapshot(),value)
    def test_foreign_takeover_after_write_is_not_read_or_marked_success(self):
        def intercepted(mime):self.clip.owned=False;self.clip.sequence+=1
        self.client._owned_marker=str(uuid4()).encode('ascii')
        before=self.client._owned_marker
        with patch.object(self.clip,'setMimeData',side_effect=intercepted):
            self.assertFalse(self.client._apply(('text',dict(text='peer',html='')),Path(self.temp.name),identity=str(uuid4())))
        self.assertEqual(self.clip.reads,0);self.assertEqual(self.client._owned_marker,before)
        self.assertFalse(self.client._native_current());self.assertEqual(self.posts(),0)

    def test_native_file_stat_is_reused_without_gui_source_io_and_updates_next_copy(self):
        path=Path(self.temp.name)/'source.bin';path.write_bytes(b'first original')
        def announce():
            info=path.stat();canonical=os.path.normcase(os.path.abspath(path))
            self.clip.file_info=dict(signature=('files',((canonical,info.st_size,info.st_mtime_ns),)),
                source_stat=[info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns])
            self.clip.value=('files',[str(path)]);self.clip.sequence+=1;self.clip.dataChanged.emit()
        announce();self.client.change_debounce.stop();self.client._changed()
        original=Path.stat;gui=threading.get_ident();reads=[]
        def checked(candidate,*args,**kwargs):
            if str(candidate)==str(path):
                reads.append(threading.get_ident())
                if threading.get_ident()==gui:raise AssertionError('GUI restatted the native source')
            return original(candidate,*args,**kwargs)
        with patch.object(Path,'stat',checked):
            self.reader.deliver();until(lambda:bool(self.peer.held))
        self.assertTrue(reads);self.assertNotIn(gui,reads)
        first=self.client.view['id'];first_stamp=tuple(self.client.file_jobs.tasks[first]['source_stat'])
        path.write_bytes(b'different original content and size');announce()
        self.client.change_debounce.stop();self.client._changed()
        with patch.object(Path,'stat',checked):
            self.reader.deliver();until(lambda:self.client.view['id']!=first and self.client.view['id'] in self.peer.held)
        second=self.client.view['id']
        self.assertNotEqual(tuple(self.client.file_jobs.tasks[second]['source_stat']),first_stamp)
        self.assertEqual(path.read_bytes(),b'different original content and size')

    def test_received_file_apply_uses_worker_fingerprint_without_gui_stat(self):
        path=Path(self.temp.name)/'received.bin';path.write_bytes(b'original received bytes')
        value=('files',subject._prepared_file_paths([str(path)]));original=Path.stat
        def forbidden(candidate,*args,**kwargs):
            if str(candidate)==str(path):raise AssertionError('apply restatted the verified owned source')
            return original(candidate,*args,**kwargs)
        with patch.object(Path,'stat',forbidden):
            self.assertTrue(self.client._apply(value,Path(self.temp.name),identity=str(uuid4())))
        self.assertEqual(subject.signature(self.client._snapshot()),subject.signature(value))


if __name__=='__main__':unittest.main()
