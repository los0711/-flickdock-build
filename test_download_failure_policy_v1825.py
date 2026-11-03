"""Permanent item failures must not re-request data on change hints or idle ticks."""
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from PySide6.QtCore import QTimer
from cloud_api import CloudError
from shelf_sync import ShelfSync
from test_sync_receive_priority import APP,Api,Service,Adapter,file_record,spin


class FaultApi(Api):
    def __init__(self,records,blobs,failed,error):
        super().__init__(records,blobs)
        self.failed=failed;self.error=error;self.attempts=0

    def request(self,method,path,callback,**kwargs):
        if '/parts/' in path and path.split('/')[3]==self.failed and self.error:
            self.attempts+=1;key=uuid4().hex;self.pending[key]=True
            def complete():
                if self.pending.pop(key,False):callback(None,self.error)
            QTimer.singleShot(0,complete)
            return key
        return super().request(method,path,callback,**kwargs)


class DownloadFailureTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(prefix='download-failure-policy-')
        self.first,self.second=uuid4().hex,uuid4().hex
        self.blobs={self.first:b'first original bytes',self.second:b'next independent item'}
        self.records=[dict(cursor=str(index+1),record=file_record(identity,name,self.blobs[identity]))
            for index,(identity,name) in enumerate(((self.first,'first.bin'),(self.second,'second.bin')))]
        self.adapter=Adapter();self.service=Service([],{});self.engine=None

    def tearDown(self):
        if self.engine:
            self.engine.close();self.engine.pool.waitForDone(3000);self.engine.deleteLater()
        self.service.deleteLater();APP.processEvents();self.directory.cleanup()

    def start(self,error):
        self.api=FaultApi(self.records,self.blobs,self.first,error);self.service.api=self.api
        self.engine=ShelfSync(self.directory.name,self.service,self.adapter,poll_ms=600000)
        self.statuses=[];self.transfers=[]
        self.engine.status_changed.connect(self.statuses.append)
        self.engine.transfers_changed.connect(self.transfers.append)
        self.service.account_changed.emit({'id':'failure-policy-account'})
        self.assertTrue(spin(lambda:self.engine.state['cursor']=='2' and not self.engine.running))

    def test_save_failure_stops_requests_keeps_reason_and_does_not_block_next_item(self):
        self.start(CloudError('Cannot save original',code='DOWNLOAD_SAVE_FAILED'))
        self.assertEqual([Path(ref).name for ref in self.adapter.view['permanent']],['second.bin'])
        deferred=self.engine.state['deferred_downloads'][self.first]
        self.assertTrue(deferred.get('blocked'),deferred)
        self.assertIn('Cannot save original',deferred['last_error'])
        self.assertFalse(self.engine.debounce.isActive(),'A permanent item must not own a retry timer')
        for _ in range(3):
            self.engine.request_refresh()
            self.assertTrue(spin(lambda:not self.engine.running and not self.api.pending))
        self.assertEqual(self.api.attempts,1)
        failure=next(row for row in self.transfers[-1] if row['id']==self.first)
        self.assertEqual(failure['phase'],'error')
        self.assertFalse(any('等待重试' in text for text in self.statuses[-2:]))

    def test_manual_retry_clears_only_failed_item_and_downloads_original_bytes(self):
        self.start(CloudError('Denied',code='DOWNLOAD_SAVE_FAILED'))
        self.api.error=None
        self.assertTrue(self.engine.retry_download(self.first))
        self.assertTrue(spin(lambda:len(self.adapter.view['permanent'])==2 and not self.engine.running))
        self.assertNotIn(self.first,self.engine.state['deferred_downloads'])
        actual={Path(ref).name:Path(ref).read_bytes() for ref in self.adapter.view['permanent']}
        self.assertEqual(actual,{'first.bin':self.blobs[self.first],'second.bin':self.blobs[self.second]})
        self.assertFalse(self.engine.retry_download(self.first))

    def test_transient_timeout_keeps_retry_and_recovers_same_version(self):
        self.start(CloudError('Temporary timeout'))
        self.assertFalse(self.engine.state['deferred_downloads'][self.first].get('blocked'))
        self.assertTrue(self.engine.debounce.isActive())
        self.api.error=None
        self.engine.state['deferred_downloads'][self.first]['retry_at']=0
        self.engine.request_refresh()
        self.assertTrue(spin(lambda:len(self.adapter.view['permanent'])==2 and not self.engine.running))

    def test_new_revision_releases_permanent_failure_without_replaying_old_one(self):
        self.start(CloudError('Invalid object',422,'INVALID_REQUEST'))
        self.api.error=None
        self.api.blobs[self.first]=b'new verified revision'
        self.api.records[0]['record']=dict(file_record(self.first,'first.bin',self.api.blobs[self.first]),version=2)
        self.engine.request_refresh()
        self.assertTrue(spin(lambda:len(self.adapter.view['permanent'])==2 and not self.engine.running))
        entry=self.engine.state['entries'][self.first]
        self.assertEqual(entry['version'],2)
        self.assertEqual(Path(entry['ref']).read_bytes(),b'new verified revision')


if __name__=='__main__':unittest.main()
