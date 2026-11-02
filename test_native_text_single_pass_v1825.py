"""Owned original validation: full raw hashes share the strict text scan."""
import gc,hashlib,json,os,struct,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
import win_clipboard_cache as cache
import win_clipboard_native as native


def fingerprint(text,html=b''):
    return hashlib.sha256(struct.pack('<QQ',len(text),len(html))+hashlib.sha256(text).digest()+hashlib.sha256(html).digest()).hexdigest()


def html_part(body,tail=b''):
    template=b'StartHTML:%010d\r\nEndHTML:%010d\r\n';start=len(template%(0,0))
    return template%(start,start+len(body))+body+tail


class NativeTextSinglePassTests(unittest.TestCase):
    def setUp(self):
        from test_win_clipboard_io_v1825 import ensure_owned_process_temp
        ensure_owned_process_temp();self.temp=tempfile.TemporaryDirectory(prefix='fd-native-text-stream-')
        self.base=Path(self.temp.name).resolve(strict=True)/'private';self.patch=patch.object(cache,'cache_base',return_value=self.base);self.patch.start()
    def tearDown(self):
        gc.collect();cache.schedule_cleanup();self.assertTrue(cache._IDLE.wait(2));self.patch.stop();self.temp.cleanup()
    def original(self,text,html=None,*,encoding='utf-16-le',tail=b''):
        raw=text.encode(encoding)+(b'\0\0' if encoding=='utf-16-le' else b'')+tail
        html_raw=html_part(html,b'\x00raw HTML tail') if html is not None else None
        cap=cache.CacheCapability();sink=cache.CacheSink(cap.root,cap.token,cap.instance)
        sink.write('text',[raw],encoding=encoding)
        if html_raw is not None:sink.write('html',[html_raw],encoding='utf-8')
        descriptor=sink.descriptor('text',len(text.encode())+len(html or b''),fingerprint(text.encode(),html or b''));sink.close()
        return cap,descriptor,raw,html_raw
    def test_adopt_has_no_separate_full_raw_read_pass(self):
        text='x'*(4*1024*1024+1);body=b'<p>'+b'y'*262144+b'</p>'
        cap,descriptor,raw,html=self.original(text,body)
        paths={cap.root/(part['name']+'.bin'):part['name'] for part in descriptor['local_parts']}
        inodes={(path.stat().st_dev,path.stat().st_ino):name for path,name in paths.items()}
        counts={name:dict(file_read_bytes=0,mapped_chunk_bytes=0) for name in paths.values()};maps={}
        real_open=Path.open;real_map=cache.mmap.mmap;real_chunks=native._chunks
        class Stream:
            def __init__(self,source,name):self.source=source;self.name=name
            def read(self,*a,**kw):
                value=self.source.read(*a,**kw);counts[self.name]['file_read_bytes']+=len(value);return value
            def __getattr__(self,name):return getattr(self.source,name)
            def __enter__(self):self.source.__enter__();return self
            def __exit__(self,*a):return self.source.__exit__(*a)
        def opened(path,*a,**kw):
            source=real_open(path,*a,**kw)
            return Stream(source,paths[path]) if path in paths and (a[0] if a else kw.get('mode','r'))=='rb' else source
        def mapped(fd,*a,**kw):
            value=real_map(fd,*a,**kw);info=os.fstat(fd);maps[id(value)]=inodes[(info.st_dev,info.st_ino)];return value
        def chunks(value,*a,**kw):
            for part in real_chunks(value,*a,**kw):
                if id(value) in maps:counts[maps[id(value)]]['mapped_chunk_bytes']+=len(part)
                yield part
        with patch.object(Path,'open',opened),patch.object(cache.mmap,'mmap',side_effect=mapped),patch.object(native,'_chunks',chunks):content=cap.adopt(descriptor)
        print(json.dumps(dict(case='parent_raw_traversals',counts=counts,raw_sizes=dict(text=len(raw),html=len(html)))))
        self.assertEqual(sum(row['file_read_bytes'] for row in counts.values()),0,'Raw SHA must not rescan the complete file before strict decoding')
        self.assertEqual(counts['text']['mapped_chunk_bytes'],len(raw));self.assertEqual(counts['html']['mapped_chunk_bytes'],len(html))
        self.assertEqual(content.fingerprint,('text',descriptor['fingerprint']))
        del content,cap
    def test_optional_raw_digests_cover_terminator_text_tail_and_html_framing(self):
        for encoding,text in (('utf-16-le','\ufeff甲\U0001f642'),('utf-8','\ufefftext\0kept')):
            raw=text.encode(encoding)+(b'\0\0\x00\xd8raw tail' if encoding=='utf-16-le' else b'')
            body='<b>完整格式</b>'.encode();html=html_part(body,b'\xffraw tail');digests={}
            result=native._text_parts(raw,html,0,encoding=encoding,part_digests=digests)
            self.assertEqual(digests,dict(text=hashlib.sha256(raw).hexdigest(),html=hashlib.sha256(html).hexdigest()))
            self.assertEqual(result[2],fingerprint(text.encode(),body))
    def test_same_logical_identity_cannot_hide_corrupt_raw_suffix_or_html_header(self):
        for part,offset in (('text',-1),('html','header'),('html',-1)):
            with self.subTest(part=part,offset=offset):
                cap,descriptor,raw,html=self.original('same logical text',b'<b>same HTML</b>',tail=b'XY')
                path=cap.root/(part+'.bin');changed=bytearray(path.read_bytes())
                if offset=='header':changed[changed.index(13)]=10 # Valid offsets and logical body are unchanged.
                else:changed[offset]^=1
                path.write_bytes(changed)
                with self.assertRaisesRegex(ValueError,'hash mismatch'):cap.adopt(descriptor)
                self.assertIsNone(cap.lock);del cap
    def test_all_unicode_is_strict_even_with_matching_raw_digest(self):
        cap,descriptor,raw,_=self.original('x'*150000)
        bad=b'x\0'*150000+b'\0\xd8\0\0';path=cap.root/'text.bin';path.write_bytes(bad)
        descriptor['local_parts'][0].update(size=len(bad),sha256=hashlib.sha256(bad).hexdigest())
        with self.assertRaises(UnicodeError):cap.adopt(descriptor)
        self.assertIsNone(cap.lock);del cap
    def test_cancellation_during_raw_tail_never_publishes_digest_or_lease(self):
        raw=b'x\0\0\0'+b'T'*(native.NATIVE_COPY_CHUNK_BYTES+2);digests={}
        class TailCancel:
            def __init__(self):self.calls=0
            def is_set(self):self.calls+=1;return self.calls>1
        with self.assertRaises(InterruptedError):native._text_parts(raw,None,0,cancel=TailCancel(),part_digests=digests)
        self.assertEqual(digests,{})
        cap,descriptor,raw,_=self.original('content',tail=b'T'*native.NATIVE_COPY_CHUNK_BYTES);cap.cancel=TailCancel()
        with self.assertRaises(InterruptedError):cap.adopt(descriptor)
        self.assertIsNone(cap.lock);del cap
    def test_image_still_uses_existing_complete_hash_validation(self):
        cap=cache.CacheCapability();sink=cache.CacheSink(cap.root,cap.token,cap.instance)
        raw=b'original opaque image-kind bytes';sink.write('image',[raw],mime='image/png');descriptor=sink.descriptor('image',len(raw),hashlib.sha256(raw).hexdigest());sink.close()
        (cap.root/'image.bin').write_bytes(b'X'*len(raw))
        with patch.object(native,'_text_parts',side_effect=AssertionError('Image must not use text path')):
            with self.assertRaisesRegex(ValueError,'hash mismatch'):cap.adopt(descriptor)
        self.assertIsNone(cap.lock);del cap


if __name__=='__main__':unittest.main()
