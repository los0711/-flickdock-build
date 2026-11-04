"""Cloud route encryption and API ownership fences; all bytes stay in memory."""
import base64
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from cloud_pipe import CloudPipeStream,CloudPipeApi
from test_html_native_workspace import APP,until


class MemoryCloud:
    def __init__(self):self.blocks={};self.condition=threading.Condition();self.closed=False
    def call(self,method,path,cancel,**options):
        parts=path.split('/');position=tuple(parts[-2:])
        with self.condition:
            if len(parts)<7:
                if method=='DELETE':self.closed=True;self.condition.notify_all()
                return {}
            if method=='PUT':self.blocks[position]=options['raw'];self.condition.notify_all();return {}
            if method=='DELETE':return {}
            while position not in self.blocks:
                if cancel.is_set() or self.closed:raise ConnectionError('Closed')
                self.condition.wait(.02)
            return dict(ready=True,body=base64.b64encode(self.blocks[position]).decode('ascii'))


class CloudRouteTests(unittest.TestCase):
    def test_both_directions_preserve_original_bytes_and_store_only_ciphertext(self):
        api=MemoryCloud();descriptor=dict(id=uuid4().hex,key=os.urandom(32).hex())
        sender=CloudPipeStream(api,descriptor,True);receiver=CloudPipeStream(api,descriptor,False)
        try:
            body=os.urandom(3*1024*1024+47);sender.sendall(body);result=bytearray()
            deadline=time.perf_counter()+6
            while len(result)<len(body) and time.perf_counter()<deadline:
                try:result.extend(receiver.recv(65536))
                except TimeoutError:continue
            self.assertEqual(result,body)
            self.assertTrue(api.blocks);self.assertFalse(any(block[:128] in body for block in api.blocks.values()))
            receiver.sendall(b'reverse acknowledgement')
            self.assertEqual(sender.recv(64),b'reverse acknowledgement')
        finally:sender.shutdown();receiver.shutdown()

    def test_wrong_key_never_delivers_plaintext(self):
        api=MemoryCloud();descriptor=dict(id=uuid4().hex,key=os.urandom(32).hex())
        sender=CloudPipeStream(api,descriptor,True)
        receiver=CloudPipeStream(api,dict(descriptor,key=os.urandom(32).hex()),False)
        try:
            sender.sendall(b'private original')
            until(lambda:receiver.closed);self.assertEqual(receiver.recv(64),b'')
        finally:sender.shutdown();receiver.shutdown()

    def test_account_switch_rejects_late_http_result_and_cancels_only_own_request(self):
        callbacks=[];api=SimpleNamespace(token='first',request=Mock(side_effect=lambda *args,**options:callbacks.append(args[2]) or 'request-key'),cancel=Mock(),close=Mock())
        account=SimpleNamespace(signed_in=True,account=dict(id='account-a'),device_id='device-a',api=api)
        broker=CloudPipeApi(account,'0'*64,network_api=api);result=[]
        def request():
            try:broker.call('GET','/v1/wan/cloud/'+uuid4().hex+'/0/0',threading.Event())
            except OSError as error:result.append(error)
        worker=threading.Thread(target=request);worker.start()
        until(lambda:bool(callbacks));api.token='second';callbacks[0]({'ready':True},None)
        until(lambda:bool(result));worker.join(2);self.assertFalse(broker._calls)
        broker.close()


if __name__=='__main__':unittest.main()
