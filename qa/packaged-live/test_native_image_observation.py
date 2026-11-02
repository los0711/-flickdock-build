"""Fake-only PNG observer tests. No native clipboard, executable or Job APIs."""
import ctypes,hashlib,json,os,struct,sys,tempfile,unittest,zlib
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
import native_image_observation as subject
import native_reader_observation as base
from test_native_reader_observation import FakeAPI,FakeProducer,FakeOperation

def png():
    def chunk(name,data):return struct.pack('!I',len(data))+name+data+struct.pack('!I',zlib.crc32(name+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',640,360,8,6,0,0,0))+chunk(b'IDAT',zlib.compress((b'\0'+bytes((20,60,90,170))*640)*360))+chunk(b'IEND',b'')

class Clipboard:
    def __init__(self,formats):self.formats=formats;self.opened=False;self.reads=[];self.busy=False;self.closed=0
    def open(self):self.opened=not self.busy;return self.opened
    def close(self):self.closed+=1;self.opened=False
    def available(self,name):assert self.opened;return name in self.formats
    def read(self,name,limit):
        assert self.opened;assert name in subject.FORMATS;self.reads.append(name)
        raw=self.formats[name]
        if len(raw)>limit:raise OSError(12,'private payload path')
        return raw

class FormatTests(unittest.TestCase):
    def test_availability_api_errors_cannot_be_reported_as_absent(self):
        import win_clipboard_native as native
        class Native:
            def __init__(self):self.user=type('User',(),{'IsClipboardFormatAvailable':staticmethod(lambda name:False)})();self.identifier=42
            def format(self,name):return self.identifier
        with patch.object(native,'NativeClipboard',Native),patch.object(ctypes,'set_last_error',create=True),patch.object(ctypes,'get_last_error',return_value=5,create=True):
            probe=subject._format_clipboard()
            with self.assertRaises(OSError):probe.available('PNG')
        with patch.object(native,'NativeClipboard',Native),patch.object(ctypes,'set_last_error',create=True),patch.object(ctypes,'get_last_error',return_value=0,create=True):
            probe=subject._format_clipboard();self.assertFalse(probe.available('PNG'))
            probe.identifier=0
            with self.assertRaises(OSError):probe.available('PNG')
    def test_only_advertised_formats_and_full_padding_are_preserved_hash_after_close(self):
        original=png();padded=original+b'\0'*5;clip=Clipboard({'PNG':padded,'image/png':original,'private unknown':b'never read'})
        api=FakeAPI();digest=subject.hashlib.sha256;phases=[]
        def checked(raw):phases.append(clip.opened);return digest(raw)
        with patch.object(subject.hashlib,'sha256',side_effect=checked):row=subject._format_capture(api,clip,(99,321),77,original)
        self.assertEqual(clip.reads,['PNG','image/png']);self.assertEqual(phases,[False,False]);self.assertEqual(clip.closed,1)
        self.assertEqual(row['formats'][0]['size'],len(padded));self.assertEqual(row['formats'][0]['sha256'],digest(padded).hexdigest())
        self.assertEqual(row['formats'][0]['extra_bytes'],5);self.assertTrue(row['formats'][0]['extra_all_zero']);self.assertFalse(row['formats'][0]['matches_original']);self.assertTrue(row['formats'][1]['matches_original'])
        self.assertEqual(subject._checked_formats(row,77,digest(original).hexdigest(),len(original)),row)
    def test_absent_format_does_not_call_getdata_and_busy_does_not_close(self):
        clip=Clipboard({});api=FakeAPI();row=subject._format_capture(api,clip,(99,321),77,png())
        self.assertEqual(clip.reads,[]);self.assertEqual(row['formats'],[dict(format=name,advertised=False) for name in subject.FORMATS])
        clip.busy=True;row=subject._format_capture(api,clip,(99,321),77,png());self.assertEqual(row,{'unavailable':'CLIPBOARD_BUSY'});self.assertEqual(clip.closed,1)
    def test_read_failure_closes_and_owner_change_cannot_read(self):
        clip=Clipboard({'PNG':b'x'*(subject.MAX_SYNTHETIC_BYTES+1)});api=FakeAPI()
        with self.assertRaises(OSError):subject._format_capture(api,clip,(99,321),77,png())
        self.assertFalse(clip.opened);self.assertEqual(clip.closed,1)
        clip.reads.clear();api.owned=(100,999)
        with self.assertRaisesRegex(RuntimeError,'OWNER_CHANGED'):subject._format_capture(api,clip,(99,321),77,png())
        self.assertEqual(clip.reads,[])
    def test_malformed_or_arbitrary_format_report_is_rejected(self):
        valid=subject._format_capture(FakeAPI(),Clipboard({}),(99,321),77,png())
        for wrong in ({'formats':[{'format':'secret name','advertised':False}],'sequence':77},dict(valid,sequence=88),dict(valid,private='secret payload')):
            with self.assertRaises(ValueError):subject._checked_formats(wrong,77,hashlib.sha256(png()).hexdigest(),len(png()))
    def test_child_cli_is_exact_before_native_construction(self):
        for args in ([],['--other'],['--format-child']*14):
            with self.assertRaises(ValueError):subject._child_arguments(args)
        with patch.object(base,'_hosted'),patch.object(base,'HostedNativeReaderObservation',side_effect=AssertionError('native/fixture reached')):
            self.assertEqual(subject.child_main(['--other']),2)
    def test_child_real_parser_and_framing_keep_raw_sha_and_no_native_api(self):
        import win_clipboard_native as native
        from win_clipboard_cache import cache_base
        from win_clipboard_io import decode_frame
        raw=png();api=FakeAPI();clip=Clipboard({'PNG':raw+b'\0','image/png':raw});frames=[]
        nonce='b'*32;fake=type('Observer',(),{'_guard':lambda _:None,'_native':lambda _:api,'producer_profile':Path('owned-fixture/FlickDock')})()
        args=['--format-child','owned-root',nonce,'owned-profile','321','99','77',hashlib.sha256(raw).hexdigest(),
              '--native-cache-root',str(cache_base()/nonce),'--native-cache-token',nonce,'--native-cache-instance','c'*32]
        with patch.object(base,'_hosted'),patch.object(base,'HostedNativeReaderObservation',return_value=fake),patch.object(subject,'_original',return_value=raw),patch.object(subject,'_format_clipboard',return_value=clip),patch.object(native,'write_stdout',side_effect=lambda chunks:frames.append(b''.join(chunks))):
            self.assertEqual(subject.child_main(args),0)
        result=decode_frame(frames[0]);report=subject._checked_formats(json.loads(result['value'][1]['text']),77,hashlib.sha256(raw).hexdigest(),len(raw))
        self.assertTrue(report['formats'][1]['matches_original']);self.assertEqual(report['formats'][0]['extra_bytes'],1)
        self.assertFalse(clip.opened)
    def test_child_native_error_has_safe_phase_without_raw_exception_text(self):
        import win_clipboard_native as native
        from win_clipboard_cache import cache_base
        from win_clipboard_io import decode_frame
        raw=png();api=FakeAPI();clip=Clipboard({'PNG':b'bad'});frames=[]
        clip.read=lambda *args:(_ for _ in ()).throw(OSError(5,'PRIVATE: original image and path'))
        fake=type('Observer',(),{'_guard':lambda _:None,'_native':lambda _:api,'producer_profile':Path('owned-fixture/FlickDock')})();nonce='b'*32
        args=['--format-child','owned-root',nonce,'owned-profile','321','99','77',hashlib.sha256(raw).hexdigest(),
              '--native-cache-root',str(cache_base()/nonce),'--native-cache-token',nonce,'--native-cache-instance','c'*32]
        with patch.object(base,'_hosted'),patch.object(base,'HostedNativeReaderObservation',return_value=fake),patch.object(subject,'_original',return_value=raw),patch.object(subject,'_format_clipboard',return_value=clip),patch.object(native,'write_stdout',side_effect=lambda chunks:frames.append(b''.join(chunks))):
            self.assertEqual(subject.child_main(args),0)
        result=decode_frame(frames[0]);self.assertEqual(result['status'],'unknown');self.assertFalse(result['valid']);self.assertEqual(result['diagnostic']['errno'],5)
        self.assertNotIn(b'PRIVATE',frames[0]);self.assertFalse(clip.opened)
    def test_child_close_failure_exits_without_hash_or_pipe_write_under_lock(self):
        import win_clipboard_native as native
        from win_clipboard_cache import cache_base
        raw=png();api=FakeAPI();clip=Clipboard({'PNG':raw});frames=[]
        clip.close=lambda:(_ for _ in ()).throw(OSError(5,'PRIVATE close failure'))
        fake=type('Observer',(),{'_guard':lambda _:None,'_native':lambda _:api,'producer_profile':Path('owned-fixture/FlickDock')})();nonce='b'*32
        args=['--format-child','owned-root',nonce,'owned-profile','321','99','77',hashlib.sha256(raw).hexdigest(),
              '--native-cache-root',str(cache_base()/nonce),'--native-cache-token',nonce,'--native-cache-instance','c'*32]
        with patch.object(base,'_hosted'),patch.object(base,'HostedNativeReaderObservation',return_value=fake),patch.object(subject,'_original',return_value=raw),patch.object(subject,'_format_clipboard',return_value=clip),patch.object(native,'write_stdout',side_effect=AssertionError('write under uncertain lock')):
            self.assertEqual(subject.child_main(args),3)

class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-image-observer-');self.root=Path(self.temp.name);self.repo=self.root/'repo'
        (self.repo/'qa/packaged-live').mkdir(parents=True);(self.repo/'qa/packaged-live/native_editor.py').write_text('# synthetic')
        self.exe=self.repo/'dist/FlickDock/FlickDock.exe';self.exe.parent.mkdir(parents=True);self.exe.write_bytes(b'never-executed-synthetic-exe');self.sha=hashlib.sha256(self.exe.read_bytes()).hexdigest()
        self.profile=self.root/(base.NORMAL_PREFIX+'owned')/'FlickDock';self.profile.mkdir(parents=True);(self.profile.parent/'files').mkdir()
        self.original=self.profile.parent/'files/original-image.png';self.raw=png();self.original.write_bytes(self.raw);self.expected=hashlib.sha256(self.raw).hexdigest()
        self.patches=[patch.dict(os.environ,dict(GITHUB_ACTIONS='true',RUNNER_OS='Windows',RUNNER_ENVIRONMENT='github-hosted',GITHUB_REPOSITORY=base.EXPECTED_REPOSITORY,RUNNER_TEMP=str(self.root)),clear=True),patch.object(base.sys,'platform','win32'),patch.object(base,'REPO',self.repo),patch.object(base.tempfile,'gettempdir',return_value=str(self.root)),patch('subprocess.Popen',FakeProducer)]
        for item in self.patches:item.start()
        self.producer=FakeProducer([sys.executable,str(self.repo/'qa/packaged-live/native_editor.py'),str(self.profile.parent)])
        self.api=FakeAPI();self.commands=[]
        self.frozen=FakeOperation(result=dict(status='ready',valid=True,known_empty=False,code='',sequence=77,sequence_before=77,sequence_after=77,value=('image',dict(raw=self.raw+b'\0',mime='image/png')),marker=b'private'))
        facts=subject._format_capture(self.api,Clipboard({'PNG':self.raw+b'\0','image/png':self.raw}),(99,321),77,self.raw)
        self.formats=FakeOperation(result=dict(status='ready',valid=True,known_empty=False,code='',sequence=77,sequence_before=77,sequence_after=77,value=('text',dict(text=json.dumps(facts),html='')),marker=b''))
        def operation(command):self.commands.append(command);return self.frozen if len(self.commands)==1 else self.formats
        self.observer=base.HostedNativeReaderObservation(self.root/(base.OBSERVER_PREFIX+'a'*32),'a'*32,producer_profile=self.profile,_api_factory=lambda:self.api,_operation_factory=operation)
        self.observer.before_app_start();self.api.events.clear()
    def tearDown(self):
        self.observer.close()
        for item in reversed(self.patches):item.stop()
        self.temp.cleanup()
    def observe(self,**options):return subject.after_native_image_failure(self.observer,self.exe,self.sha,self.producer,self.original,self.expected,**options)
    def test_exact_frozen_and_format_sequence_hash_only_no_acceptance(self):
        row=self.observe();self.assertEqual(self.commands[0],[str(self.exe),'--clipboard-reader']);self.assertEqual(self.commands[1][2],'--format-child')
        self.assertFalse(row['normal_acceptance']);self.assertTrue(row['original_failure_preserved']);self.assertFalse(row['frozen_content']['matches_original'])
        self.assertTrue(row['native_formats']['formats'][1]['matches_original']);self.assertTrue(self.observer.close()['helper_reaped'])
        encoded=json.dumps(row)
        for private in (str(self.exe),str(self.original),str(self.profile),'private','never-executed',self.raw.hex()):self.assertNotIn(private,encoded)
    def test_wrong_owner_zero_sequence_or_dead_producer_never_launches(self):
        self.api.owned=(99,999);row=self.observe();self.assertFalse(row['owner_verified']);self.assertFalse(self.commands)
    def test_zero_sequence_never_launches(self):
        self.api.seq=0;row=self.observe();self.assertEqual(row['unavailable'],'CLIPBOARD_OWNER_OR_SEQUENCE_UNVERIFIED');self.assertFalse(self.commands)
    def test_live_producer_args_and_exe_sha_guard_before_owner(self):
        self.producer.args[-1]=str(self.root)
        with self.assertRaisesRegex(RuntimeError,'PRODUCER_ARGS_REQUIRED'):self.observe()
        self.assertNotIn('owner',self.api.events);self.assertFalse(self.commands)
    def test_wrong_host_never_constructs_native_api(self):
        with patch.dict(os.environ,{'RUNNER_ENVIRONMENT':'self-hosted'}):
            with self.assertRaisesRegex(RuntimeError,'HOSTED_RUNNER_REQUIRED'):self.observe()
        self.assertNotIn('owner',self.api.events);self.assertFalse(self.commands)
    def test_wrong_hash_or_original_or_failure_scope_before_native(self):
        self.expected='0'*64
        with self.assertRaisesRegex(RuntimeError,'ORIGINAL_MISMATCH'):self.observe()
        self.assertNotIn('owner',self.api.events);self.assertFalse(self.commands)
    def test_wrong_failure_label_cannot_be_called_as_success_probe(self):
        with self.assertRaisesRegex(RuntimeError,'FAILURE_SCOPE_REQUIRED'):self.observe(failure_label='02-native-text-copy')
        self.assertFalse(self.commands)
    def test_original_must_be_exact_owned_fixture(self):
        wrong=self.root/'other.png';wrong.write_bytes(self.raw)
        with self.assertRaisesRegex(RuntimeError,'ORIGINAL_REQUIRED'):subject.after_native_image_failure(self.observer,self.exe,self.sha,self.producer,wrong,self.expected)
        self.assertFalse(self.commands)
    def test_unreaped_frozen_operation_stays_owned_without_second_child(self):
        self.frozen.reaped=False;row=self.observe();self.assertEqual(row['unavailable'],'HELPER_UNREAPED');self.assertEqual(len(self.commands),1);self.assertIs(self.observer._owned_operation,self.frozen);self.assertTrue(self.observer.close()['retained_ownership'])
    def test_changed_owner_after_frozen_does_not_start_format_child(self):
        old=self.frozen.start
        def changed():old();self.api.owned=(100,999)
        self.frozen.start=changed;row=self.observe();self.assertEqual(row['unavailable'],'CLIPBOARD_OWNER_CHANGED');self.assertNotIn('frozen_content',row);self.assertEqual(len(self.commands),1)
    def test_malformed_format_payload_is_not_serialized(self):
        self.formats.result['value']=('text',dict(text='{"secret":"private payload"}',html=''))
        row=self.observe();self.assertEqual(row['unavailable'],'INVALID_FORMAT_REPORT');self.assertNotIn('private payload',json.dumps(row))
    def test_contradictory_original_match_flag_cannot_appear_as_fact(self):
        report=json.loads(self.formats.result['value'][1]['text']);report['formats'][0]['matches_original']=True
        self.formats.result['value']=('text',dict(text=json.dumps(report),html=''))
        row=self.observe();self.assertEqual(row['unavailable'],'INVALID_FORMAT_REPORT');self.assertNotIn('native_formats',row)
    def test_changed_owner_after_format_probe_suppresses_all_native_hashes(self):
        old=self.formats.start
        def changed():old();self.api.owned=(101,999)
        self.formats.start=changed;row=self.observe();self.assertEqual(row['unavailable'],'IMAGE_OBSERVER_SCOPE_CHANGED');self.assertNotIn('frozen_content',row);self.assertNotIn('native_formats',row)
    def test_unfinished_format_probe_stays_owned_and_bounded(self):
        waits=[]
        class NeverDone:
            def wait(self,seconds):waits.append(seconds);return False
            def is_set(self):return False
        self.formats=FakeOperation(ready=False,reaped=False);self.formats.done=NeverDone()
        row=self.observe();self.assertEqual(row['unavailable'],'FORMAT_HELPER_UNREAPED');self.assertIs(self.observer._owned_operation,self.formats)
        self.assertEqual(waits,[base.OBSERVATION_WAIT_SECONDS,base.CLOSE_WAIT_SECONDS]);self.assertTrue(self.observer.close()['retained_ownership'])
    def test_marker_tamper_rejected_before_native_and_operation(self):
        (self.observer.root/'observer-owner.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError,'WRONG_OWNER'):self.observe()
        self.assertFalse(self.commands)
        (self.observer.root/'observer-owner.json').write_text(json.dumps({'nonce':'a'*32,'fixture':'native-reader-observer-v1'}))

if __name__=='__main__':unittest.main(verbosity=2)
