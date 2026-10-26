"""Original-byte staging, bounded cancellation and adaptive directory transfer."""
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile
import sync_files


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='flickdock-stage-review-')
        self.root=Path(self.temporary.name);self.outbox=self.root/'outbox';self.cancel=threading.Event()

    def tearDown(self):self.temporary.cleanup()

    def stage_without_reread(self,source,text,name):
        original_open=Path.open
        def open_checked(path,mode='r',*args,**kwargs):
            if path.parent==self.outbox and 'r' in mode:raise AssertionError('Raw/text staging must hash while writing, without a second full-file read')
            return original_open(path,mode,*args,**kwargs)
        with patch.object(Path,'open',open_checked):
            return sync_files.stage_item(str(source),text,name,self.outbox,self.cancel)

    def test_raw_bytes_are_hashed_during_copy_without_reopening_staged_file(self):
        data=b'\x89PNG\r\n\x1a\noriginal-metadata\0'+bytes(range(256))*8193
        source=self.root/'original.png';source.write_bytes(data)
        staged=self.stage_without_reread(source,None,source.name)
        self.assertEqual(Path(staged['path']).read_bytes(),data)
        self.assertEqual(staged['sha256'],hashlib.sha256(data).hexdigest());self.assertEqual(staged['size'],len(data))
        source.unlink();self.assertEqual(Path(staged['path']).read_bytes(),data)

    def test_text_and_empty_files_need_no_hash_scan(self):
        text='原文\n原始文本内容🙂'*100
        staged=self.stage_without_reread('',text,'文字')
        self.assertEqual(Path(staged['path']).read_bytes(),text.encode('utf8'));self.assertEqual(staged['sha256'],hashlib.sha256(text.encode('utf8')).hexdigest())
        source=self.root/'empty.bin';source.touch();empty=self.stage_without_reread(source,None,source.name)
        self.assertEqual(empty['size'],0);self.assertEqual(empty['sha256'],hashlib.sha256(b'').hexdigest())

    def test_directory_stores_media_compresses_text_and_preserves_every_original_byte(self):
        source=self.root/'folder';(source/'empty'/'nested').mkdir(parents=True)
        media=bytes(range(256))*4097;text=b'hello original text\n'*65536
        (source/'image.png').write_bytes(media);(source/'code.txt').write_bytes(text)
        staged=sync_files.stage_item(str(source),None,'folder',self.outbox,self.cancel)
        with zipfile.ZipFile(staged['path']) as archive:
            image,code=archive.getinfo('image.png'),archive.getinfo('code.txt')
            self.assertEqual(image.compress_type,zipfile.ZIP_STORED);self.assertEqual(image.compress_size,len(media))
            self.assertEqual(code.compress_type,zipfile.ZIP_DEFLATED);self.assertLess(code.compress_size,len(text)//10)
            self.assertEqual(archive.read(image),media);self.assertEqual(archive.read(code),text)
        target=self.root/'restored';sync_files.unpack_directory(staged['path'],target,self.cancel)
        self.assertEqual((target/'image.png').read_bytes(),media);self.assertEqual((target/'code.txt').read_bytes(),text)
        self.assertTrue((target/'empty'/'nested').is_dir())
        raw=Path(staged['path']).read_bytes();self.assertEqual(staged['sha256'],hashlib.sha256(raw).hexdigest())

    def test_cancelled_and_over_limit_staging_removes_only_its_partial_copy(self):
        data=bytes(range(256))*8192;source=self.root/'source.bin';source.write_bytes(data)
        with self.assertRaises(ValueError):sync_files.stage_item(str(source),None,'source.bin',self.outbox,self.cancel,1024)
        self.assertEqual(list(self.outbox.iterdir()),[]);self.assertEqual(source.read_bytes(),data)
        self.cancel.set()
        with self.assertRaises(InterruptedError):sync_files.stage_item(str(source),None,'source.bin',self.outbox,self.cancel)
        self.assertEqual(list(self.outbox.iterdir()),[]);self.assertEqual(source.read_bytes(),data)


if __name__=='__main__':unittest.main(verbosity=2)
