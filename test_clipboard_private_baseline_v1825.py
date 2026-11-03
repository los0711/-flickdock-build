"""Unknown startup data stays private and cannot be erased by an older peer."""
import tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
import test_clipboard_native_read_v1825 as fixture
from test_html_native_workspace import APP,flush,until
from test_shared_clipboard_files_v1821 import Peer
from test_clipboard_sync_v1819 import Events,retire_clipboard_clients
import clipboard_sync as sync


class PrivateBaselineFenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='fd-private-baseline-');self.clip=fixture.NativeClipboard()
        self.clip.value=('text',dict(text='synthetic unread initial private text',html=''))
        self.reader=fixture.Reader(self.clip);self.peer=Peer();self.patches=[]
        for item in (patch.object(sync,'_windows_clipboard_reader',return_value=self.reader),patch.object(sync,'native_sequence',side_effect=lambda:self.clip.sequence),patch.object(sync.QApplication,'clipboard',return_value=self.clip),patch.object(sync,'_windows_clipboard_metadata',side_effect=lambda:dict(status='ready',valid=True,known_empty=False,sequence_before=self.clip.sequence,sequence_after=self.clip.sequence))):
            item.start();self.patches.append(item)
        self.client=sync.ClipboardSync(self.peer,Events(),self.temp.name,clipboard=self.clip);flush()
        self.assertTrue(self.client._needs_security_baseline);self.old_token=self.client._native_read_token
    def tearDown(self):
        retire_clipboard_clients([self.client])
        for item in reversed(self.patches):item.stop()
        self.temp.cleanup()
    def posts(self):return sum(method=='POST' for method,path,data in self.peer.calls)
    def head(self,text,at,version):
        value=('text',dict(text=text,html=''));item=sync.inline_text(value)['inline']
        self.peer.head=dict(id=str(uuid4()),device=str(uuid4()),version=version,ready=True,kind='text',size=item['size'],sha256=item['sha256'],inline=item['body'],copied_at=at)
        return self.peer.head['id']
    def copy_pending(self):
        self.clip.copy('synthetic first value while initial baseline unavailable');self.client.change_debounce.stop();self.client._changed()
        return self.client._native_read_token
    def settle(self):
        stop=time.monotonic()+.12
        while time.monotonic()<stop:APP.processEvents();time.sleep(.002)
    def test_unknown_first_value_is_not_uploaded_or_overwritten_by_older_head(self):
        identity=self.head('synthetic older peer text',round(time.time()*1000)-100000,1)
        token=self.copy_pending();expected=self.clip.value;self.reader.deliver(token);self.settle()
        self.assertEqual(self.posts(),0);self.assertEqual(self.clip.value,expected);self.assertEqual(self.clip.writes,0)
        self.assertNotEqual(self.client._seen,identity);self.assertIsNotNone(self.client._local_fence)
        self.assertFalse(self.client._needs_security_baseline);self.assertEqual(self.client.view['phase'],'local')
    def test_strictly_newer_peer_still_receives_and_late_zero_or_old_sample_cannot_reclaim(self):
        token=self.copy_pending();self.reader.deliver(token);self.settle()
        self.assertIsNotNone(self.client._local_fence)
        identity=self.head('synthetic genuinely newer peer',self.client._local_fence['at']+10000,2)
        self.client.refresh();until(lambda:self.client._seen==identity)
        self.assertEqual(self.clip.value[1]['text'],'synthetic genuinely newer peer');self.assertEqual(self.clip.writes,1)
        snapshot=dict(self.client._native_sample);self.reader.deliver(self.old_token,even_cancelled=True);flush()
        self.assertEqual(self.client._native_sample,snapshot);self.assertEqual(self.posts(),0)
    def test_duplicate_notifications_never_advance_the_first_private_receive_fence(self):
        token=self.copy_pending();first=self.client._local_fence;self.assertIsNotNone(first);mono=first['mono']
        for unused in range(12):self.clip.sequence+=1;self.clip.dataChanged.emit()
        self.client.change_debounce.stop();self.client._changed();self.reader.deliver()
        self.settle();fence=self.client._local_fence
        self.assertIs(fence,first);self.assertEqual(fence['mono'],mono);at=fence['at'];calls=len(self.peer.calls)
        for unused in range(12):
            self.clip.sequence+=1;self.clip.dataChanged.emit();self.client.change_debounce.stop();self.client._changed();self.reader.deliver();flush()
        self.assertIs(self.client._local_fence,first);self.assertEqual(first['mono'],mono);self.assertEqual(first['at'],at)
        self.assertEqual(self.posts(),0);self.assertEqual(self.clip.writes,0);self.assertFalse(self.client._native_waiters)
        self.assertEqual(len(self.peer.calls),calls)
    def test_later_real_copy_after_private_baseline_still_uploads_once(self):
        self.reader.deliver(self.copy_pending());self.settle();self.assertEqual(self.posts(),0)
        self.clip.copy('synthetic later real Copy after private baseline');self.client.change_debounce.stop();self.client._changed();self.reader.deliver()
        until(lambda:self.posts()==1 and self.client.view['phase']=='synced')
        self.assertEqual(self.clip.value[1]['text'],'synthetic later real Copy after private baseline');self.assertEqual(self.clip.writes,0)
    def test_account_switch_does_not_inherit_private_receive_fence_or_late_callback(self):
        token=self.copy_pending();old=self.client._local_fence;self.assertIsNotNone(old)
        self.peer.account={'id':str(uuid4())};self.peer.token='synthetic-new-session';self.peer.account_changed.emit(self.peer.account)
        self.assertIsNone(self.client._local_fence);self.reader.deliver(token,even_cancelled=True);flush()
        self.assertIsNone(self.client._local_fence);self.assertEqual(self.posts(),0)
    def test_disabled_session_cannot_apply_or_restore_old_private_fence(self):
        token=self.copy_pending();self.assertIsNotNone(self.client._local_fence)
        self.client.set_enabled(False);self.assertIsNone(self.client._local_fence)
        self.head('synthetic peer while disabled',round(time.time()*1000)+100000,2)
        self.reader.deliver(token,even_cancelled=True);self.client.refresh();flush()
        self.assertIsNone(self.client._local_fence);self.assertEqual(self.posts(),0);self.assertEqual(self.clip.writes,0)
    def test_no_notification_does_not_create_a_private_receive_fence(self):
        self.reader.deliver(self.old_token);self.settle()
        self.assertIsNone(self.client._local_fence);self.assertEqual(self.posts(),0)


if __name__=='__main__':unittest.main()
