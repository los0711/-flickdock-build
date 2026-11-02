"""Same-session intent fences survive switches, never restart cancelled copies."""
import hashlib,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from PySide6.QtCore import QMimeData,QUrl
from PySide6.QtGui import QImage,QColor
import clipboard_sync as subject
from test_shared_clipboard_files_v1821 import Peer
from test_clipboard_loop_v1824 import SequencedClipboard
from test_clipboard_sync_v1819 import APP,Events,until,flush


class ReenableTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-reenable-')
        self.base=Path(self.temp.name);self.offset=0;self.version=0
        self.wall=round(time.time()*1000)-60000;self.mono=time.monotonic()
        self.clock=SimpleNamespace(time=lambda:(self.wall+self.offset)/1000,
            monotonic=lambda:self.mono+self.offset/1000,
            time_ns=lambda:(self.wall+self.offset)*1000000)
        self.time_patch=patch.object(subject,'time',self.clock);self.time_patch.start()
        self.peer=Peer();request=self.peer.request
        def timed_request(method,path,callback,**options):
            def done(result,error):
                if isinstance(result,dict):result=dict(result,server_time=self.wall+self.offset)
                callback(result,error)
            return request(method,path,done,**options)
        self.peer.request=timed_request;self.clip=SequencedClipboard()
        self.client=subject.ClipboardSync(self.peer,Events(),self.base,enabled=False,clipboard=self.clip)
        self.client._sequence=lambda:self.clip.sequence
        self.client._last_sequence=self.clip.sequence;self.client.enabled=True
        self.results=[];self.client.copy_result.connect(lambda ok,context:self.results.append(ok))
    def tearDown(self):
        self.client.close();until(lambda:not self.client._jobs);flush()
        self.time_patch.stop();self.temp.cleanup()
    def settle(self):
        # Drain normal debounce/Qt queues; copy ordering uses the controlled
        # clock above, not elapsed sleeps or timing-dependent timestamp guesses.
        stop=time.monotonic()+.09
        while time.monotonic()<stop:APP.processEvents();time.sleep(.002)
    def head(self,at,version=1,label='synthetic committed peer'):
        self.version=max(version,self.version+1)
        item=subject.inline_text(('text',dict(text=label,html='')))
        head=dict(id=str(uuid4()),version=self.version,ready=True,kind='text',size=item['size'],
            sha256=item['sha256'],inline=item['inline']['body'],copied_at=at)
        self.peer.head=head;self.peer.heads[head['id']]=head;return head
    def receive_old(self):
        old=self.head(self.wall-1000);self.client.refresh()
        until(lambda:self.client._seen==old['id'] and not self.client._fetching)
        return old
    def copy_file(self,size=8*1024*1024,wait_for_put=True):
        self.offset+=100
        path=self.base/'original.bin';path.write_bytes(bytes(range(256))*(size//256)+b'x'*(size%256))
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(path))])
        mime.setData('application/x-synthetic-native-source',b'private original format')
        self.clip.setMimeData(mime);self.client.change_debounce.stop();self.client._changed()
        identity=self.client.view['id']
        until(lambda:identity in self.peer.held if wait_for_put else bool(self.client._pending and self.client._pending.get('prepared')))
        return path,mime
    def test_cancelled_file_stays_natively_pasteable_after_reenable_without_reupload(self):
        old=self.receive_old();path,mime=self.copy_file();digest=hashlib.sha256(path.read_bytes()).digest()
        at=self.client._local_fence['at'];self.assertGreater(at,old['copied_at'])
        before=len(self.peer.calls);writes=self.clip.writes
        self.client.set_enabled(False);self.offset+=10000;self.client.set_enabled(True);self.settle()
        self.assertIs(self.clip.mimeData(),mime)
        self.assertTrue(Path(mime.urls()[0].toLocalFile()).samefile(path))
        self.assertTrue(mime.hasFormat('application/x-synthetic-native-source'))
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(),digest)
        self.assertEqual(self.clip.writes,writes);self.assertEqual(self.client._local_fence['at'],at)
        self.assertIsNone(self.client._pending);self.assertFalse(self.results)
        self.assertFalse(any(m in ('POST','PUT') for m,p,o in self.peer.calls[before:]))
    def test_remote_copy_after_original_intent_still_arrives_before_reenable_time(self):
        self.receive_old();self.copy_file();at=self.client._local_fence['at']
        self.client.set_enabled(False);self.offset+=100
        newer=self.head(self.wall+self.offset,version=3,label='synthetic genuinely newer peer')
        self.offset+=10000;self.client.set_enabled(True)
        until(lambda:self.client._seen==newer['id'])
        self.assertGreater(newer['copied_at'],at);self.assertTrue(self.clip.mimeData().hasText())
        self.assertIsNone(self.client._local_fence)
    def test_changed_native_content_while_off_drops_previous_intent_without_uploading_it(self):
        old=self.receive_old();self.copy_file();self.client.set_enabled(False)
        self.clip.text('synthetic local-only while off');before=len(self.peer.calls)
        self.client.set_enabled(True);until(lambda:self.client._seen==old['id'])
        self.assertTrue(self.clip.mimeData().hasText());self.assertIsNone(self.client._local_fence)
        self.assertFalse(any(m in ('POST','PUT') for m,p,o in self.peer.calls[before:]))
    def test_peer_created_while_disabled_is_received_normally(self):
        self.receive_old();self.client.set_enabled(False);self.clip.text('synthetic off-only copy')
        self.offset+=100;newer=self.head(self.wall+self.offset,version=2)
        self.client.set_enabled(True);until(lambda:self.client._seen==newer['id'])
        self.assertFalse(self.client._pending);self.assertFalse(self.results)
    def test_account_and_token_changes_cannot_inherit_previous_copy_fence(self):
        for change in ('account','token'):
            with self.subTest(change=change):
                old=self.receive_old();self.copy_file();self.client.set_enabled(False)
                if change=='account':self.peer.account={'id':str(uuid4())}
                else:self.peer.token='different-synthetic-token'
                self.peer.account_changed.emit(self.peer.account)
                self.client.set_enabled(True);until(lambda:self.client._seen==old['id'])
                self.assertIsNone(self.client._local_fence)
    def test_rapid_toggles_and_late_cancelled_put_callback_cannot_restore_task_or_notice(self):
        self.receive_old();callbacks=[];original=self.peer.request
        def request(method,path,callback,**options):
            if method=='PUT':callbacks.append(callback)
            return original(method,path,callback,**options)
        self.peer.request=request;path,mime=self.copy_file();task=self.client._pending;at=task['intent']['at']
        before=len(self.peer.calls);self.client.set_enabled(False)
        for _ in range(3):
            self.client.set_enabled(True);self.settle();self.client.set_enabled(False)
        self.client.set_enabled(True);self.settle()
        for callback in callbacks:callback(dict(clipboard=dict(self.peer.staged[task['id']],ready=True)),None)
        self.settle();self.assertIs(self.clip.mimeData(),mime)
        self.assertEqual(self.client._local_fence['at'],at);self.assertFalse(self.results)
        self.assertFalse(any(m in ('POST','PUT') for m,p,o in self.peer.calls[before:]))
    def test_uncalibrated_original_mono_is_calibrated_without_becoming_reenable_time(self):
        old=self.head(self.wall-1000);original=self.peer.request;held=[];block=[True]
        def request(method,path,callback,**options):
            if block[0] and method=='GET' and path=='/v1/clipboard':held.append(callback);return str(uuid4())
            return original(method,path,callback,**options)
        self.peer.request=request;path,mime=self.copy_file(size=1024,wait_for_put=False)
        until(lambda:bool(held));self.assertIsNone(self.client._local_fence['at'])
        original_time=self.wall+self.offset;self.client.set_enabled(False)
        block[0]=False;self.offset+=10000;before=len(self.peer.calls)
        self.client.set_enabled(True);self.settle()
        self.assertIs(self.clip.mimeData(),mime)
        self.assertEqual(self.client._local_fence['at'],original_time)
        self.assertGreater(original_time,old['copied_at'])
        for callback in held:callback(dict(clipboard=old,server_time=self.wall+self.offset),None)
        self.settle();self.assertIs(self.clip.mimeData(),mime)
        self.assertFalse(any(m in ('POST','PUT') for m,p,o in self.peer.calls[before:]))
    def test_copy_during_enable_baseline_read_is_not_consumed_as_the_older_sample(self):
        self.client.set_enabled(False);self.clip.text('synthetic baseline B')
        original=self.clip.mimeData;changed=[False]
        def change_during_read():
            value=original()
            if not changed[0]:changed[0]=True;self.clip.text('synthetic new copy C')
            return value
        with patch.object(self.clip,'mimeData',side_effect=change_during_read):self.client.set_enabled(True)
        until(lambda:self.peer.head is not None and self.client._pending is None)
        self.assertEqual(self.clip.mimeData().text(),'synthetic new copy C')
        self.assertEqual(sum(m=='POST' for m,p,o in self.peer.calls),1)
        self.assertEqual(self.client._last_local_signature,subject.signature(self.client._snapshot()))
    def test_account_baseline_same_content_sequence_change_never_exports_previous_content(self):
        self.clip.text('synthetic previous-account baseline');self.client.change_debounce.stop()
        self.peer.token='new-session';self.peer.account={'id':str(uuid4())}
        original=self.clip.mimeData;changed=[False]
        def reannounce_during_read():
            value=original()
            if not changed[0]:changed[0]=True;self.clip.reannounce()
            return value
        with patch.object(self.clip,'mimeData',side_effect=reannounce_during_read):
            self.peer.account_changed.emit(self.peer.account)
        self.settle();self.assertFalse(any(m in ('POST','PUT') for m,p,o in self.peer.calls))
        self.assertEqual(self.client._last_local_signature,subject.signature(self.client._snapshot()))
    def test_peer_preemption_cannot_discard_unsettled_account_baseline_proof(self):
        self.clip.text('synthetic previous-account baseline');self.client.change_debounce.stop()
        self.peer.token='new-session';self.peer.account={'id':str(uuid4())}
        original=self.clip.mimeData;changed=[False]
        def reannounce_during_read():
            value=original()
            if not changed[0]:changed[0]=True;self.clip.reannounce()
            return value
        with patch.object(self.clip,'mimeData',side_effect=reannounce_during_read):
            self.peer.account_changed.emit(self.peer.account)
        # Start the real refresh entry with a held initial GET, then deliver a
        # pushed new head before the baseline reader consumes its notification.
        request=self.peer.request
        def held_get(method,path,callback,**options):
            if method=='GET' and path=='/v1/clipboard':return str(uuid4())
            return request(method,path,callback,**options)
        newer=self.head(self.wall+self.offset+1,version=5)
        with patch.object(self.peer,'request',side_effect=held_get):
            self.client.refresh();self.assertTrue(self.client._fetching)
            generation=self.client._generation
            self.client._pushed(dict(clipboard=newer))
            self.assertGreater(self.client._generation,generation)
            self.settle()
        self.assertFalse(any(m in ('POST','PUT') for m,p,o in self.peer.calls))
    def test_permission_loss_and_unreadable_baseline_drop_paused_intent(self):
        self.receive_old();self.copy_file();self.client.set_enabled(False);before=len(self.peer.calls)
        with patch.object(self.client,'_native_accessible',return_value=False):
            self.client.set_enabled(True);self.settle()
            self.assertFalse(self.client._suspended_intent);self.assertIsNone(self.client._local_fence)
            self.assertEqual(len(self.peer.calls),before)
        self.client.set_enabled(False)
        with patch.object(self.client,'_snapshot',return_value=None):self.client.set_enabled(True)
        self.assertIsNone(self.client._local_fence);self.assertFalse(self.client._suspended_intent)
    def test_oversize_local_copy_keeps_native_paste_without_resuming_upload(self):
        self.receive_old()
        # The real 100MiB boundary is streamed in the size-contract module;
        # exercise this ordering invariant without a >100MiB text allocation.
        with patch.object(subject,'MAX_BYTES',65536):
            self.clip.text('x'*65537);mime=self.clip.mimeData()
            self.client.change_debounce.stop();self.client._changed()
            until(lambda:self.client._pending is None and not self.client._jobs)
            at=self.client._local_fence['at'];before=len(self.peer.calls)
            self.client.set_enabled(False);self.client.set_enabled(True)
            self.settle();self.assertIs(self.clip.mimeData(),mime)
            self.assertEqual(self.client._local_fence['at'],at)
            self.assertFalse(any(m in ('POST','PUT') for m,p,o in self.peer.calls[before:]))
    def test_pure_native_image_paused_before_encoding_is_not_replaced_by_old_head(self):
        self.receive_old();self.offset+=100
        image=QImage(40,30,QImage.Format_ARGB32);image.fill(QColor('#336699'))
        mime=QMimeData();mime.setImageData(image);mime.setData('application/x-synthetic-native-source',b'original')
        with patch.object(self.client,'_work',return_value=object()):
            self.clip.setMimeData(mime);self.client.change_debounce.stop();self.client._changed()
        self.assertFalse(self.client._pending['prepared']);at=self.client._local_fence['at']
        before=len(self.peer.calls);self.client.set_enabled(False);self.offset+=10000
        self.client.set_enabled(True);self.settle()
        self.assertIs(self.clip.mimeData(),mime);self.assertEqual(mime.imageData(),image)
        self.assertEqual(self.client._local_fence['at'],at)
        self.assertFalse(any(m in ('POST','PUT') for m,p,o in self.peer.calls[before:]))
    def test_same_session_none_baseline_then_true_copy_uses_normal_copy_reader(self):
        self.client.set_enabled(False);self.clip.clear();original=self.clip.mimeData;changed=[False]
        def change_during_read():
            value=original()
            if not changed[0]:changed[0]=True;self.offset+=100;self.clip.text('synthetic true copy from empty')
            return value
        with patch.object(self.clip,'mimeData',side_effect=change_during_read):self.client.set_enabled(True)
        until(lambda:self.peer.head is not None and self.client._pending is None)
        self.assertEqual(self.clip.mimeData().text(),'synthetic true copy from empty')
        self.assertEqual(sum(m=='POST' for m,p,o in self.peer.calls),1)
    def test_unknown_account_baseline_is_not_exported_or_clobbered_by_older_peer(self):
        self.head(self.wall-1000);self.clip.clear();self.client.change_debounce.stop()
        self.peer.account={'id':str(uuid4())};self.peer.token='new-synthetic-session'
        original=self.clip.mimeData;changed=[False]
        def change_during_read():
            value=original()
            if not changed[0]:changed[0]=True;self.offset+=100;self.clip.text('synthetic unclassified native value')
            return value
        with patch.object(self.clip,'mimeData',side_effect=change_during_read):self.peer.account_changed.emit(self.peer.account)
        self.settle();self.assertEqual(self.clip.mimeData().text(),'synthetic unclassified native value')
        at=self.client._local_fence['at']
        for _ in range(3):self.clip.reannounce();self.client.change_debounce.stop();self.client._changed()
        self.assertFalse(any(m in ('POST','PUT') for m,p,o in self.peer.calls))
        self.offset+=100;newer=self.head(self.wall+self.offset,version=3)
        self.client.refresh();until(lambda:self.client._seen==newer['id'])
        self.assertGreater(newer['copied_at'],at);self.assertIsNone(self.client._baseline_guard)
        self.assertFalse(any(m in ('POST','PUT') for m,p,o in self.peer.calls))

    def _disabled_identity_read_race(self,change,stable=False):
        self.client.set_enabled(False);self.clip.text('synthetic previous-session baseline')
        if change=='account':self.peer.account={'id':str(uuid4())}
        else:self.peer.token='new-token-'+str(uuid4())
        self.peer.account_changed.emit(self.peer.account)
        snapshot=self.client._snapshot;first=[True]
        def unreadable():
            if first[0]:
                first[0]=False
                if not stable:self.clip.reannounce()
                return None
            return snapshot()
        with patch.object(self.client,'_snapshot',side_effect=unreadable):self.client.set_enabled(True)
        if stable:self.clip.reannounce()
        self.settle()
        self.assertFalse(any(m in ('POST','PUT') for m,p,o in self.peer.calls))
        for _ in range(2):self.clip.reannounce();self.client.change_debounce.stop();self.client._changed()
        self.assertFalse(any(m in ('POST','PUT') for m,p,o in self.peer.calls))
    def test_account_change_while_disabled_keeps_nonexporting_baseline_until_enable(self):
        self._disabled_identity_read_race('account')
    def test_token_change_while_disabled_keeps_nonexporting_baseline_until_enable(self):
        self._disabled_identity_read_race('token')
    def test_stable_unreadable_new_session_then_late_format_never_exports_old_content(self):
        self._disabled_identity_read_race('account',stable=True)
    def test_held_old_confirm_cannot_overwrite_guard_reassert_or_advance_noise_time(self):
        self.clip.text('synthetic stable security baseline');self.client.change_debounce.stop()
        self.head(self.wall-1000);self.peer.account={'id':str(uuid4())};self.peer.token='new-session'
        request=self.peer.request;gets=[0];held=[]
        def hold_confirm(method,path,callback,**options):
            if method=='GET' and path=='/v1/clipboard':
                gets[0]+=1
                if gets[0]==2:return request(method,path,lambda r,e:held.append(lambda:callback(r,e)),**options)
            return request(method,path,callback,**options)
        self.peer.request=hold_confirm;self.peer.account_changed.emit(self.peer.account)
        until(lambda:bool(held));native=self.clip.mimeData()
        self.offset+=100;self.clip.reannounce();self.client.change_debounce.stop();self.client._changed()
        at=self.client._local_fence['at'];held.pop()();self.settle()
        self.assertIs(self.clip.mimeData(),native);count=len(self.peer.calls)
        for _ in range(3):
            self.offset+=1000;self.clip.reannounce();self.client.change_debounce.stop();self.client._changed()
        self.settle();self.assertEqual(len(self.peer.calls),count)
        self.assertEqual(self.client._local_fence['at'],at)
        self.assertFalse(any(m in ('POST','PUT') for m,p,o in self.peer.calls))
        newer=self.head(at+1,version=3);self.client.refresh();until(lambda:self.client._seen==newer['id'])
        self.assertIsNone(self.client._local_fence);self.assertIsNone(self.client._baseline_guard)
        self.assertFalse(self.results)
        self.assertFalse(any(m in ('POST','PUT') for m,p,o in self.peer.calls))

    def _startup_baseline(self,kind):
        self.client.close();clip=SequencedClipboard();mime=QMimeData()
        if kind=='text':mime.setText('synthetic pre-startup content A')
        elif kind=='file':
            source=self.base/'pre-startup.bin';source.write_bytes(b'original pre-startup file bytes')
            mime.setUrls([QUrl.fromLocalFile(str(source))]);mime.setData('application/x-synthetic-private',b'original')
        clip.setMimeData(mime);request=self.peer.request;held=[];block=[True]
        def hold_get(method,path,callback,**options):
            if block[0] and method=='GET' and path=='/v1/clipboard':held.append(callback);return str(uuid4())
            return request(method,path,callback,**options)
        self.peer.request=hold_get
        self.client=subject.ClipboardSync(self.peer,Events(),self.base/'startup',enabled=True,clipboard=clip)
        self.client._sequence=lambda:clip.sequence;self.client._last_sequence=clip.sequence
        self.clip=clip;starts=[];results=[]
        self.client.copy_started.connect(lambda:starts.append(True))
        self.client.copy_result.connect(lambda ok,context:results.append(ok))
        until(lambda:bool(held))
        if kind!='empty':
            clip.reannounce();self.settle()
            self.assertFalse(starts);self.assertIsNone(self.client._pending)
        block[0]=False
        for callback in held:callback(dict(clipboard=None,server_time=self.wall+self.offset),None)
        self.settle();self.assertFalse(any(m in ('POST','PUT') for m,p,o in self.peer.calls))
        self.assertIs(clip.mimeData(),mime);self.assertFalse(results)
        # A truly different copy still takes the normal production path.
        self.offset+=100;clip.text('synthetic genuinely new startup copy D')
        until(lambda:self.peer.head is not None and self.client._pending is None)
        self.assertEqual(starts,[True]);self.assertEqual(results,[True])
        self.assertEqual(sum(m=='POST' for m,p,o in self.peer.calls),1)
        self.offset+=100;newer=self.head(self.wall+self.offset,version=5)
        self.client.refresh();until(lambda:self.client._seen==newer['id'])
    def test_actual_constructor_preexisting_text_reannounce_never_creates_copy_task(self):
        self._startup_baseline('text')
    def test_actual_constructor_preexisting_file_reannounce_never_creates_copy_task(self):
        self._startup_baseline('file')
    def test_actual_constructor_empty_clipboard_first_true_copy_still_uploads(self):
        self._startup_baseline('empty')
    def _startup_read_race(self,same_content=False,empty=False):
        self.client.close();clip=SequencedClipboard()
        if not empty:clip.text('synthetic startup sample A')
        original=clip.mimeData;changed=[False]
        def read_and_change():
            value=original()
            if not changed[0]:
                changed[0]=True;self.offset+=100
                if same_content:clip.reannounce()
                else:clip.text('synthetic actual new startup copy C')
            return value
        # Synthetic sequence plumbing during the actual constructor, with no
        # system clipboard access or native event registrations.
        with patch.object(subject.QApplication,'clipboard',return_value=clip),patch.object(subject,'native_sequence',side_effect=lambda:clip.sequence),patch.object(clip,'mimeData',side_effect=read_and_change):
            self.client=subject.ClipboardSync(self.peer,Events(),self.base/'startup-race',enabled=True,clipboard=clip)
        if same_content:
            self.settle();clip.reannounce();self.settle()
            self.assertFalse(any(m in ('POST','PUT') for m,p,o in self.peer.calls))
        else:
            until(lambda:self.peer.head is not None and self.client._pending is None)
            self.assertEqual(clip.mimeData().text(),'synthetic actual new startup copy C')
            self.assertEqual(sum(m=='POST' for m,p,o in self.peer.calls),1)
    def test_actual_constructor_read_race_preserves_new_nonempty_copy_event(self):
        self._startup_read_race()
    def test_actual_constructor_read_race_from_empty_preserves_first_copy_event(self):
        self._startup_read_race(empty=True)
    def test_actual_constructor_read_same_content_noise_then_more_noise_stays_nonexporting(self):
        self._startup_read_race(same_content=True)


if __name__=='__main__':unittest.main()
