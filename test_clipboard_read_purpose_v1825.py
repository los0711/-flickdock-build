"""Purpose bindings share one owned reader lifecycle, not cloud payload policy."""
import json,subprocess,sys,unittest
from unittest.mock import patch
from test_win_clipboard_io_v1825 import APP,until,ensure_owned_process_temp
from win_clipboard_io import WindowsClipboardReader

class ReferencePurpose:
    max_wire=128
    def cache(self,cancel):return None
    def arguments(self,cache):
        if cache is not None:raise AssertionError('Reference capture acquired an original-content cache')
        return []
    def decode(self,raw,cache):
        if cache is not None or not raw.startswith(b'owned-refs:'):raise ValueError('Invalid synthetic reference frame')
        return dict(purpose='owned-reference-test',status='ready',paths=tuple(json.loads(raw[len(b'owned-refs:'):])))
    def failure(self,code='READ_UNAVAILABLE',diagnostic=None):
        return dict(purpose='owned-reference-test',status='failed',code=code,diagnostic=diagnostic)
    def command(self):return [sys.executable,'-c',"import sys;assert len(sys.argv)==1;sys.stdout.buffer.write(b'owned-refs:[\"D:/owned file\",\"D:/owned directory\"]')"]

class ReadPurposeTests(unittest.TestCase):
    def setUp(self):ensure_owned_process_temp();self.readers=[]
    def tearDown(self):
        for reader in self.readers:
            reader.close();until(lambda:reader._active is None);reader.deleteLater()
        APP.processEvents()
    def reader(self,**options):
        reader=WindowsClipboardReader(purpose=ReferencePurpose(),process_factory=lambda *args,**options:subprocess.Popen(*args,**options),**options)
        self.readers.append(reader);return reader
    def test_reference_result_has_no_cloud_baseline_and_never_creates_original_cache(self):
        reader=self.reader();values=[]
        with patch('win_clipboard_io.CacheCapability',side_effect=AssertionError('No content cache needed')):
            reader.read(lambda value,error:values.append(value));until(lambda:bool(values))
        self.assertEqual(values,[dict(purpose='owned-reference-test',status='ready',paths=('D:/owned file','D:/owned directory'))])
        self.assertNotIn('valid',values[0]);self.assertNotIn('known_empty',values[0]);self.assertFalse(reader._clock.isActive())
    def test_invalid_reference_frame_keeps_specific_purpose_failure(self):
        reader=self.reader(command=[sys.executable,'-c',"print('bad owned frame')"]);values=[]
        reader.read(lambda value,error:values.append(value));until(lambda:bool(values))
        self.assertEqual(values[0]['code'],'INVALID_FRAME');self.assertEqual(values[0]['purpose'],'owned-reference-test')
    def test_purpose_wire_limit_does_not_fall_back_to_cloud_body_limit(self):
        reader=self.reader(command=[sys.executable,'-c',"import sys;sys.stdout.buffer.write(b'x'*4096)"]);values=[]
        reader.read(lambda value,error:values.append(value));until(lambda:bool(values))
        self.assertEqual(values[0]['code'],'OUTPUT_LIMIT');self.assertEqual(values[0]['purpose'],'owned-reference-test')
    def test_deadline_reaps_its_child_and_next_reference_can_complete(self):
        reader=self.reader(deadline_ms=50,command=[sys.executable,'-c',"import time;time.sleep(10)"]);values=[]
        reader.read(lambda value,error:values.append(value));until(lambda:bool(values))
        self.assertEqual(values[0]['code'],'DEADLINE');self.assertIsNone(reader._quarantined)
        reader.command=None;reader.deadline_ms=1000;reader.read(lambda value,error:values.append(value));until(lambda:len(values)==2)
        self.assertEqual(values[1]['status'],'ready');self.assertIsNone(reader._active)
    def test_closed_request_uses_purpose_failure_without_starting_process(self):
        reader=self.reader();reader.close();values=[]
        reader.read(lambda value,error:values.append(value));until(lambda:bool(values))
        self.assertEqual(values[0]['code'],'CLOSED');self.assertEqual(values[0]['purpose'],'owned-reference-test')

if __name__=='__main__':unittest.main()
