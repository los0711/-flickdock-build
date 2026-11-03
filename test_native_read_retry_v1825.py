"""Bounded native unknown retries through the actual callback, fake APIs only."""
import ctypes,tempfile,unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
import clipboard_sync as sync
import win_clipboard_native as native
from test_clipboard_native_read_v1825 import NativeClipboard,Reader
from test_clipboard_empty_bootstrap_v1825 import MetadataProvider
from test_shared_clipboard_files_v1821 import Peer
from test_clipboard_sync_v1819 import Events,retire_clipboard_clients
from test_html_native_workspace import flush,until

class NativeRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-native-retry-');self.clip=NativeClipboard();self.reader=Reader(self.clip);self.peer=Peer();self.provider=MetadataProvider(self.clip)
        self.patches=[patch.object(sync,'_windows_clipboard_reader',return_value=self.reader),patch.object(sync,'native_input_available',return_value=True),patch.object(sync.sys,'platform','win32'),patch.object(ctypes,'windll',SimpleNamespace(user32=SimpleNamespace(GetClipboardSequenceNumber=Mock(side_effect=lambda:self.clip.sequence))),create=True),patch.object(sync.QApplication,'clipboard',return_value=self.clip),patch.object(sync,'_windows_clipboard_metadata',side_effect=lambda:native.native_metadata(self.provider))]
        for item in self.patches:item.start()
        self.client=sync.ClipboardSync(self.peer,Events(),self.temp.name,clipboard=self.clip);self.notices=[];self.client.copy_result.connect(lambda ok,context:self.notices.append(ok));flush()
    def tearDown(self):
        retire_clipboard_clients([self.client])
        for item in reversed(self.patches):item.stop()
        self.temp.cleanup()
    def copy(self,text='synthetic pending original'):
        self.clip.copy(text);self.client.change_debounce.stop();self.client._changed();return self.client._native_read_token
    def retry(self):self.client._native_retry.stop();self.client._start_native_read();return self.client._native_read_token
    def posts(self):return sum(method=='POST' for method,path,data in self.peer.calls)
    def test_three_same_sequence_unknown_callbacks_have_original_bounded_backoff(self):
        self.copy();sequence=self.clip.sequence
        for attempt in range(1,4):
            if attempt>1:self.retry()
            self.reader.deliver(status='unknown')
            self.assertEqual(self.client._native_failures[:2],(sequence,attempt))
            self.assertEqual(self.client._native_retry.isActive(),attempt<3)
            if attempt<3:self.assertEqual(self.client._native_retry.interval(),250*2**(attempt-1))
        calls=self.reader.serial;self.client._start_native_read();self.assertEqual(self.reader.serial,calls)
        self.assertIsNone(self.client._native_read_token);self.assertEqual(self.posts(),0);self.assertEqual(self.notices,[])
        self.assertEqual(self.clip.writes,0);self.assertEqual(self.clip.reads,0);self.assertEqual(self.clip.value[1]['text'],'synthetic pending original')
    def test_ready_after_unknown_resets_failures_and_next_sequence_restarts_at_one(self):
        self.copy();self.reader.deliver(status='unknown');self.retry();self.reader.deliver()
        until(lambda:self.posts()==1 and self.client.view['phase']=='synced')
        self.assertEqual(self.client._native_failures,(None,0,0.))
        self.copy('another real synthetic Copy');self.reader.deliver(status='unknown')
        self.assertEqual(self.client._native_failures[:2],(self.clip.sequence,1));self.assertEqual(self.client._native_retry.interval(),250)
    def test_late_old_sequence_callback_cannot_increment_new_request(self):
        self.copy();self.reader.deliver(status='unknown');old=self.retry();old_callback=self.reader.active[old][0]
        new=self.copy('new sequence original');self.assertIn(old,self.reader.cancelled)
        before=self.client._native_failures
        old_callback(dict(status='unknown',valid=False,value=None,known_empty=False,sequence_before=self.clip.sequence-1,sequence_after=self.clip.sequence-1),None)
        self.assertEqual(self.client._native_failures,before);self.assertEqual(self.client._native_read_token,new)
        self.reader.deliver(new,status='unknown');self.assertEqual(self.client._native_failures[:2],(self.clip.sequence,1));self.assertEqual(self.posts(),0)
    def test_account_epoch_cancels_retry_and_late_callback_cannot_touch_new_baseline(self):
        self.copy();self.reader.deliver(status='unknown');old=self.retry();old_callback=self.reader.active[old][0]
        self.peer.token='new synthetic token';self.peer.account_changed.emit(self.peer.account);new=self.client._native_read_token
        self.assertIn(old,self.reader.cancelled);before=self.client._native_failures
        old_callback(dict(status='unknown',valid=False,value=None,known_empty=False),None)
        self.assertEqual(self.client._native_failures,before);self.assertEqual(self.client._native_read_token,new)
        self.assertEqual(self.posts(),0);self.assertEqual(self.notices,[])
    def test_close_releases_waiters_timer_and_cancels_inflight_callback(self):
        self.copy();self.reader.deliver(status='unknown');old=self.retry();callback=self.reader.active[old][0]
        self.client.close();self.assertTrue(self.reader.closed);self.assertIn(old,self.reader.cancelled)
        self.assertFalse(self.client._native_retry.isActive());self.assertFalse(self.client._native_waiters)
        before=self.client._native_failures;callback(dict(status='unknown',valid=False,value=None,known_empty=False),None)
        self.assertEqual(self.client._native_failures,before);self.assertEqual(self.posts(),0);self.assertEqual(self.notices,[])
    def test_unavailable_none_sequence_also_has_bounded_unknown_retries(self):
        with patch.object(self.client,'_sequence',return_value=None):
            self.client._native_sample=None;self.client._ensure_native(lambda:None,'synthetic-boundary')
            for attempt in range(1,4):
                if attempt>1:self.retry()
                self.reader.deliver(status='unknown');self.assertEqual(self.client._native_failures[:2],(None,attempt))
            self.assertFalse(self.client._native_retry.isActive());self.assertEqual(self.posts(),0)
    def test_thirty_second_guard_allows_new_probe_at_existing_boundary(self):
        self.copy()
        for attempt in range(3):
            if attempt:self.retry()
            self.reader.deliver(status='unknown')
        sequence,count,stamp=self.client._native_failures;calls=self.reader.serial
        with patch.object(sync.time,'monotonic',return_value=stamp+29.999):self.client._start_native_read()
        self.assertEqual(self.reader.serial,calls)
        with patch.object(sync.time,'monotonic',return_value=stamp+30.):self.client._start_native_read()
        self.assertEqual(self.reader.serial,calls+1,dict(sequence=sequence,count=count,stamp=stamp,
            boundary=stamp+30.,elapsed=(stamp+30.)-stamp,failures=self.client._native_failures,
            reader_pending=self.client._native_read_token is not None,waiters=sorted(self.client._native_waiters)))
        self.assertEqual(self.posts(),0)

if __name__=='__main__':unittest.main(verbosity=2)
