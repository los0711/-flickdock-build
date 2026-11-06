"""Snapshots and delivery never overwrite or remove user originals."""
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import patch

from manual_payload import prepare,extract,publish


class ManualPayloadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-manual-payload-')
        self.root=Path(self.temp.name).resolve();self.cancel=threading.Event()
        self.original=self.root/'文件.bin';self.original.write_bytes(bytes(range(256))*4096)
        self.folder=self.root/'目录';self.folder.mkdir();(self.folder/'空目录').mkdir();(self.folder/'空文件').touch()
    def tearDown(self):self.temp.cleanup()
    def snapshot(self):return prepare([self.original,self.folder],self.root/'snapshot',self.cancel)
    def test_stable_snapshot_and_named_sibling_preserve_both_originals(self):
        job=self.snapshot();before=self.original.read_bytes()
        self.original.write_bytes(b'changed after snapshot')
        roots=extract(job['path'],self.root/'verified',job['descriptor'],
                      total=job['total'],count=job['count'],names=job['names'],cancel=self.cancel)
        target=self.root/'received';target.mkdir();(target/self.original.name).write_bytes(b'existing')
        paths=publish(self.root/'verified',target,roots,self.cancel)
        self.assertEqual((target/'文件 (2).bin').read_bytes(),before)
        self.assertEqual((target/self.original.name).read_bytes(),b'existing')
        self.assertEqual(self.original.read_bytes(),b'changed after snapshot')
        self.assertTrue((target/'目录/空目录').is_dir())
        self.assertTrue((target/'目录/空文件').is_file())
        self.assertEqual(len(paths),2)
    def test_corruption_or_offer_mismatch_never_publishes_a_file(self):
        job=self.snapshot()
        with self.assertRaises(ValueError):extract(job['path'],self.root/'wrong',job['descriptor'],
            total=job['total']+1,count=job['count'],names=job['names'],cancel=self.cancel)
        self.assertFalse((self.root/'wrong').exists())
        with job['path'].open('r+b') as stream:stream.seek(100);stream.write(b'corrupt')
        with self.assertRaises(ValueError):extract(job['path'],self.root/'corrupt',job['descriptor'],
            total=job['total'],count=job['count'],names=job['names'],cancel=self.cancel)
        self.assertFalse((self.root/'corrupt').exists())
    def test_cancel_rolls_back_earlier_promotions(self):
        job=self.snapshot();roots=extract(job['path'],self.root/'verified',job['descriptor'],
            total=job['total'],count=job['count'],names=job['names'],cancel=self.cancel)
        target=self.root/'received';target.mkdir()
        import lan_transfer
        rename=lan_transfer._rename_noreplace
        def cancel_after_first(source,destination):
            rename(source,destination)
            if Path(destination).parent==target:self.cancel.set()
        with patch.object(lan_transfer,'_rename_noreplace',cancel_after_first),self.assertRaises(InterruptedError):
            publish(self.root/'verified',target,roots,self.cancel)
        self.assertEqual(list(target.iterdir()),[])
        self.assertTrue(self.original.is_file())
        self.assertTrue((self.root/'verified'/roots[0]).exists())
    def test_complete_accepted_manifest_is_checked_before_extracting(self):
        job=self.snapshot();expected=[dict(row) for row in job['entries']]
        expected[-1]['path']='目录/another-name'
        with self.assertRaisesRegex(ValueError,'清单'):
            extract(job['path'],self.root/'wrong-manifest',job['descriptor'],total=job['total'],count=job['count'],
                    names=job['names'],cancel=self.cancel,expected_entries=expected)
        self.assertFalse((self.root/'wrong-manifest').exists())

    def test_preparation_is_not_throttled_by_network_priority_and_hashes_stream_once(self):
        from transfer_priority import set_clipboard_priority
        set_clipboard_priority(True);progress=[]
        try:
            with patch('manual_payload.digest_file',side_effect=AssertionError('Snapshot must not be read twice')), \
                 patch('transfer_priority._bulk_ready.wait',side_effect=AssertionError('Local disk work must not wait for network bandwidth')):
                job=prepare([self.original],self.root/'streamed',self.cancel,lambda done,total:progress.append((done,total)))
        finally:set_clipboard_priority(False)
        self.assertEqual(hashlib.sha256(job['path'].read_bytes()).hexdigest(),job['descriptor']['sha256'])
        self.assertEqual(job['path'].stat().st_size,job['descriptor']['size'])
        self.assertEqual(progress[-1],(self.original.stat().st_size,self.original.stat().st_size))
        with zipfile.ZipFile(job['path']) as archive:self.assertEqual(archive.read(self.original.name),self.original.read_bytes())

    def test_cancellation_during_snapshot_removes_only_owned_partial_output(self):
        before=self.original.read_bytes()
        with self.assertRaises(InterruptedError):
            prepare([self.original,self.folder],self.root/'cancelled',self.cancel,lambda done,total:self.cancel.set())
        self.assertFalse((self.root/'cancelled').exists());self.assertEqual(self.original.read_bytes(),before)


if __name__=='__main__':unittest.main()
