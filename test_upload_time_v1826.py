"""Upload metadata ownership, request coalescing and account/source fences."""
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


if __name__=='__main__':unittest.main()
