"""Actual TLS consent and two original native kernels, without desktop input."""
import hashlib
import os
from pathlib import Path
import tempfile
import time
import unittest

from PySide6.QtWidgets import QApplication
from syncthing_lan import SyncthingLanService
from test_syncthing_engine import BINARY

app=QApplication.instance() or QApplication([])


def until(predicate,timeout=30):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        app.processEvents()
        if predicate():return
        time.sleep(.005)
    raise TimeoutError('Original-engine LAN transfer did not finish')


@unittest.skipUnless(BINARY.is_file(),'Prepare the official kernel first')
class NativeLanTests(unittest.TestCase):
    def test_cancel_before_consent_does_not_publish_or_delete_originals(self):
        with tempfile.TemporaryDirectory(prefix='native-lan-cancel-') as directory:
            root=Path(directory);a=SyncthingLanService(root/'a',bind_host='127.0.0.1',discovery=False,binary=BINARY)
            b=SyncthingLanService(root/'b',bind_host='127.0.0.1',discovery=False,binary=BINARY)
            offers=[];finished=[];source=root/'original.bin';source.write_bytes(b'original bytes')
            b.offer_received.connect(offers.append);b.completed.connect(finished.append)
            try:
                a.start();b.start();until(lambda:a.port and b.port);a.remember_peer(b.own_peer())
                identifier=a.send(b.device_id,[source]);until(lambda:bool(offers));a.cancel(identifier)
                until(lambda:not a._transfers[identifier].active and b._transfers[identifier].cancel.is_set() and not b._transfers[identifier].active)
                self.assertEqual(source.read_bytes(),b'original bytes');self.assertEqual(finished,[])
                self.assertEqual(a.engine.request('GET','/rest/config/folders'),[])
                self.assertEqual(b.engine.request('GET','/rest/config/folders'),[])
                self.assertFalse((root/'a'/('send-'+identifier)).exists())
            finally:
                a.close();b.close();a._shutdown.join(15);b._shutdown.join(15)
                self.assertFalse(a._shutdown.is_alive());self.assertFalse(b._shutdown.is_alive())

    def test_both_directions_consent_collision_and_exact_original_bytes(self):
        with tempfile.TemporaryDirectory(prefix='native-lan-') as directory:
            root=Path(directory);a=SyncthingLanService(root/'a',device_name='A',bind_host='127.0.0.1',discovery=False,binary=BINARY)
            b=SyncthingLanService(root/'b',device_name='B',bind_host='127.0.0.1',discovery=False,binary=BINARY)
            finished=[];progress=[];offers=[]
            for service in (a,b):service.completed.connect(finished.append);service.progress.connect(progress.append)
            try:
                a.start();b.start();until(lambda:a.port and b.port)
                for index,(sender,receiver) in enumerate(((a,b),(b,a))):
                    source=root/('source-'+str(index));source.mkdir();original=source/'中文.bin'
                    data=os.urandom(6*1024*1024+17);original.write_bytes(data);(source/'zero').touch();(source/'empty').mkdir()
                    destination=root/('received-'+str(index));destination.mkdir();(destination/source.name).mkdir()
                    slot=lambda value:offers.append((receiver,value,destination))
                    receiver.offer_received.connect(slot)
                    # Discovery is deliberately disabled in this loopback test;
                    # supply its fresh announcement before each new selection.
                    sender.remember_peer(receiver.own_peer())
                    identifier=sender.send(receiver.device_id,[source]);until(lambda:len(offers)>index)
                    selected,offer,target=offers[index]
                    self.assertEqual(offer['id'],identifier);self.assertFalse(any(row['id']==identifier for row in finished))
                    self.assertTrue(selected.accept(identifier,target));until(lambda:sum(row['id']==identifier for row in finished)==2)
                    received=next(row for row in finished if row['id']==identifier and row['direction']=='receive')
                    output=Path(received['paths'][0]);self.assertEqual(output.name,source.name+' (2)')
                    self.assertEqual(hashlib.sha256((output/original.name).read_bytes()).digest(),hashlib.sha256(data).digest())
                    self.assertTrue((output/'zero').is_file());self.assertEqual((output/'zero').stat().st_size,0)
                    self.assertTrue((output/'empty').is_dir());self.assertEqual(original.read_bytes(),data)
                    until(lambda:not sender._transfers[identifier].active)
                    self.assertEqual(sender.engine.request('GET','/rest/config/folders'),[])
                    self.assertEqual(receiver.engine.request('GET','/rest/config/folders'),[])
                    self.assertFalse(any(row['id']==identifier and row.get('state')=='error' for row in progress))
                    self.assertTrue(all(row.get('route')=='lan' for row in progress if row['id']==identifier))
                    receiver.offer_received.disconnect(slot)
            finally:
                a.close();b.close();a._shutdown.join(15);b._shutdown.join(15)
                self.assertFalse(a._shutdown.is_alive());self.assertFalse(b._shutdown.is_alive())
                self.assertIsNone(a.engine.process);self.assertIsNone(b.engine.process)


if __name__=='__main__':unittest.main()
