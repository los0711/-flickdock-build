"""Owned TEMP original-cache contract, no native clipboard or user source IO."""
import gc,hashlib,json,mmap,os,struct,subprocess,sys,tempfile,time,unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch
import win_clipboard_cache as cache

class OriginalCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-owned-cache-test-')
        self.base=Path(self.temp.name)/'private';self.patch=patch.object(cache,'cache_base',return_value=self.base);self.patch.start()
    def tearDown(self):
        gc.collect();cache.schedule_cleanup();self.assertTrue(cache._IDLE.wait(2))
        self.patch.stop();self.temp.cleanup()
    def original(self,raw=b'complete-original'):
        capability=cache.CacheCapability();sink=cache.CacheSink(capability.root,capability.token,capability.instance)
        sink.write('image',[raw[:3],raw[3:]],mime='image/png');descriptor=sink.descriptor('image',len(raw),hashlib.sha256(raw).hexdigest());sink.close()
        return capability,descriptor
    def test_original_is_verified_and_immutable_and_holder_retains_it(self):
        cap,descriptor=self.original();content=cap.adopt(descriptor);path=Path(content.parts[0]['path']);self.assertEqual(path.read_bytes(),b'complete-original')
        with self.assertRaises(FrozenInstanceError):content.logical_size=1
        with self.assertRaises(TypeError):content.parts[0]['path']='different'
        holder=content.acquire();live=holder.__enter__();other=content.acquire();live2=other.__enter__();self.assertIs(live,content)
        del content,cap;gc.collect();cache._sweep();self.assertTrue(path.exists())
        holder.__exit__(None,None,None);del live,holder;gc.collect();cache._sweep();self.assertTrue(path.exists())
        other.__exit__(None,None,None);del live2,other;gc.collect();end=time.monotonic()+2
        while path.exists() and time.monotonic()<end:time.sleep(.01)
        self.assertFalse(path.exists())
    def test_wrong_capability_and_corrupt_bytes_never_adopt(self):
        cap,descriptor=self.original();descriptor['cache_token']='0'*32
        with self.assertRaises(ValueError):cap.adopt(descriptor)
        descriptor['cache_token']=cap.token;(cap.root/'image.bin').write_bytes(b'X'*descriptor['local_parts'][0]['size'])
        with self.assertRaises(ValueError):cap.adopt(descriptor)
        self.assertIsNone(cap.lock)
    def test_no_arbitrary_root_or_names_and_no_user_source_deletion(self):
        cap=cache.CacheCapability()
        with self.assertRaises(ValueError):cache.CacheSink(Path(self.temp.name),cap.token,cap.instance)
        sink=cache.CacheSink(cap.root,cap.token,cap.instance)
        for name in ('../original','source.txt','image.bin'):
            with self.assertRaises(ValueError):sink.write(name,[b'no'])
        source=Path(self.temp.name)/'original-user-file';source.write_bytes(b'untouched')
        cache._sweep();self.assertEqual(source.read_bytes(),b'untouched')
    def test_abandoned_published_cache_is_cleaned_after_parent_capability_release(self):
        cap,descriptor=self.original();root=cap.root;cap.retire();del descriptor,cap;gc.collect();end=time.monotonic()+2
        while root.exists() and time.monotonic()<end:time.sleep(.01)
        self.assertFalse(root.exists())
    def test_above_cloud_limit_file_keeps_original_reference_and_stat(self):
        from test_win_clipboard_io_v1825 import Fake,frame
        from win_clipboard_native import MAX_BYTES,capture
        from win_clipboard_io import decode_frame
        source=Path(self.temp.name)/'original-large.bin'
        with source.open('wb') as output:output.truncate(MAX_BYTES+1)
        provider=Fake({15:b''});provider.path=str(source)
        result=decode_frame(frame(capture(provider)))
        self.assertTrue(result['valid']);self.assertFalse(result['sync_eligible']);self.assertEqual(result['value'],('files',[str(source)]))
        self.assertEqual(result['source_stat'][2],MAX_BYTES+1);self.assertEqual(source.stat().st_size,MAX_BYTES+1)
    def test_scaled_text_html_and_dib_cache_originals_only_after_native_close(self):
        from test_win_clipboard_io_v1825 import Fake,frame
        from win_clipboard_native import capture
        from win_clipboard_io import decode_frame
        html_body=b'<body>'+b'original'*10+b'</body>';template=b'StartHTML:%010d\r\nEndHTML:%010d\r\n';n=len(template%(0,0));html=template%(n,n+len(html_body))+html_body
        raw_text=('complete text '*20+'\0').encode('utf-16-le')
        dib=struct.pack('<IiiHHIIiiII',40,2,2,1,24,0,16,0,0,0,0)+b'\x00'*16
        for formats,kind,expected in (({13:raw_text,'HTML Format':html},'text',{'text':raw_text,'html':html}),({8:dib},'image',{'native_image':dib})):
            cap=cache.CacheCapability();sink=cache.CacheSink(cap.root,cap.token,cap.instance);provider=Fake(formats);provider.read_snapshot=lambda name:formats[name]
            original_write=sink.write;states=[]
            def write(*a,**k):states.append(provider.is_open);return original_write(*a,**k)
            sink.write=write
            result=capture(provider,32,cache=sink);sink.close()
            with patch('win_clipboard_io.MAX_BYTES',32):parsed=decode_frame(frame(result),cache=cap)
            content=parsed['local_content'];self.assertEqual(content.kind,kind);self.assertFalse(parsed['sync_eligible']);self.assertIsNone(parsed['value']);self.assertTrue(states);self.assertFalse(any(states))
            for part in content.parts:
                if part['name'] in expected:self.assertEqual(Path(part['path']).read_bytes(),expected[part['name']])
            del parsed,content,result,sink,cap;gc.collect()
    def test_actual_100mib_plus_one_original_bytes_are_cached_not_framed_as_payload(self):
        from test_win_clipboard_io_v1825 import Fake,frame
        from win_clipboard_native import MAX_BYTES,capture
        from win_clipboard_io import decode_frame
        # Opaque image-kind native bytes prove preservation and policy. This
        # does not claim to decode a valid 100MiB photograph.
        raw=mmap.mmap(-1,MAX_BYTES+1);chunk=b'Z'*262144
        for offset in range(0,len(raw),len(chunk)):raw[offset:min(len(raw),offset+len(chunk))]=chunk[:min(len(chunk),len(raw)-offset)]
        digest=hashlib.sha256(raw).hexdigest();provider=Fake({'PNG':b''});provider.read_snapshot=lambda name:raw
        cap=cache.CacheCapability();sink=cache.CacheSink(cap.root,cap.token,cap.instance)
        result=capture(provider,cache=sink);sink.close();wire=frame(result);self.assertLess(len(wire),16384)
        parsed=decode_frame(wire,cache=cap);content=parsed['local_content'];self.assertEqual(content.logical_size,MAX_BYTES+1)
        self.assertEqual(content.parts[0]['sha256'],digest);self.assertFalse(parsed['sync_eligible']);self.assertTrue(parsed['valid']);raw.close()
    def test_cancelled_parent_verification_cannot_adopt_or_drop_original_file(self):
        import threading
        cap,descriptor=self.original();cancel=threading.Event();cancel.set();cap.cancel=cancel
        with self.assertRaises(InterruptedError):cap.adopt(descriptor)
        self.assertIsNone(cap.lock);self.assertTrue((cap.root/'image.bin').exists())
    def test_cache_memory_or_disk_failure_is_unknown_resource_not_empty_or_policy(self):
        from test_win_clipboard_io_v1825 import Fake
        from win_clipboard_native import capture
        for error in (MemoryError('private bytes'),OSError(28,'private path')):
            provider=Fake({'PNG':b''});provider.read_snapshot=lambda name:b'z'*33
            sink=type('Sink',(),{})()
            def fail(*a,**k):self.assertFalse(provider.is_open);raise error
            sink.write=fail;result=capture(provider,32,cache=sink)
            self.assertEqual(result['status'],'unknown');self.assertEqual(result['code'],'RESOURCE_UNAVAILABLE');self.assertFalse(result['known_empty']);self.assertFalse(result['valid']);self.assertNotIn('private',repr(result))
    def test_confirmed_dead_writer_partial_initialization_can_retire_only_owned_root(self):
        cap=cache.CacheCapability();cap.root.mkdir(parents=True);(cap.root/'image.partial').write_bytes(b'partial original')
        cap.retire();root=cap.root;del cap;gc.collect();end=time.monotonic()+2
        while root.exists() and time.monotonic()<end:time.sleep(.01)
        self.assertFalse(root.exists())
    def test_separated_small_samples_do_not_create_or_restart_cleanup_actors(self):
        before=cache._CLEANER
        for unused in range(2):
            cap=cache.CacheCapability();cap.retire();del cap;gc.collect();time.sleep(.3)
        self.assertIs(cache._CLEANER,before)
        if before is not None:self.assertTrue(before.is_alive());self.assertTrue(cache._IDLE.is_set())
    def test_declared_fingerprint_and_logical_size_cannot_substitute_original_identity(self):
        cap,descriptor=self.original()
        for key,value in (('fingerprint','0'*64),('logical_size',999)):
            bad=dict(descriptor);bad[key]=value
            with self.assertRaises(ValueError):cap.adopt(bad)
            self.assertIsNone(cap.lock)
    def test_dib_derivative_cannot_be_replaced_by_different_hashed_image(self):
        from win_clipboard_native import dib_image
        raw=struct.pack('<IiiHHIIiiII',40,2,2,1,24,0,16,0,0,0,0)+b'\x00'*16
        image,mime=dib_image(raw);cap=cache.CacheCapability();sink=cache.CacheSink(cap.root,cap.token,cap.instance)
        unrelated=b'X'*len(image);part=sink.write('image',[unrelated],mime=mime);sink.write('native_image',[raw]);descriptor=sink.descriptor('image',len(image),part['sha256']);sink.close()
        with self.assertRaisesRegex(ValueError,'DIB derivative'):cap.adopt(descriptor)
    def test_synthetic_restart_sweep_keeps_locked_foreign_holder_and_cleans_released_crash_root(self):
        cap,descriptor=self.original();content=cap.adopt(descriptor);root=cap.root
        marker=json.loads((root/'owner.json').read_text());marker.update(instance='a'*32,created=time.time()-86401);(root/'owner.json').write_text(json.dumps(marker));root.with_suffix('.owner.json').write_text(json.dumps(marker))
        # Controlled lost-process namespace state, not a real process crash.
        with cache._GUARD:cache._LIVE.pop(cap.token,None)
        cache._sweep();self.assertTrue(root.exists());self.assertEqual(cache._LAST_CLEANUP_STATUS,{'phase':'cleanup_lease','status':'active'})
        cap.lock.close();cap.lock=None;cache._sweep();self.assertFalse(root.exists())
    def test_exact_helper_argument_parser_rejects_before_any_native_capture(self):
        import win_clipboard_native as native
        cap=cache.CacheCapability();arguments=cap.arguments();self.assertEqual(native.parse_cache_arguments(arguments)['token'],cap.token);self.assertIsNone(native.parse_cache_arguments([]))
        for invalid in (['extra'],arguments+['extra'],arguments[:-1],arguments+arguments[:2],['--native-cache-token',cap.token]*3,['--clipboard-reader','--clipboard-reader'],['--native-cache-root',str(self.base/'other'),'--native-cache-token',cap.token,'--native-cache-instance',cap.instance]):
            with self.assertRaises(ValueError):native.parse_cache_arguments(invalid)
            with patch.object(native,'capture') as capture,patch.object(native,'write_stdout') as output:
                self.assertEqual(native.main(invalid),2);capture.assert_not_called();output.assert_not_called()
    def test_durable_sidecar_reclaims_crash_between_directory_and_inner_marker(self):
        cap=cache.CacheCapability();sink=cache.CacheSink(cap.root,cap.token,cap.instance);original=Path.write_text
        def fail(path,*a,**k):
            if path.name=='owner.json':raise OSError(28,'private temporary cache')
            return original(path,*a,**k)
        with patch.object(Path,'write_text',fail):
            with self.assertRaises(OSError):sink.write('image',[b'original'],mime='image/png')
        sink.close();root=cap.root;sidecar=root.with_suffix('.owner.json')
        self.assertTrue(sidecar.exists());self.assertFalse((root/'owner.json').exists())
        marker=json.loads(sidecar.read_text());marker.update(instance='b'*32,created=time.time()-86401);sidecar.write_text(json.dumps(marker))
        with cache._GUARD:cache._LIVE.pop(cap.token,None)
        cache._sweep();self.assertFalse(root.exists());self.assertFalse(sidecar.exists())
    def test_helper_cleanup_failure_exits_two_without_unhandled_exception_or_success_acceptance(self):
        import win_clipboard_native as native
        cap=cache.CacheCapability();sink=type('FailClose',(),{})()
        def close():raise OSError(5,'private temporary path')
        sink.close=close
        with patch.object(cache,'CacheSink',return_value=sink),patch.object(native,'capture',return_value=native._base('ready',7,7,empty=True)),patch.object(native,'write_stdout') as output:
            self.assertEqual(native.main(cap.arguments()),2);output.assert_called_once()
    def test_conflicting_owner_markers_are_not_repaired_or_adopted(self):
        cap,descriptor=self.original();marker=cap.root/'owner.json';data=json.loads(marker.read_text());data['instance']='c'*32;marker.write_text(json.dumps(data))
        with self.assertRaises(cache.CacheIdentityError):cap.retire()
        with self.assertRaises(cache.CacheIdentityError):cap.adopt(descriptor)
        self.assertEqual(json.loads(marker.read_text())['instance'],'c'*32);self.assertTrue((cap.root/'image.bin').exists())

class OwnedChildCacheTests(unittest.TestCase):
    def test_retire_io_failure_rejects_only_this_sample_and_next_copy_still_spawns(self):
        import io
        from test_win_clipboard_io_v1825 import until,text_result,frame
        from win_clipboard_io import WindowsClipboardReader
        calls=[]
        class Process:
            def __init__(self):self.stdout=io.BytesIO(frame(text_result('next actual sample')));self.returncode=0
            def wait(self,timeout=None):return 0
            def kill(self):pass
            def close(self):pass
        class Capability:
            def arguments(self):return []
            def retire(self):
                if len(calls)==1:raise OSError(28,'private temporary path')
        def factory(*a,**k):calls.append(True);return Process()
        with patch('win_clipboard_io.CacheCapability',side_effect=lambda **k:Capability()):
            reader=WindowsClipboardReader(process_factory=factory);results=[]
            try:
                reader.read(lambda value,error:results.append(value));until(lambda:len(results)==1)
                self.assertEqual(results[0]['code'],'CACHE_UNAVAILABLE');self.assertFalse(results[0]['valid']);self.assertIsNone(reader._quarantined)
                reader.read(lambda value,error:results.append(value));until(lambda:len(results)==2)
                self.assertEqual(len(calls),2);self.assertTrue(results[1]['valid']);self.assertIsNone(reader._quarantined)
                self.assertEqual(results[0]['diagnostic']['errno'],28);self.assertNotIn('private',repr(results[0]))
            finally:reader.close()
    def test_real_owned_child_preserves_100mib_plus_one_with_bounded_frame_and_ui_callbacks(self):
        from PySide6.QtCore import QTimer
        from test_win_clipboard_io_v1825 import APP,until
        from win_clipboard_io import WindowsClipboardReader
        from win_clipboard_native import MAX_BYTES
        code=r'''
import sys
from win_clipboard_cache import CacheSink
from win_clipboard_native import capture,encode_frame,MAX_BYTES
def arg(name):return sys.argv[sys.argv.index(name)+1]
sink=CacheSink(arg('--native-cache-root'),arg('--native-cache-token'),arg('--native-cache-instance'))
raw=b'R'*(MAX_BYTES+1)
class Provider:
    def open(self):return True
    def close(self):pass
    def sequence(self):return 314
    def count(self):return 1
    def available(self,name):return name=='PNG'
    def read_snapshot(self,name):return raw
    def read(self,*args):raise AssertionError('unexpected provider path')
try:
    result=capture(Provider(),cache=sink)
    for chunk in encode_frame(result):sys.stdout.buffer.write(chunk)
    sys.stdout.buffer.flush()
finally:sink.close()
'''
        expected=hashlib.sha256()
        for unused in range(MAX_BYTES//262144):expected.update(b'R'*262144)
        expected.update(b'R')
        beats=[];timer=QTimer();timer.setInterval(5);timer.timeout.connect(lambda:beats.append(True));timer.start()
        reader=WindowsClipboardReader(deadline_ms=3000,command=[sys.executable,'-c',code],process_factory=subprocess.Popen);results=[]
        try:
            reader.read(lambda value,error:results.append(value));until(lambda:bool(results),6)
            result=results[0];self.assertTrue(result['valid']);self.assertFalse(result['sync_eligible']);self.assertEqual(result['sequence'],314)
            self.assertEqual(result['local_content'].logical_size,MAX_BYTES+1);self.assertEqual(result['local_content'].parts[0]['sha256'],expected.hexdigest())
            self.assertIsNone(result['value']);self.assertGreater(len(beats),5);self.assertIsNone(reader._active)
        finally:timer.stop();reader.close()

if __name__=='__main__':unittest.main(verbosity=2)
