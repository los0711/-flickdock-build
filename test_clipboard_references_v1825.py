"""Explicit local references never use file stat, body cache or cloud baseline."""
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from clipboard_references import ReferenceReadPurpose,decode_frame,encode_frame,receipt
from win_clipboard_references import capture,parse_arguments,parse_hdrop,qt_local_references
from test_win_clipboard_io_v1825 import APP,until,ensure_owned_process_temp
from win_clipboard_io import WindowsClipboardReader

REQUEST='a'*32
SEQUENCE=37
PATHS=('D:\\owned large file.bin','D:\\owned directory','\\\\owned-host\\share\\中文.txt')


def hdrop(paths=PATHS,*,wide=True):
    text='\0'.join(paths)+'\0\0'
    return struct.pack('<IiiII',20,0,0,0,int(wide))+text.encode('utf-16-le' if wide else 'cp1252')


class Provider:
    def __init__(self,raw=None,*,advertised=True):
        self.raw=hdrop() if raw is None else raw;self.advertised=advertised
        self.counter=SEQUENCE;self.opened=False;self.closed=False;self.reads=0
    def sequence(self):return self.counter
    def open(self):self.opened=True;return True
    def close(self):self.closed=True;self.opened=False
    def available(self,format):
        if not self.opened or format!=15:raise AssertionError('Wrong native capture boundary')
        return self.advertised
    def read(self,format,limit):
        if not self.opened:raise AssertionError('Read without clipboard lock')
        self.reads+=1
        return self.raw


class CaptureTests(unittest.TestCase):
    def test_shelf_canonicalization_is_explicit_after_unlock_and_never_reads_body(self):
        with tempfile.TemporaryDirectory(prefix='fd-reference-canonical-') as directory:
            root=Path(directory).resolve();(root/'nested').mkdir();source=root/'original.bin';source.write_bytes(b'owned original')
            paths=(str(root/'nested'/'..'/'original.bin'),str(root/'nested'))
            provider=Provider(hdrop(paths));resolve=Path.resolve
            def canonical(path,*args,**options):
                self.assertTrue(provider.closed);self.assertFalse(provider.opened)
                return resolve(path,*args,**options)
            with patch.object(Path,'resolve',canonical),patch.object(Path,'open',side_effect=AssertionError('Reference normalization read file body')):
                value=capture(REQUEST,SEQUENCE,provider=provider,canonicalize=True)
            self.assertEqual(value['paths'],(str(source),str(root/'nested')))
            self.assertEqual(source.read_bytes(),b'owned original')

    def test_change_or_filesystem_failure_during_canonicalization_is_not_committed(self):
        provider=Provider()
        def changed(paths):provider.counter+=1;return paths
        with patch('win_clipboard_references.canonical_references',side_effect=changed):
            self.assertEqual(capture(REQUEST,SEQUENCE,provider=provider,canonicalize=True)['code'],'CHANGED')
        with patch('win_clipboard_references.canonical_references',side_effect=OSError(5,'private location')):
            value=capture(REQUEST,SEQUENCE,provider=Provider(),canonicalize=True)
        self.assertEqual(value['code'],'READ_UNAVAILABLE');self.assertNotIn('private',repr(value))

    def test_purpose_and_private_parser_keep_transfer_arguments_and_reject_ambiguous_flags(self):
        ordinary=ReferenceReadPurpose(REQUEST,SEQUENCE);shelf=ReferenceReadPurpose(REQUEST,SEQUENCE,canonicalize=True)
        self.assertEqual(ordinary.arguments(None),['--request',REQUEST,'--sequence',str(SEQUENCE)])
        self.assertEqual(parse_arguments(ordinary.arguments(None)),(REQUEST,SEQUENCE,False))
        self.assertEqual(parse_arguments(shelf.arguments(None)),(REQUEST,SEQUENCE,True))
        for flag in ('0','true','', '2'):
            with self.subTest(flag=flag),self.assertRaises(ValueError):parse_arguments(ordinary.arguments(None)+['--canonicalize',flag])
        with self.assertRaises(ValueError):parse_arguments(shelf.arguments(None)+['--canonicalize','1'])
        with self.assertRaises(ValueError):ReferenceReadPurpose(REQUEST,SEQUENCE,canonicalize=1)

    def test_multiple_files_directory_unc_and_original_order_without_fs_reads(self):
        provider=Provider()
        def parse(raw,**options):
            self.assertTrue(provider.closed);self.assertFalse(provider.opened)
            return parse_hdrop(raw,**options)
        with patch('win_clipboard_references.parse_hdrop',side_effect=parse),patch.object(Path,'stat',side_effect=AssertionError('No stat')):
            value=capture(REQUEST,SEQUENCE,provider=provider)
        self.assertEqual(value,receipt(REQUEST,SEQUENCE,'paths',paths=PATHS))
        self.assertEqual(provider.reads,1)
        for key in ('valid','known_empty','sync_eligible','file_stat','local_content'):
            self.assertNotIn(key,value)

    def test_qt_alias_fallback_occurs_only_after_unlock_and_preserves_all_refs(self):
        provider=Provider(advertised=False)
        def fallback():
            self.assertTrue(provider.closed);self.assertFalse(provider.opened)
            return PATHS
        self.assertEqual(capture(REQUEST,SEQUENCE,provider=provider,fallback=fallback)['paths'],PATHS)
        self.assertEqual(provider.reads,0)

    def test_unknown_advertisement_does_not_classify_aliases_as_text(self):
        provider=Provider(advertised=False)
        value=capture(REQUEST,SEQUENCE,provider=provider,fallback=lambda:PATHS)
        self.assertEqual(value['status'],'paths')
        provider=Provider(advertised=False)
        self.assertEqual(capture(REQUEST,SEQUENCE,provider=provider,fallback=lambda:())['status'],'not_files')

    def test_changed_before_open_never_reads_or_invokes_fallback(self):
        provider=Provider();provider.counter+=1
        with patch.object(provider,'open',side_effect=AssertionError('Stale read')):
            self.assertEqual(capture(REQUEST,SEQUENCE,provider=provider)['code'],'CHANGED')

    def test_change_during_lock_or_fallback_never_publishes_old_paths(self):
        provider=Provider();original=provider.read
        def read(*args):provider.counter+=1;return original(*args)
        provider.read=read
        self.assertEqual(capture(REQUEST,SEQUENCE,provider=provider)['code'],'CHANGED')
        self.assertTrue(provider.closed)
        provider=Provider(advertised=False)
        def fallback():provider.counter+=1;return PATHS
        self.assertEqual(capture(REQUEST,SEQUENCE,provider=provider,fallback=fallback)['code'],'CHANGED')

    def test_busy_and_close_failures_are_not_success_or_non_file_receipts(self):
        provider=Provider();provider.open=lambda:False
        self.assertEqual(capture(REQUEST,SEQUENCE,provider=provider)['code'],'BUSY')
        self.assertFalse(provider.closed)
        provider=Provider()
        provider.close=lambda:(_ for _ in ()).throw(OSError('private text must not escape'))
        value=capture(REQUEST,SEQUENCE,provider=provider)
        self.assertEqual(value['code'],'CLOSE_UNAVAILABLE');self.assertNotIn('private',repr(value))

    def test_malformed_or_ansi_data_is_checked_without_rewriting_paths(self):
        paths=('C:\\résumé.txt','D:\\directory')
        self.assertEqual(parse_hdrop(hdrop(paths,wide=False),ansi_codec='cp1252'),paths)
        for raw in (b'',hdrop()[:-4],struct.pack('<IiiII',19,0,0,0,1)+b'\0'*8,
                    struct.pack('<IiiII',20,0,0,0,1)+b'\0\xd8\0\0\0\0'):
            with self.subTest(size=len(raw)):
                with self.assertRaises((ValueError,UnicodeError)):parse_hdrop(raw)

    def test_wrong_nonce_sequence_schema_and_trailing_data_rejected(self):
        raw=encode_frame(receipt(REQUEST,SEQUENCE,'paths',paths=PATHS))
        self.assertEqual(decode_frame(raw,request=REQUEST,sequence=SEQUENCE)['paths'],PATHS)
        for nonce,seq,data in ((REQUEST,SEQUENCE,raw+b'x'),('b'*32,SEQUENCE,raw),(REQUEST,SEQUENCE+1,raw)):
            with self.assertRaises(ValueError):decode_frame(data,request=nonce,sequence=seq)
        with self.assertRaises(ValueError):receipt(REQUEST,SEQUENCE,'not_files',paths=PATHS)
        with self.assertRaises(ValueError):receipt(REQUEST,SEQUENCE,'not_files',paths='')
        body=raw[len(b'FDREF1\0')+4:]
        duplicate=body[:-1]+b',"request":"'+REQUEST.encode('ascii')+b'"}'
        with self.assertRaises(ValueError):
            decode_frame(b'FDREF1\0'+struct.pack('<I',len(duplicate))+duplicate,request=REQUEST,sequence=SEQUENCE)
        for args in ([],['--sequence','37','--request',REQUEST],['--request',REQUEST,'--sequence','037'],
                     ['--request',REQUEST,'--sequence','0'],['--request',REQUEST,'--sequence','4294967296'],
                     ['--request',REQUEST,'--sequence','37','--native-cache-root','ignored']):
            with self.assertRaises(ValueError):parse_arguments(args)

    def test_private_early_entry_never_reaches_gui_even_with_extra_flags(self):
        entry=str(Path(__file__).with_name('flickdock.py'))
        script="""
import runpy,sys,types,json
entry,arguments=sys.argv[1],json.loads(sys.argv[2])
from win_clipboard_references import parse_arguments
sys.platform='win32';sys.argv=[entry,*arguments]
poison=types.ModuleType('surface_morph')
def fail(name):raise AssertionError('GUI application reached')
poison.__getattr__=fail;sys.modules['surface_morph']=poison
helper=types.ModuleType('win_clipboard_references')
def main(args):
    try:parse_arguments(args)
    except ValueError:return 2
    return 39
helper.main=main;sys.modules['win_clipboard_references']=helper
runpy.run_path(entry,run_name='__main__')
"""
        args=['--clipboard-references','--request',REQUEST,'--sequence',str(SEQUENCE)]
        for arguments,code in ((args,39),(args+['--verify-cloud'],2),(['--other',*args],2)):
            result=subprocess.run([sys.executable,'-c',script,entry,json.dumps(arguments)],capture_output=True,timeout=5,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.assertEqual(result.returncode,code,result.stderr);self.assertEqual(result.stdout,b'')

    def test_qt_reference_alias_semantics_match_original_local_url_rule(self):
        from PySide6.QtCore import QUrl
        from types import SimpleNamespace
        paths=('/owned/a.bin','/owned/directory')
        for urls,expected in (([QUrl.fromLocalFile(path) for path in paths],paths),
            ([QUrl.fromLocalFile(paths[0]),QUrl('https://example.invalid')],()),
            ([QUrl('https://example.invalid')],()),([],())):
            mime=SimpleNamespace(hasUrls=lambda:bool(urls),urls=lambda:urls)
            app=SimpleNamespace(clipboard=lambda:SimpleNamespace(mimeData=lambda:mime))
            with patch('PySide6.QtGui.QGuiApplication',return_value=app),patch.object(Path,'stat',side_effect=AssertionError('No body lookup')):
                self.assertEqual(qt_local_references(),expected)

    def test_reference_metadata_limits_are_not_file_size_or_cloud_limits(self):
        with patch('clipboard_references.MAX_REFERENCE_BYTES',8):
            self.assertEqual(receipt(REQUEST,SEQUENCE,'paths',paths=['a'])['paths'],('a',))
            with self.assertRaises(ValueError):receipt(REQUEST,SEQUENCE,'paths',paths=['abc','def'])
        # The helper receives names only; even an offline UNC path is retained
        # without asking the source disk/network for its size or existence.
        with patch.object(Path,'stat',side_effect=AssertionError('No remote stat')):
            self.assertEqual(capture(REQUEST,SEQUENCE,provider=Provider())['paths'],PATHS)


class ReaderTests(unittest.TestCase):
    def setUp(self):ensure_owned_process_temp();self.readers=[]
    def tearDown(self):
        for reader in self.readers:
            reader.close();until(lambda:reader._active is None);reader.deleteLater()
        APP.processEvents()
    def reader(self,**options):
        value=WindowsClipboardReader(process_factory=lambda *args,**options:subprocess.Popen(*args,**options),**options)
        self.readers.append(value);return value
    @staticmethod
    def purpose(request,sequence,*,delay=0):
        class SyntheticReferences(ReferenceReadPurpose):
            def command(self):
                raw=encode_frame(receipt(self.request,self.sequence,'paths',paths=PATHS))
                return [sys.executable,'-c',f'import sys,time;time.sleep({delay!r});sys.stdout.buffer.write({raw!r})']
        return SyntheticReferences(request,sequence)
    def test_each_request_keeps_immutable_argv_nonce_decode_and_pending_receipt(self):
        reader=self.reader();values={}
        purposes=[self.purpose(char*32,SEQUENCE+index,delay=.1 if index==0 else 0)
                  for index,char in enumerate('abc')]
        with patch('win_clipboard_io.CacheCapability',side_effect=AssertionError('No content cache')):
            for index,purpose in enumerate(purposes):
                reader.read(lambda value,error,index=index:values.setdefault(index,value),purpose=purpose)
            until(lambda:len(values)==3)
        self.assertEqual(values[0]['request'],'a'*32);self.assertEqual(values[0]['status'],'paths')
        self.assertEqual(values[1]['request'],'b'*32);self.assertEqual(values[1]['code'],'SUPERSEDED')
        self.assertEqual(values[2]['request'],'c'*32);self.assertEqual(values[2]['sequence'],SEQUENCE+2)
        self.assertEqual(values[2]['status'],'paths');self.assertIsNone(reader._quarantined)

    def test_deadline_and_close_use_request_specific_failure_not_cloud_result(self):
        reader=self.reader(deadline_ms=50);values=[]
        purpose=self.purpose(REQUEST,SEQUENCE,delay=10)
        reader.read(lambda value,error:values.append(value),purpose=purpose);until(lambda:bool(values))
        self.assertEqual(values[0]['code'],'DEADLINE');self.assertEqual(values[0]['request'],REQUEST)
        self.assertNotIn('known_empty',values[0]);self.assertIsNone(reader._quarantined)
        reader.close();reader.read(lambda value,error:values.append(value),purpose=purpose);until(lambda:len(values)==2)
        self.assertEqual(values[1]['code'],'CLOSED');self.assertEqual(values[1]['request'],REQUEST)

    def test_dispatch_ready_never_waits_for_running_child_and_delivers_completed_once(self):
        reader=self.reader();values=[]
        reader.read(lambda value,error:values.append(value),purpose=self.purpose(REQUEST,SEQUENCE,delay=.125))
        started=time.perf_counter();reader.dispatch_ready()
        self.assertLess(time.perf_counter()-started,.1);self.assertEqual(values,[])
        # Wait on owned synthetic process completion, without pumping Qt. The
        # explicit dispatch must deliver its verified result before another key.
        self.assertTrue(reader._active.done.wait(2));self.assertTrue(reader._active.reaped)
        reader.dispatch_ready();reader.dispatch_ready()
        self.assertEqual(len(values),1);self.assertEqual(values[0]['status'],'paths')


if __name__=='__main__':unittest.main()
