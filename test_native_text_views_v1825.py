"""Borrowed text analysis releases maps even while callers retain failures."""
import hashlib,mmap,tempfile,unittest
from pathlib import Path
import win_clipboard_native as native


def html(body):
    template=b'StartHTML:%010d\r\nEndHTML:%010d\r\n'
    start=len(template%(0,0))
    return template%(start,start+len(body))+body


class NativeTextViewsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='fd-text-view-owned-')
        self.root=Path(self.temp.name);self.maps=[]
    def tearDown(self):
        for raw in self.maps:raw.close()
        self.temp.cleanup()
    def mapped(self,name,raw):
        path=self.root/name;path.write_bytes(raw)
        with path.open('rb') as source:raw=mmap.mmap(source.fileno(),0,access=mmap.ACCESS_READ)
        self.maps.append(raw);return raw
    def test_slices_borrow_original_buffer_and_release_after_scope(self):
        raw=self.mapped('view.bin',b'x'*300000)
        with native._text_spans(raw) as source:
            part=next(source)
            self.assertIsInstance(part,memoryview);self.assertIs(part.obj,raw)
            self.assertEqual(len(part),native.NATIVE_COPY_CHUNK_BYTES)
        with self.assertRaises(ValueError):len(part)
        raw.close();self.assertTrue(raw.closed)
    def test_success_returns_only_owned_values_and_all_original_digests(self):
        text='a'*(native.NATIVE_COPY_CHUNK_BYTES//2-1)+'😀中尾'
        text_raw=text.encode('utf-16-le')+b'\0\0raw padding!'
        html_raw=html('<p>完整</p>'.encode())+b'\xffsuffix'
        raw=self.mapped('text.bin',text_raw);formatted=self.mapped('html.bin',html_raw)
        digests={};value,size,fingerprint,end=native._text_parts(raw,formatted,1000000,part_digests=digests)
        self.assertEqual(value,dict(text=text,html='<p>完整</p>'))
        self.assertEqual(size,len(text.encode())+len('<p>完整</p>'.encode()))
        self.assertEqual(digests,dict(text=hashlib.sha256(text_raw).hexdigest(),html=hashlib.sha256(html_raw).hexdigest()))
        raw.close();formatted.close();self.assertTrue(raw.closed and formatted.closed)
        self.assertEqual(value['text'],text)
    def test_invalid_utf16_traceback_does_not_export_the_map(self):
        raw=self.mapped('invalid-text.bin',b'x\0'*150000+b'\0\xd8\0\0')
        try:native._text_parts(raw,None,0,part_digests={})
        except UnicodeDecodeError as error:
            held=error.__traceback__;self.assertIsNotNone(held)
            raw.close();self.assertTrue(raw.closed)
            self.assertIs(error.__traceback__,held)
        else:self.fail('Strict Unicode validation was bypassed')
    def test_invalid_html_traceback_releases_text_and_html_maps(self):
        raw=self.mapped('valid-text.bin',b'x\0\0\0')
        formatted=self.mapped('invalid-html.bin',html(b'<p>\xff</p>'))
        digests={}
        try:native._text_parts(raw,formatted,0,part_digests=digests)
        except UnicodeDecodeError as error:
            held=error.__traceback__;raw.close();formatted.close()
            self.assertTrue(raw.closed and formatted.closed);self.assertIs(error.__traceback__,held)
            self.assertEqual(digests,{})
        else:self.fail('Invalid HTML acquired a valid original')
    def test_body_or_raw_tail_cancellation_keeps_original_exception_and_closes(self):
        sample=b'x\0'*(native.NATIVE_COPY_CHUNK_BYTES//2+2)+b'\0\0'+b'P'*native.NATIVE_COPY_CHUNK_BYTES
        for after in (0,1,2):
            with self.subTest(after=after):
                raw=self.mapped('cancel-'+str(after)+'.bin',sample);digests={}
                class Cancel:
                    calls=0
                    def is_set(self):self.calls+=1;return self.calls>after
                try:native._text_parts(raw,None,0,cancel=Cancel(),part_digests=digests)
                except InterruptedError as error:
                    held=error.__traceback__;raw.close()
                    self.assertTrue(raw.closed);self.assertIs(error.__traceback__,held);self.assertEqual(digests,{})
                else:self.fail('Cancellation did not stop complete analysis')
    def test_consumer_failure_explicitly_closes_generator_before_map(self):
        raw=self.mapped('consumer.bin',b'x'*300000);failure=OSError(5,'owned synthetic consumer')
        try:
            with native._text_spans(raw) as source:
                retained=next(source);raise failure
        except OSError as error:
            self.assertIs(error,failure);held=error.__traceback__;raw.close()
            self.assertTrue(raw.closed);self.assertIs(error.__traceback__,held)
            with self.assertRaises(ValueError):len(retained)
    def test_custom_slice_provider_keeps_semantics_and_releases_its_views(self):
        raw=self.mapped('custom.bin',b'x\0'*4096+b'\0\0')
        class Original:
            def __len__(self):return len(raw)
            def find(self,*args):return raw.find(*args)
            def __getitem__(self,key):return memoryview(raw)[key]
        result=native._text_parts(Original(),None,0,part_digests={})
        self.assertEqual(result[1],4096);self.assertIsNone(result[0]);raw.close();self.assertTrue(raw.closed)


if __name__=='__main__':unittest.main()
