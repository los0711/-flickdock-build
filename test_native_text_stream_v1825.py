"""One complete native text pass feeds the independently verified cache."""
import hashlib,mmap,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import win_clipboard_native as native


class Provider:
    def __init__(self,raw):self.raw=raw;self.opened=False
    def sequence(self):return 17
    def open(self):self.opened=True;return True
    def close(self):self.opened=False
    def count(self):return 1
    def available(self,name):return name==13
    def read_snapshot(self,name):return self.raw


class Sink:
    def __init__(self,provider):self.provider=provider;self.parts=[];self.output=[]
    def write(self,name,chunks,**metadata):
        if self.provider.opened:raise AssertionError('Clipboard lock held during cache write')
        digest=hashlib.sha256();size=0
        for chunk in chunks:
            value=bytes(chunk);self.output.append(value);digest.update(value);size+=len(value)
        part=dict(name=name,size=size,sha256=digest.hexdigest(),**metadata);self.parts.append(part);return part
    def descriptor(self,kind,size,fingerprint):return dict(local_kind=kind,logical_size=size,fingerprint=fingerprint,local_parts=self.parts)


class NativeTextStreamTests(unittest.TestCase):
    def test_proven_large_text_writes_original_once_without_second_chunk_scan(self):
        text='a'*300000+'😀中';original=text.encode('utf-16-le')+b'\0\0';raw=original+b'ignored padding!'
        provider=Provider(raw);sink=Sink(provider)
        with patch.object(native,'_chunks',side_effect=AssertionError('Second original scan')):
            value=native.capture(provider,32,cache=sink)
        self.assertEqual(value['status'],'ready');self.assertFalse(value['sync_eligible'])
        self.assertEqual(value['logical_size'],len(text.encode()))
        self.assertEqual(b''.join(sink.output),original)
        self.assertEqual(sink.parts,[dict(name='text',size=len(original),sha256=hashlib.sha256(original).hexdigest(),encoding='utf-16-le')])
        self.assertEqual(value['fingerprint'],native._text_parts(raw,None,32)[2])
    def test_small_text_does_not_create_cache_or_change_upload_value(self):
        provider=Provider('中文🙂'.encode('utf-16-le')+b'\0\0');sink=Sink(provider)
        value=native.capture(provider,100,cache=sink)
        self.assertEqual(value['value'],('text',dict(text='中文🙂',html='')))
        self.assertTrue(value['sync_eligible']);self.assertEqual(sink.parts,[])
    def test_consumer_error_preserves_failure_and_releases_retained_borrow(self):
        with tempfile.TemporaryDirectory(prefix='fd-stream-owned-') as directory:
            path=Path(directory)/'text';path.write_bytes(b'x\0'*300000+b'\0\0')
            with path.open('rb') as source:raw=mmap.mmap(source.fileno(),0,access=mmap.ACCESS_READ)
            failure=OSError(28,'owned consumer failure')
            try:
                with native._text_analysis(raw,None,0,cache_prefix=True) as (chunks,analysis):
                    retained=next(chunks);raise failure
            except OSError as error:
                self.assertIs(error,failure);held=error.__traceback__;raw.close()
                self.assertTrue(raw.closed);self.assertIs(error.__traceback__,held);self.assertEqual(analysis,[])
                with self.assertRaises(ValueError):len(retained)
    def test_invalid_unicode_has_no_completed_analysis_and_releases_map(self):
        with tempfile.TemporaryDirectory(prefix='fd-stream-owned-') as directory:
            path=Path(directory)/'text';path.write_bytes(b'x\0'*150000+b'\0\xd8\0\0')
            with path.open('rb') as source:raw=mmap.mmap(source.fileno(),0,access=mmap.ACCESS_READ)
            emitted=0
            try:
                with native._text_analysis(raw,None,0,cache_prefix=True) as (chunks,analysis):
                    for chunk in chunks:emitted+=len(chunk)
            except UnicodeDecodeError as error:
                held=error.__traceback__;raw.close();self.assertTrue(raw.closed)
                self.assertEqual(analysis,[]);self.assertGreater(emitted,0);self.assertIs(error.__traceback__,held)
            else:self.fail('Invalid Unicode produced a usable cache')


class NativeTextTerminatorTests(unittest.TestCase):
    def test_unaligned_false_matches_do_not_create_one_python_search_per_character(self):
        raw=b'x\0\0\x01'*262144+b'\0\0'
        class Original:
            def __init__(self):self.find_calls=0
            def __len__(self):return len(raw)
            def __getitem__(self,key):return memoryview(raw)[key]
            def find(self,*args):self.find_calls+=1;return raw.find(*args)
        original=Original();self.assertEqual(native._text_end(original,'utf-16-le'),len(raw)-2)
        self.assertLessEqual(original.find_calls,1+len(raw)//native.NATIVE_COPY_CHUNK_BYTES,
            'Aligned Unicode metadata must not cause a Python callback for every false byte match')

    def test_first_aligned_nul_survives_surrogates_and_chunk_boundaries(self):
        for text in ('', 'short text', '😀中', 'x'*(native.NATIVE_COPY_CHUNK_BYTES//2-1)+'😀尾',
                     '\u0100x'*40000, 'x'*(native.NATIVE_COPY_CHUNK_BYTES//2)):
            with self.subTest(characters=len(text)):
                raw=text.encode('utf-16-le')+b'\0\0\0\xd8ignored invalid tail'
                if len(raw)%2:raw+=b'!'
                self.assertEqual(native._text_end(raw,'utf-16-le'),len(text.encode('utf-16-le')))

    def test_invalid_logical_unicode_still_fails_complete_strict_analysis(self):
        raw=b'x\0'*150000+b'\0\xd8\0\0';digests={}
        self.assertEqual(native._text_end(raw,'utf-16-le'),len(raw)-2)
        with self.assertRaises(UnicodeDecodeError):native._text_parts(raw,None,0,part_digests=digests)
        self.assertEqual(digests,{})

    def test_missing_terminator_odd_length_and_unsupported_encoding_stay_invalid(self):
        for raw,encoding in ((b'', 'utf-16-le'),(b'x\0', 'utf-16-le'),
                             (b'x\0\0', 'utf-16-le'),(b'x', 'unsupported')):
            with self.subTest(length=len(raw),encoding=encoding),self.assertRaises(native.Unsupported):
                native._text_end(raw,encoding)
        self.assertEqual(native._text_end(b'utf8\0kept','utf-8'),9)

    def test_failed_metadata_scan_releases_borrow_even_with_retained_traceback(self):
        with tempfile.TemporaryDirectory(prefix='fd-end-owned-') as directory:
            path=Path(directory)/'unterminated';path.write_bytes(b'x\0'*300000)
            with path.open('rb') as source:raw=mmap.mmap(source.fileno(),0,access=mmap.ACCESS_READ)
            try:native._text_end(raw,'utf-16-le')
            except native.Unsupported as error:
                traceback=error.__traceback__;raw.close()
                self.assertTrue(raw.closed);self.assertIs(error.__traceback__,traceback)
            else:self.fail('Missing native terminator was accepted')


if __name__=='__main__':unittest.main()
