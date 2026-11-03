"""Trusted metadata-empty bootstrap, fake native providers and local peer only."""
import ctypes,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace,ModuleType
from dataclasses import FrozenInstanceError,replace
from unittest.mock import patch
from uuid import uuid4
import win_clipboard_native as native
import clipboard_sync as sync
from test_clipboard_native_read_v1825 import NativeClipboard,Reader
from test_clipboard_sync_v1819 import Events,retire_clipboard_clients
from test_shared_clipboard_files_v1821 import Peer
from test_html_native_workspace import APP,flush,until

_PRODUCTION_NATIVE_SEQUENCE=sync.native_sequence

class MetadataProvider:
    def __init__(self,clip):self.clip=clip;self.busy=False;self.count_error=None;self.close_error=None;self.force_count=None;self.after_close=None;self.reads=0;self.closes=0;self.counts=0
    def sequence(self):return self.clip.sequence
    def open(self):return not self.busy
    def count(self):
        self.counts+=1
        if self.count_error:raise self.count_error
        return self.force_count if self.force_count is not None else (0 if self.clip.value is None else 1)
    def close(self):
        self.closes+=1
        hook,self.after_close=self.after_close,None
        if hook:hook()
        if self.close_error:raise self.close_error
    def read(self,*a):self.reads+=1;raise AssertionError('Metadata read payload')

class EmptyProofFactoryTests(unittest.TestCase):
    def setUp(self):self.clip=NativeClipboard();self.clip.sequence=0;self.provider=MetadataProvider(self.clip)
    def test_zero_empty_issues_only_scoped_immutable_proof_not_valid_sample(self):
        context=object()
        with native.empty_proof_scope(context):
            result=native.native_metadata(self.provider);proof=result['empty_proof']
            self.assertEqual(result['status'],'unknown');self.assertFalse(result['valid']);self.assertFalse(result['known_empty']);self.assertIsNone(result['value'])
            with self.assertRaises(FrozenInstanceError):proof.sequence=1
            self.assertTrue(native.consume_empty_proof(proof,context,0));self.assertFalse(native.consume_empty_proof(proof,context,0))
        self.assertEqual(self.provider.reads,0);self.assertEqual(self.provider.closes,1)
    def test_later_positive_copy_is_not_current_empty_but_may_follow_historical_anchor(self):
        context=object()
        with native.empty_proof_scope(context):
            proof=native.native_metadata(self.provider)['empty_proof'];self.clip.copy('after exclusive empty check')
            self.assertTrue(native.consume_empty_proof(proof,context,self.clip.sequence))
    def test_unavailable_counter_cannot_borrow_a_zero_empty_proof(self):
        context=object()
        with native.empty_proof_scope(context):
            proof=native.native_metadata(self.provider)['empty_proof']
            self.assertFalse(native.consume_empty_proof(proof,context,None))
            self.assertTrue(native.consume_empty_proof(proof,context,0))
    def test_forgery_cached_old_scope_and_newer_metadata_serial_are_rejected(self):
        one=object()
        with native.empty_proof_scope(one):
            proof=native.native_metadata(self.provider)['empty_proof']
            self.assertFalse(native.consume_empty_proof({'sequence':0},one,0));self.assertFalse(native.consume_empty_proof(replace(proof),one,0))
            native.native_metadata(self.provider);self.assertFalse(native.consume_empty_proof(proof,one,0))
        with native.empty_proof_scope(object()):self.assertFalse(native.consume_empty_proof(proof,one,0))
    def test_busy_nonempty_count_error_close_error_and_counter_change_issue_no_proof(self):
        for mode in ('busy','nonempty','count_error','close_error','changed'):
            with self.subTest(mode=mode):
                clip=NativeClipboard();clip.sequence=0;provider=MetadataProvider(clip)
                if mode=='busy':provider.busy=True
                if mode=='nonempty':provider.force_count=1
                if mode=='count_error':provider.count_error=OSError(5,'synthetic count failure')
                if mode=='close_error':provider.close_error=OSError(5,'synthetic close failure')
                if mode=='changed':
                    count=provider.count
                    def changed():result=count();clip.sequence=1;return result
                    provider.count=changed
                with native.empty_proof_scope(object()):self.assertNotIn('empty_proof',native.native_metadata(provider))
                self.assertEqual(provider.reads,0)

class EmptyBootstrapFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-empty-bootstrap-');self.clip=NativeClipboard();self.clip.sequence=0;self.peer=Peer();self.reader=Reader(self.clip);self.provider=MetadataProvider(self.clip);self.client=None
        self.patches=[patch.object(sync,'_windows_clipboard_reader',return_value=self.reader),patch.object(sync,'native_sequence',side_effect=lambda:self.clip.sequence),patch.object(sync.QApplication,'clipboard',return_value=self.clip),patch.object(sync,'_windows_clipboard_metadata',side_effect=lambda:native.native_metadata(self.provider))]
        for item in self.patches:item.start()
    def tearDown(self):
        if self.client is not None:retire_clipboard_clients([self.client])
        for item in reversed(self.patches):item.stop()
        self.temp.cleanup()
    def start(self):
        # Canonicalize this newly owned fixture, never arbitrary application
        # input: macOS's /var alias must not defeat the real no-links worker.
        base=Path(self.temp.name).resolve(strict=True)
        self.client=sync.ClipboardSync(self.peer,Events(),base,clipboard=self.clip);flush();return self.client
    def posts(self):return sum(method=='POST' and path=='/v1/clipboard' for method,path,data in self.peer.calls)
    def copy(self,text):
        self.clip.copy(text);self.client.change_debounce.stop();self.client._changed();token=self.client._native_read_token;self.reader.deliver(token);until(lambda:self.posts()==1 and self.client.view.get('phase')=='synced')
    def test_first_copy_after_zero_empty_bootstrap_posts_once_without_zero_sample(self):
        client=self.start();notices=[];client.copy_result.connect(lambda ok,context:notices.append(ok))
        self.assertIsNone(client._native_sample);self.assertFalse(client._needs_security_baseline);self.assertNotIn('baseline',client._native_waiters);self.assertEqual(self.posts(),0)
        self.copy('first real Copy from empty');self.assertEqual(notices,[True]);self.assertEqual(self.clip.writes,0);self.assertEqual(self.clip.reads,0);self.assertEqual(self.provider.reads,0)
        self.assertGreater(client._last_sequence,0);self.assertEqual(client.view['value'],self.clip.value)
    def test_copy_after_count_close_before_consumption_is_not_absorbed(self):
        self.provider.after_close=lambda:self.clip.copy('Copy between close and proof consumption')
        client=self.start();self.assertEqual(client._last_sequence,0);self.assertNotIn('baseline',client._native_waiters)
        client.change_debounce.stop();client._changed();self.reader.deliver();until(lambda:self.posts()==1 and client.view.get('phase')=='synced')
        self.assertEqual(client.view['value'],self.clip.value)
    def test_late_old_baseline_even_cancelled_and_retry_cannot_reset_new_copy(self):
        self.provider.busy=True;client=self.start();old=client._native_read_token
        self.assertIn('baseline',client._native_waiters);old_callback=self.reader.active[old][0]
        self.provider.busy=False;client._account_changed(force=True)
        self.assertIn(old,self.reader.cancelled);self.assertNotIn('baseline',client._native_waiters)
        self.copy('new Copy after baseline reader retired');view=dict(client.view);sequence=client._last_sequence
        old_callback(dict(status='ready',valid=True,known_empty=True,value=None,marker=b'',sequence=0,sequence_before=0,sequence_after=0),None)
        client._start_native_read();flush();self.assertEqual(self.posts(),1);self.assertEqual(client.view,view);self.assertEqual(client._last_sequence,sequence)
    def test_same_session_forced_refresh_does_not_rearm_bootstrap(self):
        client=self.start();epoch=client._baseline_epoch;counts=self.provider.counts
        for unused in range(3):client._account_changed(force=True)
        self.assertEqual(client._baseline_epoch,epoch);self.assertEqual(self.provider.counts,counts);self.assertNotIn('baseline',client._native_waiters)
        self.copy('Copy after repeated settings refresh');self.assertEqual(self.posts(),1)
    def test_historical_proof_preserves_already_started_positive_change_reader(self):
        self.provider.busy=True;client=self.start();old=client._native_read_token
        def new_copy():
            self.clip.copy('Copy while metadata scope is returning')
            client.change_debounce.stop();client._changed()
        self.provider.busy=False;self.provider.after_close=new_copy
        self.assertTrue(client._sample_empty_native_metadata());new=client._native_read_token
        self.assertNotEqual(old,new);self.assertIn(old,self.reader.cancelled)
        self.assertNotIn(new,self.reader.cancelled);self.assertIn('change',client._native_waiters);self.assertNotIn('baseline',client._native_waiters)
        self.reader.deliver(old,even_cancelled=True);self.assertEqual(self.posts(),0)
        self.reader.deliver(new);until(lambda:self.posts()==1 and client.view.get('phase')=='synced')
        self.assertEqual(client.view['value'],self.clip.value)
    def test_late_proof_does_not_backstamp_positive_sample_or_clear_copy_fence(self):
        self.provider.busy=True;client=self.start();fence={'synthetic':'current Copy owner'}
        def positive_sample():
            self.clip.copy('verified Copy already processed')
            client.change_debounce.stop();client._native_sample=dict(status='ready',valid=True,value=self.clip.value,known_empty=False,sequence=self.clip.sequence,marker=b'')
            client._last_sequence=self.clip.sequence;client._last_local_signature=sync.signature(self.clip.value);client._local_fence=fence
        self.provider.busy=False;self.provider.after_close=positive_sample
        self.assertTrue(client._sample_empty_native_metadata())
        self.assertEqual(client._last_sequence,self.clip.sequence);self.assertGreater(client._last_sequence,0)
        self.assertEqual(client._last_local_signature,sync.signature(self.clip.value));self.assertIs(client._local_fence,fence)
    def test_scope_mutations_during_metadata_reject_old_request(self):
        self.provider.busy=True;client=self.start();self.provider.busy=False
        for mutation in ('epoch','token','device','account','off','closed','permission'):
            with self.subTest(mutation=mutation):
                old=(client._baseline_epoch,self.peer.token,self.peer.device_id,self.peer.account,client.enabled,client.closed)
                allowed=[True]
                def boundary():
                    if mutation=='epoch':client._baseline_epoch+=1
                    elif mutation=='token':self.peer.token='another synthetic session'
                    elif mutation=='device':self.peer.device_id=str(uuid4())
                    elif mutation=='account':self.peer.account={'id':str(uuid4())}
                    elif mutation=='off':client.enabled=False
                    elif mutation=='closed':client.closed=True
                    else:allowed[0]=False
                self.provider.after_close=boundary
                with patch.object(client,'_native_accessible',side_effect=lambda:allowed[0]):
                    self.assertFalse(client._sample_empty_native_metadata())
                self.assertIsNone(client._empty_bootstrap);self.assertEqual(self.posts(),0)
                client._baseline_epoch,self.peer.token,self.peer.device_id,self.peer.account,client.enabled,client.closed=old
        self.assertTrue(client._sample_empty_native_metadata());self.copy('real Copy after rejected stale scopes')
    def test_access_denial_retires_completion_and_requires_new_safe_baseline(self):
        client=self.start();proof=client._empty_bootstrap
        with patch.object(client,'_native_accessible',return_value=False):
            client._account_changed(force=True)
            self.assertIsNone(client._empty_bootstrap);self.assertGreater(client._baseline_epoch,proof.epoch)
            self.assertTrue(client._needs_security_baseline);self.assertEqual(self.posts(),0)
        client._account_changed(force=True);self.assertIsNotNone(client._empty_bootstrap)
        self.copy('real Copy after native access re-established')
    def test_nonempty_unknown_zero_remains_a_private_baseline(self):
        self.clip.value=('text',dict(text='initial private payload',html=''));self.start()
        self.assertIsNone(self.client._empty_bootstrap);self.assertTrue(self.client._needs_security_baseline)
        self.clip.copy('copy while unknown nonempty baseline');self.client.change_debounce.stop();self.client._changed();self.reader.deliver();flush()
        self.assertEqual(self.posts(),0);self.assertEqual(self.clip.writes,0)
    def test_forged_zero_dictionary_cannot_complete_bootstrap(self):
        with patch.object(sync,'_windows_clipboard_metadata',return_value=dict(status='ready',valid=True,known_empty=True,sequence_before=0,sequence_after=0,empty_proof={'sequence':0})):
            self.start();self.assertIsNone(self.client._empty_bootstrap);self.assertTrue(self.client._needs_security_baseline);self.assertEqual(self.posts(),0)
    def test_off_on_and_account_token_device_epochs_cannot_reuse_empty_completion(self):
        client=self.start();old=client._empty_bootstrap;client.set_enabled(False)
        self.assertIsNone(client._empty_bootstrap);self.assertGreater(client._baseline_epoch,old.epoch)
        client.set_enabled(True);self.assertIsNot(client._empty_bootstrap,old)
        for field,value in (('token','new synthetic auth'),('device_id',str(uuid4()))):
            previous=client._empty_bootstrap;setattr(self.peer,field,value);self.peer.account_changed.emit(self.peer.account)
            self.assertIsNot(client._empty_bootstrap,previous);self.assertGreater(client._baseline_epoch,previous.epoch)
        self.assertEqual(self.posts(),0)

class ProductionSequenceBootstrapTests(EmptyBootstrapFlowTests):
    """Same user flows through the real wrapper, only fake the native API."""
    def setUp(self):
        super().setUp()
        self.patches[1].stop()
        self.assertIs(sync.native_sequence,_PRODUCTION_NATIVE_SEQUENCE)
        sequence=unittest.mock.Mock(side_effect=lambda:self.clip.sequence)
        additions=[patch.object(sync,'native_input_available',return_value=True),patch.object(sync.sys,'platform','win32'),
            patch.object(ctypes,'windll',SimpleNamespace(user32=SimpleNamespace(GetClipboardSequenceNumber=sequence)),create=True)]
        for item in additions:item.start()
        self.patches.extend(additions)

class ProductionSequenceBoundaryTests(unittest.TestCase):
    def test_exact_native_wrapper_preserves_zero_and_unsigned_dword(self):
        for value in (0,1,0x7fffffff,0x80000000,0xffffffff):
            with self.subTest(value=value):
                function=ctypes.CFUNCTYPE(ctypes.c_uint32)(lambda:value)
                with patch.object(sync,'native_input_available',return_value=True),patch.object(sync.sys,'platform','win32'),patch.object(ctypes,'windll',SimpleNamespace(user32=SimpleNamespace(GetClipboardSequenceNumber=function)),create=True):
                    self.assertEqual(_PRODUCTION_NATIVE_SEQUENCE(),value)
        self.assertFalse(sync._valid_windows_sequence(0))
        self.assertTrue(sync._valid_windows_sequence(0xffffffff))
    def test_api_error_and_unavailable_native_input_remain_none(self):
        function=unittest.mock.Mock(side_effect=OSError(5,'synthetic API failure'))
        with patch.object(sync.sys,'platform','win32'),patch.object(ctypes,'windll',SimpleNamespace(user32=SimpleNamespace(GetClipboardSequenceNumber=function)),create=True):
            with patch.object(sync,'native_input_available',return_value=False):self.assertIsNone(_PRODUCTION_NATIVE_SEQUENCE())
            function.assert_not_called()
            with patch.object(sync,'native_input_available',return_value=True):self.assertIsNone(_PRODUCTION_NATIVE_SEQUENCE())
        function.assert_called_once()
    def test_mac_counter_zero_and_positive_are_unchanged(self):
        appkit=ModuleType('AppKit')
        for value in (0,51):
            appkit.NSPasteboard=SimpleNamespace(generalPasteboard=lambda:SimpleNamespace(changeCount=lambda:value))
            with patch.object(sync,'native_input_available',return_value=True),patch.object(sync.sys,'platform','darwin'),patch.dict('sys.modules',{'AppKit':appkit}):
                self.assertEqual(_PRODUCTION_NATIVE_SEQUENCE(),value)

if __name__=='__main__':unittest.main(verbosity=2)
