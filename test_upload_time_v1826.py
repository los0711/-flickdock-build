"""Upload metadata ownership, request coalescing and account/source fences."""
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from PySide6.QtCore import QObject,Signal
from test_html_native_workspace import APP,flush
from shelf_sync import ShelfSync


class Service(QObject):
    account_changed=Signal(object)
    signed_in=True

    def __init__(self):
        super().__init__();self.api=self;self.calls=[]

    def request(self,method,path,callback,**kwargs):
        key=object();self.calls.append((method,path,callback,key));return key

    def cancel(self,key):pass


class UploadTimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-upload-time-')
        self.service=Service()
        self.engine=ShelfSync(self.temp.name,self.service,SimpleNamespace(snapshot=lambda:{}))
        self.engine.account_id='metadata-account'
        self.entry=dict(id='12345678-1234-4234-8234-123456789abc',ref='local-ref',version=1,sha256='c'*64)
        self.engine.state={'entries':{self.entry['id']:self.entry}}
        self.engine._ref_index={'local-ref':self.entry}
        self.result=dict(id=self.entry['id'],version=1,sha256=self.entry['sha256'],uploaded_at='2026-11-04T01:02:03.000Z')

    def tearDown(self):
        self.engine.state=None;self.engine.close();self.engine.deleteLater();flush();self.temp.cleanup()

    def reply(self,result=None,error=None):
        self.service.calls[-1][2](self.result if result is None else result,error)

    def test_one_request_persists_actual_upload_time_and_rename_keeps_it(self):
        self.assertIsNone(self.engine.upload_time_for_ref('local-ref'))
        for _ in range(10):self.engine.upload_time_for_ref('local-ref')
        self.assertEqual(len(self.service.calls),1)
        self.reply()
        self.assertEqual(self.engine.upload_time_for_ref('local-ref'),self.result['uploaded_at'])
        self.entry['version']=2
        self.assertEqual(self.engine.upload_time_for_ref('local-ref'),self.result['uploaded_at'])
        self.assertEqual(len(self.service.calls),1)
        # The journal writes on its own worker; the upload time is durable once
        # the journal reports the revision, not synchronously inside the reply.
        deadline = time.monotonic() + 5
        while not self.engine._profile('metadata-account').is_file() and time.monotonic() < deadline:
            self.engine._journal.flush(0.05)
            flush(); time.sleep(.01)
        self.assertTrue(self.engine._profile('metadata-account').is_file())

    def test_different_content_and_account_epoch_discard_old_response(self):
        self.engine.upload_time_for_ref('local-ref')
        self.entry.update(version=2,sha256='d'*64);self.reply()
        self.assertNotIn('uploaded_at',self.entry)
        self.engine.upload_time_for_ref('local-ref')
        self.engine._interrupt_cycle();self.reply(dict(self.result,version=2,sha256='d'*64))
        self.assertNotIn('uploaded_at',self.entry);self.assertFalse(self.engine._metadata_pending)

    def test_invalid_metadata_is_diagnostic_and_backed_off(self):
        errors=[];self.engine.status_changed.connect(errors.append)
        self.engine.upload_time_for_ref('local-ref');self.reply(dict(self.result,uploaded_at='not-a-time'))
        self.assertTrue(errors);self.assertNotIn('uploaded_at',self.entry)
        self.engine.upload_time_for_ref('local-ref');self.assertEqual(len(self.service.calls),1)

    def test_local_and_signed_out_items_never_request_an_upload_time(self):
        self.entry['version']=0
        self.assertIsNone(self.engine.upload_time_for_ref('local-ref'))
        self.entry['version']=1;self.service.signed_in=False
        self.assertIsNone(self.engine.upload_time_for_ref('local-ref'));self.assertFalse(self.service.calls)

    def test_ui_lookups_never_scan_the_journal_or_request_metadata(self):
        class NoScan(dict):
            def values(self):raise AssertionError('UI must not scan journal values')
            def items(self):raise AssertionError('UI must not scan journal items')
        self.entry.update(dirty=False,uploaded_at=self.result['uploaded_at'],uploaded_sha256=self.entry['sha256'])
        self.engine.state['entries']=NoScan(self.engine.state['entries'])
        self.assertEqual(self.engine.sync_statuses_for_refs(['local-ref']),{'local-ref':'synced'})
        self.assertEqual(self.engine.cached_upload_time_for_ref('local-ref'),self.result['uploaded_at'])
        self.assertEqual(self.engine.upload_metadata_for_refs(['local-ref'])['local-ref'][0],self.result['uploaded_at'])
        self.assertFalse(self.service.calls)
        self.engine._remove_entry(self.entry['id'])
        self.assertIsNone(self.engine.cached_upload_time_for_ref('local-ref'))
        self.assertNotIn('local-ref',self.engine._ref_index)


if __name__=='__main__':unittest.main()
