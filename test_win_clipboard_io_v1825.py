"""Fake native provider + real owned child pipes, never the system clipboard."""
import os,sys,struct,tempfile,time,ctypes,json,unittest,subprocess
os.environ['QT_QPA_PLATFORM']='offscreen'
from pathlib import Path
from PySide6.QtCore import QTimer,QProcess
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage
from win_clipboard_native import capture,native_metadata,encode_frame,dib_image,write_stdout,MAX_BYTES,SYNC_MARKER
from win_clipboard_io import decode_frame,WindowsClipboardReader
APP=QApplication.instance() or QApplication([]);APP.setQuitOnLastWindowClosed(False)
def until(check,seconds=4):
    end=time.monotonic()+seconds
    while not check() and time.monotonic()<end:APP.processEvents();time.sleep(.002)
    if not check():raise AssertionError('Bounded clipboard helper did not finish')
class Fake:
    def __init__(self,formats=None,*,busy=False,changed=False):
        self.formats=formats or {};self.busy=busy;self.changed=changed;self.closed=0;self.reads=[];self.seq_calls=0;self.path=None;self.is_open=False
    def open(self):self.is_open=not self.busy;return self.is_open
    def close(self):self.closed+=1;self.is_open=False
    def sequence(self):self.seq_calls+=1;return 11+(self.changed and self.seq_calls>1)
    def count(self):return len(self.formats)
    def available(self,key):return key in self.formats
    def read(self,key,limit):
        from win_clipboard_native import TooLarge
        self.reads.append(key);data=self.formats[key]
        if isinstance(data,Exception):raise data
        if len(data)>limit:raise TooLarge()
        return data
    def file(self):return self.path
def frame(result):return b''.join(encode_frame(result))
def text_result(text='latest'):
    return dict(status='ready',valid=True,value=('text',dict(text=text,html='')),marker=b'',known_empty=False,sequence_before=11,sequence_after=11,code='')

class NativeProviderTests(unittest.TestCase):
    def test_failure_diagnostics_preserve_only_phase_class_and_numeric_os_codes(self):
        error=PermissionError(13,'private content','private path');error.winerror=5
        result=decode_frame(frame(capture(Fake({13:error}))))
        self.assertEqual(result['diagnostic'],dict(phase='copy',exception='PermissionError',errno=13,winerror=5))
        self.assertNotIn('private',repr(result));self.assertFalse(result['valid'])
        bad=capture(Fake({13:error}));bad['diagnostic']['path']='private path'
        with self.assertRaises(ValueError):decode_frame(frame(bad))
    def test_busy_metadata_and_capture_never_retry_or_read_data(self):
        for fn in (native_metadata,capture):
            p=Fake({13:b'private'},busy=True);result=fn(p);self.assertEqual(result['status'],'unknown');self.assertEqual(p.reads,[]);self.assertEqual(p.closed,0)
        p=Fake({13:b'private'});result=native_metadata(p);self.assertTrue(result['valid']);self.assertFalse(result['known_empty']);self.assertEqual(p.reads,[]);self.assertEqual(p.closed,1)
    def test_known_empty_is_distinct_from_unknown_or_sequence_change(self):
        result=decode_frame(frame(capture(Fake())));self.assertTrue(result['valid']);self.assertTrue(result['known_empty']);self.assertIsNone(result['value'])
        changed=capture(Fake({13:'latest\0'.encode('utf-16-le')},changed=True));self.assertEqual(changed['status'],'changed');self.assertFalse(changed['valid']);self.assertIsNone(changed['value'])
        for fn in (native_metadata,capture):
            p=Fake();p.sequence=lambda:0;r=fn(p);self.assertFalse(r['valid']);self.assertFalse(r['known_empty']);self.assertEqual(r['status'],'unknown');self.assertFalse(p.is_open)
    def test_unicode_html_marker_remain_original_and_always_close(self):
        html=b'<html><body><!--StartFragment-->original<!--EndFragment--></body></html>'
        template=b'Version:0.9\r\nStartHTML:%010d\r\nEndHTML:%010d\r\n';header=template%(len(template%(0,0)),len(template%(0,0))+len(html))
        p=Fake({13:'原始\r\nUnicode\0'.encode('utf-16-le'),'HTML Format':header+html,SYNC_MARKER:b'12345678-1234-4123-8123-123456789abc\0'})
        result=decode_frame(frame(capture(p)));self.assertEqual(result['value'],('text',dict(text='原始\r\nUnicode',html=html.decode())));self.assertEqual(result['marker'],b'12345678-1234-4123-8123-123456789abc');self.assertEqual(p.closed,1)
    def test_read_error_and_unsupported_never_become_empty_success(self):
        for formats in ({13:OSError('Synthetic')},{999:b'unsupported'},{13:b'odd'}):
            p=Fake(formats);r=capture(p);self.assertEqual(r['status'],'unknown');self.assertFalse(r['known_empty']);self.assertEqual(p.closed,1)
    def test_original_png_bytes_and_dib_pixels_not_recompressed(self):
        png=b'\x89PNG\r\n\x1a\noriginal-container-bytes';r=decode_frame(frame(capture(Fake({'PNG':png}))));self.assertEqual(r['value'],('image',dict(raw=png,mime='image/png')))
        header=struct.pack('<IiiHHIIiiII',40,2,2,1,24,0,16,0,0,0,0);pixels=b'\x00\x00\xff'*2+b'\x00\x00'+b'\xff\x00\x00'*2+b'\x00\x00';dib=header+pixels
        raw,mime=dib_image(dib+b'unrelated allocation tail');self.assertEqual(mime,'image/bmp');self.assertEqual(raw[14:],dib)
        image=QImage.fromData(raw,'BMP');self.assertFalse(image.isNull());self.assertEqual((image.width(),image.height()),(2,2))
        self.assertEqual(decode_frame(frame(capture(Fake({8:dib}))))['value'][1]['raw'],raw)
    def test_single_file_stats_and_same_path_mutation_are_captured_in_helper(self):
        with tempfile.TemporaryDirectory(prefix='flickdock-native-file-fixture-') as temp:
            path=Path(temp)/'original.bin';path.write_bytes(b'first');p=Fake({15:b''});p.path=str(path)
            one=decode_frame(frame(capture(p)));self.assertEqual(one['value'],('files',[str(path)]));self.assertEqual(one['signature'][1][0][1],5);self.assertEqual(one['source_stat'][2],5)
            path.write_bytes(b'different original');two=decode_frame(frame(capture(p)));self.assertNotEqual(one['signature'],two['signature']);self.assertEqual(path.read_bytes(),b'different original')
    def test_scaled_and_actual_100mib_policy_is_inclusive_and_never_mutates_provider(self):
        from unittest.mock import patch
        p=Fake({'PNG':b'a'*32});self.assertEqual(capture(p,32)['status'],'ready');self.assertEqual(capture(Fake({'PNG':b'a'*33}),32)['status'],'overlimit')
        # Opaque image-kind bytes prove framing/size, not a decoded100MiB PNG.
        raw=b'z'*MAX_BYTES;result=decode_frame(frame(capture(Fake({'PNG':raw}))));self.assertEqual(result['value'][1]['raw'],raw)
        self.assertEqual(capture(Fake({'PNG':raw+b'x'}))['status'],'overlimit')
    def test_malformed_wire_and_mismatched_sequence_do_not_publish_a_value(self):
        raw=frame(text_result());
        for invalid in (raw[:-1],raw+b'x',b'bad',raw[:6]+struct.pack('<I',65536)+raw[10:]):
            with self.assertRaises((ValueError,UnicodeError)):decode_frame(invalid)
        r=text_result();r.update(valid=False,status='changed',sequence_after=12,value=None);self.assertFalse(decode_frame(frame(r))['valid'])
    def test_windowed_stdout_uses_typed_handle_and_handles_partial_writes(self):
        received=bytearray()
        class Function:
            def __init__(self,fn):self.fn=fn
            def __call__(self,*args):return self.fn(*args)
        def write(handle,buffer,count,sent,overlapped):
            n=min(count,7);received.extend(ctypes.string_at(buffer,n));ctypes.cast(sent,ctypes.POINTER(ctypes.c_ulong)).contents.value=n;return True
        fake=type('Kernel',(),{})();fake.GetStdHandle=Function(lambda kind:123);fake.WriteFile=Function(write)
        data=frame(text_result('windowed'));write_stdout([data],fake);self.assertEqual(bytes(received),data);self.assertEqual(len(fake.WriteFile.argtypes),5)
    def test_slow_file_stat_occurs_after_clipboard_lock_is_released(self):
        import threading
        from types import SimpleNamespace
        from unittest.mock import patch
        p=Fake({15:b''});p.path='C:/synthetic/original.bin';entered=threading.Event();release=threading.Event();seen=[];results=[]
        class File:
            def is_absolute(self):return True
            def is_symlink(self):self.assert_closed();return False
            def is_junction(self):self.assert_closed();return False
            def assert_closed(self):seen.append(p.is_open)
            def stat(self):self.assert_closed();entered.set();release.wait(2);return SimpleNamespace(st_mode=0o100600,st_size=12,st_dev=1,st_ino=2,st_mtime_ns=3)
        with patch('win_clipboard_native.Path',side_effect=lambda _:File()):
            worker=threading.Thread(target=lambda:results.append(capture(p)));worker.start();self.assertTrue(entered.wait(1));self.assertFalse(p.is_open);release.set();worker.join(2)
        self.assertFalse(worker.is_alive());self.assertEqual(seen,[False,False,False]);self.assertEqual(results[0]['status'],'ready');self.assertEqual(p.closed,1)
    def test_text_and_dib_conversion_are_outside_lock(self):
        from unittest.mock import patch
        import win_clipboard_native as native
        for formats,fn in (({13:'original\0'.encode('utf-16-le')},'unicode_text'),({8:struct.pack('<IiiHHIIiiII',40,1,1,1,24,0,4,0,0,0,0)+b'\0'*4},'dib_image')):
            p=Fake(formats);original=getattr(native,fn);seen=[]
            def checked(*a,**k):seen.append(p.is_open);return original(*a,**k)
            with patch.object(native,fn,side_effect=checked):self.assertEqual(capture(p)['status'],'ready')
            self.assertEqual(seen,[False]);self.assertEqual(p.closed,1)

_OWNED_PROCESS_TEMP=None

def ensure_owned_process_temp():
    """Keep even late cleanup wakes inside this QA process's private TEMP.

    A daemon's idle flag is not proof that a second wake is absent. Never
    restore a case's fallback to the user's cache root after just one wait.
    The strong owner and cached gettempdir remain until process shutdown.
    """
    global _OWNED_PROCESS_TEMP
    if _OWNED_PROCESS_TEMP is None:
        _OWNED_PROCESS_TEMP=tempfile.TemporaryDirectory(prefix='flickdock-qa-process-')
        root=Path(_OWNED_PROCESS_TEMP.name).resolve(strict=True)
        for name in ('TEMP','TMP','TMPDIR'):os.environ[name]=str(root)
        tempfile.tempdir=str(root)
    return Path(_OWNED_PROCESS_TEMP.name).resolve(strict=True)

class ProcessLifecycleTests(unittest.TestCase):
    def setUp(self):
        ensure_owned_process_temp()
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-owned-reader-fixture-');self.root=Path(self.temp.name);self.readers=[]
        self.data=self.root/'frame.bin';self.data.write_bytes(frame(text_result()))
        self.child=self.root/'child.py';self.child.write_text('import sys,time,os\nfrom pathlib import Path\nmode=sys.argv[1]\nif mode=="hang":time.sleep(30)\nelif mode=="closehang":os.close(1);time.sleep(30)\nelif mode=="bad":sys.stdout.buffer.write(b"invalid")\nelif mode=="oversize":sys.stdout.buffer.write(b"x"*2048)\nelif mode=="fail":sys.exit(9)\nelse:sys.stdout.buffer.write(Path(sys.argv[2]).read_bytes());sys.stdout.buffer.flush()\n',encoding='utf8')
    def tearDown(self):
        for reader in self.readers:reader.close()
        until(lambda:all(r._active is None for r in self.readers))
        import gc,win_clipboard_cache as cache
        gc.collect();cache.schedule_cleanup();self.assertTrue(cache._IDLE.wait(2));self.temp.cleanup()
    def reader(self,mode='ok',deadline=200):
        r=WindowsClipboardReader(command=[sys.executable,str(self.child),mode,str(self.data)],deadline_ms=deadline,process_factory=lambda *a,**k:subprocess.Popen(*a,**k));self.readers.append(r);return r
    def test_real_child_pipe_decodes_off_ui_and_callbacks_once(self):
        r=self.reader(deadline=2000);seen=[];r.read(lambda value,error:seen.append((value,error)));until(lambda:bool(seen));self.assertEqual(len(seen),1);self.assertEqual(seen[0][0]['value'],('text',dict(text='latest',html='')));self.assertIsNone(seen[0][1]);self.assertIsNone(r._active)
    def test_hung_provider_is_killed_at_deadline_while_qt_remains_responsive(self):
        r=self.reader('hang',150);seen=[];ticks=[];timer=QTimer();timer.setInterval(5);timer.timeout.connect(lambda:ticks.append(time.monotonic()));timer.start();r.read(lambda value,error:seen.append(value));until(lambda:bool(seen));timer.stop()
        self.assertEqual(seen[0]['status'],'unknown');self.assertEqual(seen[0]['code'],'DEADLINE');self.assertGreaterEqual(len(ticks),3);self.assertIsNone(r._active)
    def test_latest_pending_and_replace_do_not_spawn_parallel_processes(self):
        r=self.reader('hang',2000);seen=[];r.read(lambda v,e:seen.append(('first',v['code'])))
        r.command=[sys.executable,str(self.child),'ok',str(self.data)];r.read(lambda v,e:seen.append(('middle',v['code'])));r.read(lambda v,e:seen.append(('last',v['status'])),replace=True)
        until(lambda:len(seen)==3);self.assertIn(('middle','SUPERSEDED'),seen);self.assertIn(('first','SUPERSEDED'),seen);self.assertIn(('last','ready'),seen);self.assertIsNone(r._active)
    def test_cancel_close_and_bad_frame_do_not_emit_stale_success(self):
        r=self.reader('hang',2000);seen=[];token=r.read(lambda v,e:seen.append(v));r.cancel(token);until(lambda:r._active is None);self.assertEqual(seen,[])
        r.command=[sys.executable,str(self.child),'bad',str(self.data)];r.read(lambda v,e:seen.append(v));until(lambda:bool(seen));self.assertEqual(seen[0]['code'],'INVALID_FRAME')
        r.command=[sys.executable,str(self.child),'hang',str(self.data)];r.read(lambda v,e:seen.append(v));r.close();until(lambda:r._active is None);self.assertEqual(len(seen),1)
    def test_failed_start_is_unknown_and_recovers_next_owned_process(self):
        r=self.reader();r.command=[str(self.root/'missing.exe')];seen=[];r.read(lambda v,e:seen.append(v));until(lambda:bool(seen));self.assertEqual(seen[0]['code'],'START_FAILED')
        r.command=[sys.executable,str(self.child),'ok',str(self.data)];r.read(lambda v,e:seen.append(v));until(lambda:len(seen)==2);self.assertEqual(seen[1]['status'],'ready')
    def test_reentrant_completion_can_replace_pending_and_close_without_stale_callback(self):
        r=self.reader(deadline=2000);seen=[]
        def first(value,error):
            seen.append(('first',value['status']));r.read(lambda v,e:seen.append(('newest',v['status'])))
        r.read(first);r.read(lambda v,e:seen.append(('obsolete',v['code'])))
        until(lambda:len(seen)==3);self.assertEqual(seen[0],('first','ready'));self.assertIn(('obsolete','SUPERSEDED'),seen);self.assertIn(('newest','ready'),seen);self.assertIsNone(r._active);self.assertFalse(r._clock.isActive())
        r.read(lambda v,e:r.close());until(lambda:r._closed and r._active is None);self.assertFalse(r._clock.isActive())
    def test_cancel_during_spawn_kills_child_before_read_and_reaps_it(self):
        import threading,subprocess
        from unittest.mock import patch
        entered=threading.Event();release=threading.Event();created=[];original=subprocess.Popen
        def spawn(*args,**kwargs):
            entered.set();self.assertTrue(release.wait(2));p=original(*args,**kwargs);created.append(p);return p
        r=self.reader('hang',2000);seen=[]
        with patch('win_clipboard_io.subprocess.Popen',side_effect=spawn):
            token=r.read(lambda v,e:seen.append(v));until(entered.is_set);r.cancel(token);release.set();until(lambda:r._active is None)
        self.assertEqual(seen,[]);self.assertEqual(len(created),1);self.assertIsNotNone(created[0].returncode);self.assertTrue(created[0].stdout.closed)
    def test_pipe_eof_does_not_claim_complete_while_child_still_running(self):
        r=self.reader('closehang',150);seen=[];r.read(lambda v,e:seen.append(v));until(lambda:bool(seen));self.assertEqual(seen[0]['code'],'DEADLINE');self.assertIsNone(r._active);self.assertFalse(r._clock.isActive())
    def test_output_limit_and_nonzero_exit_are_unknown_and_reap_before_reuse(self):
        from unittest.mock import patch
        r=self.reader('oversize',2000);seen=[]
        with patch('win_clipboard_io.MAX_WIRE',1024):r.read(lambda v,e:seen.append(v));until(lambda:bool(seen))
        self.assertEqual(seen[0]['code'],'OUTPUT_LIMIT');self.assertFalse(seen[0]['valid'])
        r.command=[sys.executable,str(self.child),'fail',str(self.data)];r.read(lambda v,e:seen.append(v));until(lambda:len(seen)==2);self.assertEqual(seen[1]['code'],'PROCESS_FAILED');self.assertFalse(r._clock.isActive())
    def test_deadline_reaps_child_even_when_gui_events_are_not_pumped(self):
        r=self.reader('hang',100);seen=[];r.read(lambda v,e:seen.append(v));operation=r._active
        self.assertTrue(operation.done.wait(2));self.assertTrue(operation.reaped);self.assertIsNotNone(operation.process.returncode);self.assertEqual(seen,[])
        until(lambda:bool(seen));self.assertEqual(seen[0]['code'],'DEADLINE');self.assertFalse(seen[0]['valid'])
    def test_reap_error_is_unknown_but_done_always_signals_and_handles_close(self):
        import io
        class Process:
            def __init__(self):self.stdout=io.BytesIO(frame(text_result()));self.returncode=None;self.waits=0
            def wait(self,timeout=None):
                self.waits+=1
                if self.waits==1:raise OSError('synthetic wait observation')
                self.returncode=0;return 0
            def poll(self):raise OSError('stale poll unavailable')
            def kill(self):raise ProcessLookupError('already exited')
        p=Process();r=WindowsClipboardReader(process_factory=lambda *a,**k:p);self.readers.append(r);seen=[];r.read(lambda v,e:seen.append(v));until(lambda:bool(seen))
        self.assertEqual(seen[0]['status'],'unknown');self.assertFalse(seen[0]['valid']);self.assertTrue(p.stdout.closed);self.assertIsNone(r._active)
    def test_kill_already_exited_error_still_completes_unknown_and_reaps(self):
        import io,threading
        class Pipe(io.BytesIO):
            def read(self,n=-1):gate.wait(2);return b''
        gate=threading.Event()
        class Process:
            def __init__(self):self.stdout=Pipe();self.returncode=None
            def wait(self,timeout=None):self.returncode=0;return 0
            def kill(self):self.returncode=0;gate.set();raise ProcessLookupError('already exited')
        p=Process();r=WindowsClipboardReader(deadline_ms=50,process_factory=lambda *a,**k:p);self.readers.append(r);seen=[];r.read(lambda v,e:seen.append(v));operation=r._active
        self.assertTrue(operation.done.wait(2));until(lambda:bool(seen));self.assertEqual(seen[0]['code'],'TERMINATE_FAILED');self.assertTrue(p.stdout.closed);self.assertTrue(operation.reaped)

class JobOwnershipTests(unittest.TestCase):
    def test_unconfigured_job_close_failure_is_retained_and_quarantines_without_child(self):
        from types import SimpleNamespace
        from win_owned_process import _Win32,ProtectedProcess
        from win_clipboard_io import _ReadOperation
        class Function:
            def __init__(self,fn):self.fn=fn
            def __call__(self,*a):return self.fn(*a)
        calls=[]
        def fail_close(handle):calls.append(handle);raise OSError(5,'private handle error')
        kernel=SimpleNamespace(**{n:Function(lambda *a:1) for n in ('CreateJobObjectW','SetInformationJobObject','ResumeThread','InitializeProcThreadAttributeList','UpdateProcThreadAttribute','DeleteProcThreadAttributeList','CreateProcessW')})
        kernel.CreateJobObjectW=Function(lambda *a:123);kernel.SetInformationJobObject=Function(lambda *a:False)
        api=_Win32(api=SimpleNamespace(CloseHandle=fail_close),crt=object(),kernel=kernel)
        operation=_ReadOperation('synthetic',None,['owned-helper'],1000,lambda *a,**k:ProtectedProcess(['owned-helper'],backend=api))
        operation.start();self.assertTrue(operation.done.wait(2))
        self.assertFalse(operation.reaped);self.assertEqual(operation.result['code'],'REAP_FAILED')
        self.assertEqual(operation.process._failed_handles,{'unconfigured_job':123});self.assertEqual(calls,[123]);self.assertNotIn('private',repr(operation.result))
    def test_actual_job_api_layout_sets_noninherited_kill_on_close(self):
        from types import SimpleNamespace
        from win_owned_process import _Win32
        calls=[];holder={}
        class Function:
            def __init__(self,fn):self.fn=fn
            def __call__(self,*a):return self.fn(*a)
        def limits(handle,kind,pointer,size):
            value=ctypes.cast(pointer,ctypes.POINTER(holder['api'].Extended)).contents
            calls.append((handle,kind,value.BasicLimitInformation.LimitFlags,size));return True
        kernel=SimpleNamespace(**{n:Function(lambda *a:1) for n in ('CreateJobObjectW','SetInformationJobObject','ResumeThread','InitializeProcThreadAttributeList','UpdateProcThreadAttribute','DeleteProcThreadAttributeList','CreateProcessW')})
        kernel.CreateJobObjectW=Function(lambda attributes,name:calls.append(('job_security',attributes,name)) or 123)
        kernel.SetInformationJobObject=Function(limits)
        api=_Win32(api=SimpleNamespace(CloseHandle=lambda h:None),crt=object(),kernel=kernel);holder['api']=api
        self.assertEqual(api.job(),123);self.assertEqual(calls[0],('job_security',None,None));self.assertEqual(calls[1][1:3],(9,0x2000));self.assertEqual(len(kernel.CreateProcessW.argtypes),10)
    def backend(self,*,fail_spawn=False,fail_resume=False):
        import io
        from win_owned_process import _Spawn
        class API:
            def __init__(self):self.events=[];self.alive=False;self.stdout=io.BytesIO();self.assigned=False
            def job(self):self.events.append(('job',0x2000));return 10
            def spawn(self,command,job):
                self.events.append(('atomic_spawn',job))
                if fail_spawn:raise OSError('Atomic job assignment failed')
                self.alive=True;self.assigned=True;return _Spawn(20,30,40,self.stdout)
            def resume(self,thread):
                self.events.append(('resume',thread))
                if fail_resume:raise OSError('resume failed')
            def wait(self,process,timeout=None):
                if self.alive:raise subprocess.TimeoutExpired('owned',timeout)
                self.events.append(('reaped',process));return 1
            def close(self,handle):
                self.events.append(('close',handle))
                if handle==10:self.alive=False
        return API()
    def test_atomic_job_assignment_precedes_any_resume_and_parent_handle_close_kills_only_owned_child(self):
        from win_owned_process import ProtectedProcess
        api=self.backend();p=ProtectedProcess(['owned-helper'],backend=api);self.assertEqual(api.events[:3],[('job',0x2000),('atomic_spawn',10),('resume',30)])
        # Simulate the OS closing the parent's noninherited job handle.
        p.kill();self.assertFalse(api.alive);p.wait(1);p.stdout.close();p.close();self.assertFalse(api.alive)
    def test_atomic_assignment_failure_never_resumes_or_creates_unprotected_child(self):
        from win_owned_process import ProtectedProcess
        api=self.backend(fail_spawn=True)
        with self.assertRaises(OSError):ProtectedProcess(['owned-helper'],backend=api)
        self.assertFalse(api.alive);self.assertFalse(any(e[0]=='resume' for e in api.events));self.assertIn(('close',10),api.events)
    def test_resume_failure_kills_and_reaps_protected_suspended_child_and_all_handles(self):
        from win_owned_process import ProtectedProcess
        api=self.backend(fail_resume=True)
        with self.assertRaises(OSError):ProtectedProcess(['owned-helper'],backend=api)
        self.assertFalse(api.alive);self.assertIn(('reaped',20),api.events);self.assertTrue(api.stdout.closed);self.assertIn(('close',20),api.events);self.assertIn(('close',30),api.events)
    def test_concurrent_cancel_deadline_and_close_claim_each_handle_once(self):
        import threading
        from win_owned_process import ProtectedProcess
        api=self.backend();base=api.close;entered=threading.Event();release=threading.Event();errors=[]
        def close(handle):
            if handle==10:entered.set();self.assertTrue(release.wait(2))
            base(handle)
        api.close=close;p=ProtectedProcess(['owned-helper'],backend=api)
        one=threading.Thread(target=p.kill);one.start();self.assertTrue(entered.wait(1))
        two=threading.Thread(target=p.kill);two.start();two.join(1);self.assertFalse(two.is_alive())
        with self.assertRaisesRegex(OSError,'pending'):p.close()
        release.set();one.join(1);p.stdout.close();p.close()
        for handle in (10,20,30):self.assertEqual(api.events.count(('close',handle)),1)
    def test_close_error_is_explicit_quarantined_and_never_recloses_ambiguous_handle(self):
        from win_owned_process import ProtectedProcess
        api=self.backend();base=api.close;calls=[]
        def close(handle):
            calls.append(handle)
            if handle==10:raise OSError('synthetic close failure')
            base(handle)
        api.close=close;p=ProtectedProcess(['owned-helper'],backend=api)
        with self.assertRaisesRegex(OSError,'close failed'):p.kill()
        with self.assertRaisesRegex(OSError,'close failed'):p.close()
        with self.assertRaisesRegex(OSError,'close failed'):p.kill()
        self.assertEqual(calls.count(10),1);self.assertEqual(p._failed_handles,{'_job':10})
        self.assertEqual(calls.count(20),1);self.assertEqual(calls.count(30),1);p.stdout.close()
    def test_startup_cleanup_failure_transfers_child_before_raising_and_keeps_failed_resource_owner(self):
        from win_owned_process import ProtectedProcess
        api=self.backend();spawn=api.spawn
        def failure(command,job):
            result=spawn(command,job);result.cleanup_error=OSError('private startup cleanup message');result.failed_handles={'parent_write':99};return result
        api.spawn=failure
        with self.assertRaises(OSError) as caught:ProtectedProcess(['owned-helper'],backend=api)
        owner=caught.exception._helper_owner
        self.assertFalse(api.alive);self.assertTrue(owner.stdout.closed)
        self.assertFalse(any(e[0]=='resume' for e in api.events))
        for handle in (10,20,30):self.assertEqual(api.events.count(('close',handle)),1)
        self.assertEqual(owner._failed_handles,{'parent_write':99})
        with self.assertRaises(OSError):owner.close()
        self.assertNotIn(('close',99),api.events)
    def test_startup_exception_owner_is_retained_and_lane_does_not_claim_reaped(self):
        from win_owned_process import ProtectedProcess
        from win_clipboard_io import _ReadOperation
        api=self.backend();spawn=api.spawn
        def failure(command,job):
            result=spawn(command,job);result.cleanup_error=OSError('private startup message');result.failed_handles={'parent_write':99};return result
        api.spawn=failure
        operation=_ReadOperation('synthetic',None,['owned-helper'],1000,lambda *a,**k:ProtectedProcess(['owned-helper'],backend=api))
        operation.start();self.assertTrue(operation.done.wait(2))
        self.assertFalse(operation.reaped);self.assertEqual(operation.result['status'],'unknown');self.assertEqual(operation.result['code'],'REAP_FAILED')
        self.assertEqual(operation.process._failed_handles,{'parent_write':99});self.assertNotIn('private',repr(operation.result))

if __name__=='__main__':unittest.main(verbosity=2)
