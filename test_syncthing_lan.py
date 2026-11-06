"""Actual TLS consent and two original native kernels, without desktop input."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import socket
import time
import threading
import unittest
from unittest.mock import Mock,patch

from PySide6.QtWidgets import QApplication
from syncthing_lan import SyncthingLanService
from test_syncthing_engine import BINARY
from lan_transfer import _Transfer,_send_json,PROTOCOL
from syncthing_engine import EngineError

app=QApplication.instance() or QApplication([])


def until(predicate,timeout=30):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        app.processEvents()
        if predicate():return
        time.sleep(.005)
    raise TimeoutError('Original-engine LAN transfer did not finish')


class CleanupBoundaryTests(unittest.TestCase):
    def test_busy_stage_cleanup_retries_without_reverting_a_saved_transfer(self):
        with tempfile.TemporaryDirectory(prefix='native-lan-busy-') as directory:
            root=Path(directory).resolve();target=root/'target';target.mkdir();original=target/'original.bin';original.write_bytes(b'saved')
            service=SyncthingLanService(root/'service',bind_host='127.0.0.1',discovery=False)
            transfer=_Transfer('a'*32,'receive','peer',finished=True);transfer.destination=target;service._transfers[transfer.transfer_id]=transfer
            stage=service._stage(transfer,target,'.flickdock-receiving-'+transfer.transfer_id);(stage/'temporary.bin').write_bytes(b'owned')
            try:
                with patch('syncthing_lan.shutil.rmtree',side_effect=PermissionError('Native kernel still owns a handle')):
                    self.assertFalse(service._cleanup(transfer))
                self.assertTrue(transfer.finished);self.assertTrue(stage.is_dir());self.assertEqual(original.read_bytes(),b'saved')
                service._cleanup_due[transfer.transfer_id]=0;service._expire()
                self.assertIsNone(transfer.stage);self.assertFalse(stage.exists());self.assertEqual(service._owned_stages,{})
                self.assertEqual(original.read_bytes(),b'saved')
            finally:service.close();service._shutdown.join(5)

    def test_control_socket_closes_when_kernel_job_cleanup_fails(self):
        with tempfile.TemporaryDirectory(prefix='native-lan-cleanup-') as directory:
            root=Path(directory).resolve();target=root/'target';target.mkdir()
            service=SyncthingLanService(root/'service',bind_host='127.0.0.1',discovery=False)
            client,server=socket.socketpair();identifier='a'*32;token='b'*64
            entries=[dict(path='file.bin',kind='file',size=17)]
            native=dict(transport='syncthing-v3',device_id='-'.join(['AAAAAAA']*8),port=22000)
            transfer=_Transfer(identifier,'receive','peer',entries=entries,token=token,
                manifest_hash=hashlib.sha256(json.dumps(entries,sort_keys=True).encode()).hexdigest(),total=17)
            transfer.destination=target;transfer.accepted.set();service._transfers[identifier]=transfer;service._native_specs[identifier]=native
            service._slots.acquire();service._incoming_stream=lambda raw:raw
            service.engine.pair=Mock()
            job=Mock();job.receive.side_effect=EngineError('read failed');job.close.side_effect=EngineError('cleanup failed')
            _send_json(client,dict(protocol=PROTOCOL,id=identifier,token=token,entries=entries,native=native))
            _send_json(client,dict(state='content',content=dict(size=100,sha256='c'*64)))
            try:
                with patch('syncthing_lan.ReceiveJob',return_value=job),self.assertRaisesRegex(EngineError,'cleanup failed'):
                    service._incoming(server,'127.0.0.1')
                self.assertEqual(server.fileno(),-1);self.assertFalse(transfer.active)
                self.assertTrue(transfer.receiver_idle.is_set());self.assertEqual(list(target.iterdir()),[])
            finally:
                client.close();server.close();service.close();service._shutdown.join(5)


@unittest.skipUnless(BINARY.is_file(),'Prepare the official kernel first')
class NativeLanTests(unittest.TestCase):
    def test_consent_and_original_engine_connection_precede_snapshot_disk_io(self):
        with tempfile.TemporaryDirectory(prefix='native-lan-early-') as directory:
            root=Path(directory).resolve();a=SyncthingLanService(root/'a',bind_host='127.0.0.1',discovery=False,binary=BINARY)
            b=SyncthingLanService(root/'b',bind_host='127.0.0.1',discovery=False,binary=BINARY)
            source=root/'original.bin';source.write_bytes(bytes(range(256))*32768);target=root/'received';target.mkdir()
            entered=threading.Event();release=threading.Event();offers=[];finished=[]
            b.offer_received.connect(offers.append)
            for service in (a,b):service.completed.connect(finished.append)
            from manual_payload import prepare as snapshot
            def held(*args,**options):
                entered.set()
                if not release.wait(25):raise TimeoutError('QA snapshot was not released')
                return snapshot(*args,**options)
            try:
                a.start();b.start();until(lambda:a.port and b.port);a.remember_peer(b.own_peer())
                with patch('syncthing_lan.prepare',side_effect=held):
                    identifier=a.send(b.device_id,[source]);until(lambda:bool(offers),3)
                    self.assertFalse(entered.is_set());b.accept(identifier,target);until(entered.is_set,3)
                    until(lambda:a.engine.connections().get(b.engine.device_id,{}).get('connected') is True,10)
                    self.assertEqual(list(target.iterdir()),[]);release.set()
                    until(lambda:sum(row['id']==identifier for row in finished)==2)
                self.assertEqual((target/source.name).read_bytes(),source.read_bytes())
            finally:
                release.set();a.close();b.close();a._shutdown.join(15);b._shutdown.join(15)

    def test_cancel_before_consent_does_not_publish_or_delete_originals(self):
        with tempfile.TemporaryDirectory(prefix='native-lan-cancel-') as directory:
            root=Path(directory).resolve();a=SyncthingLanService(root/'a',bind_host='127.0.0.1',discovery=False,binary=BINARY)
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
            root=Path(directory).resolve();a=SyncthingLanService(root/'a',device_name='A',bind_host='127.0.0.1',discovery=False,binary=BINARY)
            b=SyncthingLanService(root/'b',device_name='B',bind_host='127.0.0.1',discovery=False,binary=BINARY)
            finished=[];progress=[];offers=[]
            for service in (a,b):service.completed.connect(finished.append);service.progress.connect(progress.append)
            try:
                a.start();b.start();until(lambda:a.port and b.port)
                for index,(sender,receiver) in enumerate(((a,b),(b,a))*3):
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
                    self.assertTrue(selected.accept(identifier,target))
                    try:until(lambda:sum(row['id']==identifier for row in finished)==2)
                    except TimeoutError as error:
                        diagnostic=dict(round=index,progress=progress,finished=finished,kernels=[])
                        for owner in (a,b):
                            status=owner.engine.request('GET','/rest/system/status')
                            diagnostic['kernels'].append(dict(connections=owner.engine.connections(),
                                dial=status.get('lastDialStatus'),listeners=status.get('connectionServiceStatus'),
                                folders=owner.engine.request('GET','/rest/config/folders')))
                        raise AssertionError(json.dumps(diagnostic,ensure_ascii=False)) from error
                    received=next(row for row in finished if row['id']==identifier and row['direction']=='receive')
                    output=Path(received['paths'][0]);self.assertEqual(output.name,source.name+' (2)')
                    self.assertEqual(hashlib.sha256((output/original.name).read_bytes()).digest(),hashlib.sha256(data).digest())
                    self.assertTrue((output/'zero').is_file());self.assertEqual((output/'zero').stat().st_size,0)
                    self.assertTrue((output/'empty').is_dir());self.assertEqual(original.read_bytes(),data)
                    until(lambda:not sender._transfers[identifier].active)
                    self.assertEqual(sender.engine.request('GET','/rest/config/folders'),[])
                    self.assertEqual(receiver.engine.request('GET','/rest/config/folders'),[])
                    identities={sender.engine.device_id,receiver.engine.device_id}
                    for owner in (sender,receiver):
                        self.assertEqual({row['deviceID'] for row in owner.engine.request('GET','/rest/config/devices')},identities)
                    self.assertFalse(any(row['id']==identifier and row.get('state')=='error' for row in progress))
                    self.assertTrue(all(row.get('route')=='lan' for row in progress if row['id']==identifier))
                    receiver.offer_received.disconnect(slot)
            finally:
                a.close();b.close();a._shutdown.join(15);b._shutdown.join(15)
                self.assertFalse(a._shutdown.is_alive());self.assertFalse(b._shutdown.is_alive())
                self.assertIsNone(a.engine.process);self.assertIsNone(b.engine.process)


if __name__=='__main__':unittest.main()
