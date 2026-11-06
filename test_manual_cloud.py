"""Real Qt HTTP streams + workerd/D1/R2; no real desktop or clipboard access."""
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from collections import deque
from uuid import uuid4

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QObject,Signal
from cloud_api import CloudApi,CloudError
from manual_cloud import ManualCloud
from manual_payload import prepare
from syncthing_jobs import deliver

ROOT=Path(__file__).parent
NODE=ROOT/'p2p/runtime'/('node.exe' if os.name=='nt' else 'node')
if not NODE.is_file():NODE=Path(shutil.which('node') or 'node')
APP=QApplication.instance() or QApplication([])


def until(predicate,seconds=90):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        APP.processEvents()
        if predicate():return
        time.sleep(.005)
    raise TimeoutError('Manual R2 integration timed out')


class FixtureAccount(QObject):
    account_changed=Signal()
    devices_changed=Signal(list)
    def __init__(self,api,auth):
        super().__init__();self.api=api;self.device_id=auth['device_id'];self.account=auth['account'];self.signed_in=True


class LocalCloudFixture:
    @classmethod
    def setUpClass(cls):
        cls.server=subprocess.Popen([str(NODE),str(ROOT/'cloud/test/qa-server.mjs')],cwd=ROOT,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        cls.addClassCleanup(cls.stop_server)
        output=queue.Queue();diagnostics=deque(maxlen=24)
        def read():
            for line in cls.server.stdout:
                diagnostics.append(line.decode('utf8','replace')[:1024])
                if line.startswith(b'http://127.0.0.1:'):output.put(line.decode().strip());return
            output.put(None)
        threading.Thread(target=read,daemon=True).start()
        try:cls.origin=output.get(timeout=75)
        except queue.Empty as error:raise RuntimeError('Local Worker did not become ready: '+''.join(diagnostics)) from error
        if cls.origin is None:raise RuntimeError('Local Worker exited before readiness: '+''.join(diagnostics))
        cls.accounts=[]
        for name,login in [('manual-'+uuid4().hex,False),('',True),('outside-'+uuid4().hex,False)]:
            if login:name=cls.username
            else:cls.username=name
            api=CloudApi(cls.origin,allow_test_http=True)
            challenge=cls.call(api,'POST','/v1/auth/challenge',authenticated=False,data={'username':name,'purpose':'login' if login else 'register'})
            key=hashlib.scrypt(b'manual-r2-local-test',salt=bytes.fromhex(challenge['salt']),n=16384,r=8,p=5,dklen=32,maxmem=32*1024*1024).hex()
            auth=cls.call(api,'POST','/v1/auth/'+('login' if login else 'register'),authenticated=False,data=dict(username=name,
                challenge_id=challenge['challenge_id'],auth_key=key,device_name='fixture',platform='windows'))
            api.token=auth['token'];cls.accounts.append(FixtureAccount(api,auth))

    @classmethod
    def call(cls,api,method,path,**options):
        result=[];api.request(method,path,lambda value,error:result.append((value,error)),**options)
        until(lambda:bool(result));value,error=result[0]
        if error:raise error
        return value

    @classmethod
    def tearDownClass(cls):
        for account in cls.accounts:account.api.close()

    @classmethod
    def stop_server(cls):
        cls.server.communicate(b'stop' if cls.server.poll() is None else None,timeout=20)

    def worker(self,function):
        result=[]
        def run():
            try:result.append((function(),None))
            except BaseException as error:result.append((None,error))
        thread=threading.Thread(target=run);thread.start();until(lambda:bool(result),120);thread.join()
        value,error=result[0]
        if error:raise error
        return value


class ManualCloudTests(LocalCloudFixture,unittest.TestCase):
    def test_actual_replaced_copy_resolves_identical_committed_body_without_false_failure(self):
        from clipboard_sync import ClipboardSync,inline_text
        from test_clipboard_sync_v1819 import Clipboard,Events
        from unittest.mock import patch
        text='verified real Worker replacement fixture';item=inline_text(('text',dict(text=text,html='')))
        with tempfile.TemporaryDirectory(prefix='actual-clipboard-conflict-') as directory:
            clipboard=Clipboard();client=ClipboardSync(self.accounts[0],Events(),Path(directory),clipboard=clipboard)
            results=[];client.copy_result.connect(lambda ok,context:results.append((ok,context)))
            try:
                APP.processEvents();until(lambda:not client._fetching)
                client.poll.stop()
                winner=self.call(self.accounts[1].api,'POST','/v1/clipboard',data=dict(id=str(uuid4()),protocol=2,
                    sequence=1,copied_at=round(time.time()*1000)+1000,inline=item['inline']))['clipboard']
                with patch.object(client,'_resolve_replaced',wraps=client._resolve_replaced) as resolve:
                    clipboard.text(text);client._changed()
                    until(lambda:client._pending is None and client.view['phase']=='synced')
                    self.assertEqual(resolve.call_count,1)
                self.assertEqual(client.view['id'],winner['id']);self.assertTrue(client.view['backed_up'])
                self.assertEqual([ok for ok,context in results],[True]);self.assertEqual(clipboard.mimeData().text(),text)
                until(lambda:not client.deliveries.inflight)
                current=self.call(self.accounts[0].api,'GET','/v1/clipboard')
                self.assertEqual(current['clipboard']['id'],winner['id'])
                self.assertEqual(current['delivery']['receipts'].get(self.accounts[0].device_id),'cloud')
            finally:
                client.close();until(lambda:not client._jobs)

    def test_verified_transfer_is_hidden_from_shelf_and_preserves_originals(self):
        with tempfile.TemporaryDirectory(prefix='manual-r2-') as directory:
            root=Path(directory).resolve();source=root/'original.bin';source.write_bytes(bytes(range(256))*32768)
            destination=root/'received';destination.mkdir();(destination/source.name).write_bytes(b'existing')
            cancel=threading.Event();payload=prepare([source],root/'snapshot',cancel)
            sender=ManualCloud(self.accounts[0]);receiver=ManualCloud(self.accounts[1]);progress=[]
            record=self.worker(lambda:sender.upload(payload,cancel,lambda done,total:progress.append((done,total))))
            try:
                self.assertEqual(record['kind'],'transfer');self.assertTrue(progress)
                for account in self.accounts[:2]:
                    feed=self.call(account.api,'GET','/v1/sync/changes?cursor=0&limit=1')
                    self.assertEqual(feed['changes'],[]);self.assertEqual(feed['transfers'],[])
                    self.assertGreater(int(feed['cursor']),0)
                with self.assertRaises(CloudError) as denied:
                    self.call(self.accounts[2].api,'GET',f'/v1/items/{record["id"]}/content')
                self.assertEqual(denied.exception.status,404)
                path=self.worker(lambda:receiver.download(record,root/'download',cancel,lambda done,total:None))
                paths=self.worker(lambda:deliver(path,root,destination,payload['descriptor'],dict(total=payload['total'],count=payload['count'],
                    names=payload['names'][:12],digest=hashlib.sha256(json.dumps(payload['entries'],sort_keys=True).encode()).hexdigest()),cancel))
                self.assertEqual(len(paths),1);self.assertEqual((destination/'original (2).bin').read_bytes(),source.read_bytes())
                self.assertEqual((destination/source.name).read_bytes(),b'existing')
            finally:
                self.worker(lambda:sender.retire(record));sender.close();receiver.close()
            with self.assertRaises(CloudError):self.call(self.accounts[1].api,'GET',f'/v1/items/{record["id"]}/content')
            self.assertTrue(source.is_file())


if __name__=='__main__':unittest.main()
