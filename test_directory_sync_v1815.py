"""148 MiB Windows-style folder staging and cross-device receive recovery."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from uuid import uuid4
from unittest.mock import patch
from sync_files import directory_size,stage_item,unpack_directory,DIRECTORY_MIME
from shelf_sync import ShelfSync
from test_sync_receive_priority import Service,Adapter,file_record,spin,APP


class DirectorySyncTests(unittest.TestCase):
    def test_148_mib_folder_is_full_archive_and_other_device_gets_original_bytes(self):
        with tempfile.TemporaryDirectory(prefix='flickdock-folder-148m-') as directory:
            root=Path(directory);source=root/'项目资料';(source/'empty'/'nested').mkdir(parents=True)
            chunk=os.urandom(1024*1024);expected={}
            for filename,size in (('视频素材.mp4',98),('设计原稿.7z',50)):
                path=source/filename;digest=hashlib.sha256()
                with path.open('wb') as stream:
                    for _ in range(size):stream.write(chunk);digest.update(chunk)
                expected[filename]=(size*1024*1024,digest.hexdigest())
            cancel=threading.Event();metrics=directory_size(source,cancel)
            self.assertEqual(metrics['logical_size'],148*1024*1024)
            staged=stage_item(str(source),None,source.name,root/'outbox',cancel)
            self.assertGreater(staged['size'],148*1024*1024);self.assertEqual(staged['mime_type'],DIRECTORY_MIME)
            blob=Path(staged['path']).read_bytes();identity=uuid4().hex
            record=file_record(identity,source.name,blob);record['mime_type']=DIRECTORY_MIME
            service=Service([dict(cursor='1',record=record)],{identity:blob});adapter=Adapter()
            engine=ShelfSync(root/'receiver',service,adapter,poll_ms=600000)
            try:
                service.account_changed.emit({'id':'folder-receiver'})
                deadline=time.monotonic()+45
                while time.monotonic()<deadline and engine.state.get('cursor')!='1':
                    APP.processEvents();service.api.release_large();time.sleep(.002)
                self.assertEqual(engine.state.get('cursor'),'1');self.assertFalse(engine.download_transfers)
                self.assertFalse(engine.state.get('deferred_downloads'))
                destination=Path(adapter.view['permanent'][0]);self.assertTrue((destination/'empty'/'nested').is_dir())
                for name,(size,digest) in expected.items():
                    path=destination/name;self.assertEqual(path.stat().st_size,size)
                    with path.open('rb') as stream:self.assertEqual(hashlib.file_digest(stream,'sha256').hexdigest(),digest)
            finally:engine.close()

    def test_extraction_replay_is_idempotent_and_preserves_an_edited_local_folder(self):
        with tempfile.TemporaryDirectory(prefix='flickdock-folder-replay-') as directory:
            root=Path(directory);source=root/'source';source.mkdir();(source/('a'*170+'.txt')).write_bytes(b'original file')
            cancel=threading.Event();staged=stage_item(str(source),None,'folder',root/'outbox',cancel)
            target=root/'downloaded';first=unpack_directory(staged['path'],target,cancel)
            self.assertEqual(unpack_directory(staged['path'],target,cancel),first)
            (target/('a'*170+'.txt')).write_bytes(b'local edit')
            second=Path(unpack_directory(staged['path'],target,cancel));self.assertNotEqual(second,target)
            self.assertEqual((target/('a'*170+'.txt')).read_bytes(),b'local edit')
            self.assertEqual((second/('a'*170+'.txt')).read_bytes(),b'original file')

    def test_failed_folder_does_not_block_later_file_and_is_cancellable(self):
        bad,good=uuid4().hex,uuid4().hex;broken=b'invalid archive';data=b'later file keeps syncing'
        first=file_record(bad,'broken-folder',broken);first['mime_type']=DIRECTORY_MIME
        service=Service([dict(cursor='1',record=first),dict(cursor='2',record=file_record(good,'later.txt',data))],{bad:broken,good:data})
        adapter=Adapter()
        with tempfile.TemporaryDirectory(prefix='flickdock-folder-failed-') as root:
            engine=ShelfSync(root,service,adapter,poll_ms=600000)
            try:
                service.account_changed.emit({'id':'folder-failed-account'})
                self.assertTrue(spin(lambda:engine.state.get('cursor')=='2' and not engine.running))
                self.assertEqual(Path(adapter.view['permanent'][0]).read_bytes(),data)
                self.assertIn(bad,engine.state['deferred_downloads']);self.assertTrue(engine.cancel_download(bad))
                self.assertNotIn(bad,engine.state['deferred_downloads']);self.assertNotIn(bad,engine.download_transfers)
            finally:engine.close()

    def test_completed_valid_body_with_lost_http_completion_finishes_without_retry(self):
        identity=uuid4().hex;blob=b'complete original file bytes'
        service=Service([dict(cursor='1',record=file_record(identity,'complete.bin',blob))],{identity:blob})
        service.api.fail_after_complete_once=identity
        with tempfile.TemporaryDirectory(prefix='flickdock-completed-body-') as root:
            adapter=Adapter();engine=ShelfSync(root,service,adapter,poll_ms=600000)
            try:
                service.account_changed.emit({'id':'valid-completed-body'})
                self.assertTrue(spin(lambda:engine.state.get('cursor')=='1' and not engine.running))
                self.assertEqual(Path(adapter.view['permanent'][0]).read_bytes(),blob)
                self.assertFalse(engine.download_transfers);self.assertFalse(engine.state.get('deferred_downloads'))
            finally:engine.close()

    def test_verified_assembled_file_reused_when_extraction_is_retried(self):
        identity=uuid4().hex;blob=b'cached verified bytes'
        record=file_record(identity,'cached.bin',blob);service=Service([],{});adapter=Adapter()
        with tempfile.TemporaryDirectory(prefix='flickdock-retry-cache-') as root:
            engine=ShelfSync(root,service,adapter,poll_ms=600000)
            try:
                service.account_changed.emit({'id':'verified-retry-cache'})
                self.assertTrue(spin(lambda:not engine.running and engine.state is not None))
                engine.debounce.stop();engine.timer.stop()
                target=engine._download_target(record);target.parent.mkdir(parents=True);target.write_bytes(blob);result=[]
                with patch.object(engine,'_request',side_effect=AssertionError('A verified completed file needs no repeated download')):
                    engine._download_record(record,target,lambda path,error:result.append((path,error)))
                    self.assertTrue(spin(lambda:bool(result)))
                self.assertIsNone(result[0][1]);self.assertEqual(result[0][0].read_bytes(),blob)
            finally:engine.close()


if __name__=='__main__':unittest.main(verbosity=2)
