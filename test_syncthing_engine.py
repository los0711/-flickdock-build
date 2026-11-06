"""Actual official kernel, isolated profiles, loopback sockets and original bytes."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import subprocess
import sys
from uuid import uuid4

from syncthing_engine import SyncthingEngine,check_addresses
from manual_payload import prepare
from syncthing_jobs import ReceiveJob,deliver

BINARY=Path(os.environ.get('FLICKDOCK_QA_SYNCTHING',''))
if not BINARY.is_file():BINARY=Path(__file__).parent/'syncthing/runtime'/('syncthing.exe' if os.name=='nt' else 'syncthing')


class AddressTests(unittest.TestCase):
    def test_modes_do_not_accept_each_others_addresses_or_public_relays(self):
        self.assertEqual(check_addresses(['tcp://192.168.1.4:22000'],'lan'),['tcp://192.168.1.4:22000'])
        self.assertEqual(check_addresses(['dynamic'],'remote'),['dynamic'])
        for address,mode in [('tcp://192.168.1.4:22000','remote'),('tcp://1.1.1.1:22000','lan'),
                ('relay://1.1.1.1:443','remote'),('tcp://user:pass@192.168.1.4:22000','lan'),
                ('tcp://localhost:22000','lan'),('tcp://192.168.1.4:22000/other','lan')]:
            with self.subTest(address=address,mode=mode),self.assertRaises(ValueError):check_addresses([address],mode)


@unittest.skipUnless(BINARY.is_file(),'Official Syncthing binary is prepared by the native build gate')
class OfficialEngineTests(unittest.TestCase):
    def test_qt_quit_reaps_both_native_kernels_before_interpreter_exit(self):
        with tempfile.TemporaryDirectory(prefix='flickdock-native-exit-') as directory:
            root=Path(directory).resolve();report=root/'result.json'
            script='''import atexit,json,sys
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from syncthing_lan import SyncthingLanService
app=QApplication([]);root=Path(sys.argv[1]);binary=Path(sys.argv[2]);services=[];processes=[]
def check_exit():
 (root/'result.json').write_text(json.dumps(dict(ok=all(p.poll() is not None for p in processes),kernels=len(processes))))
atexit.register(check_exit)
try:
 for name in ('lan','remote'):
  service=SyncthingLanService(root/name,discovery=False,bind_host='127.0.0.1',binary=binary)
  services.append(service);service.engine.start();processes.append(service.engine.process);app.aboutToQuit.connect(service.close)
 QTimer.singleShot(50,app.quit);app.exec()
finally:
 for service in services:service.close()
'''
            result=subprocess.run([sys.executable,'-c',script,str(root),str(BINARY.resolve())],cwd=Path(__file__).parent,
                env=dict(os.environ,QT_QPA_PLATFORM='offscreen'),capture_output=True,text=True,timeout=45,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(json.loads(report.read_bytes()),dict(ok=True,kernels=2))
            for name in ('lan','remote'):
                reopened=SyncthingEngine(root/name/'kernel',mode='lan',loopback=True,binary=BINARY)
                try:self.assertTrue(reopened.start())
                finally:reopened.close()

    def test_finite_job_delivers_verified_snapshot_without_continuous_user_folder_sync(self):
        with tempfile.TemporaryDirectory(prefix='flickdock-finite-sync-') as root:
            base=Path(root);cancel=threading.Event();original=base/'original.bin'
            original.write_bytes(os.urandom(5*1024*1024+17));expected=original.read_bytes()
            snapshot=prepare([original],base/'snapshot',cancel)
            original.write_bytes(b'updated after snapshot')
            sender=SyncthingEngine(base/'sender',mode='lan',binary=BINARY,loopback=True)
            receiver=SyncthingEngine(base/'receiver',mode='lan',binary=BINARY,loopback=True)
            stage=base/'stage';stage.mkdir();network=stage/'network';network.mkdir()
            destination=base/'received';destination.mkdir();(destination/original.name).write_bytes(b'existing')
            job=None
            try:
                sender.start();receiver.start();task='fd-manual-'+uuid4().hex;progress=[]
                sender.share(task,snapshot['folder'],receiver.device_id,[receiver.local_address('127.0.0.1')],send=True)
                job=ReceiveJob(receiver,task,network,sender.device_id,[sender.local_address('127.0.0.1')],
                               snapshot['descriptor'],cancel=cancel,progress=lambda count,total:progress.append((count,total)))
                payload=job.receive();job.close()
                paths=deliver(payload,stage,destination,snapshot['descriptor'],snapshot,cancel)
                sender.unshare(task)
                self.assertEqual(Path(paths[0]).read_bytes(),expected)
                self.assertEqual((destination/original.name).read_bytes(),b'existing')
                self.assertEqual(original.read_bytes(),b'updated after snapshot')
                self.assertEqual(progress[-1],(snapshot['descriptor']['size'],snapshot['descriptor']['size']))
                self.assertEqual(receiver.request('GET','/rest/config/folders'),[])
            finally:
                if job is not None:job.close()
                sender.close();receiver.close()

    def test_original_bytes_and_unshare_without_touching_source_or_delivered_file(self):
        with tempfile.TemporaryDirectory(prefix='flickdock-native-sync-') as root:
            base=Path(root);sender=SyncthingEngine(base/'sender',mode='lan',binary=BINARY,loopback=True)
            receiver=SyncthingEngine(base/'receiver',mode='lan',binary=BINARY,loopback=True)
            try:
                sender.start();receiver.start()
                for engine in (sender,receiver):
                    config=engine.request('GET','/rest/config')
                    self.assertFalse(config['options']['relaysEnabled'])
                    self.assertFalse(config['options']['globalAnnounceEnabled'])
                    self.assertEqual(config['gui']['address'].split(':')[0],'127.0.0.1')
                source=base/'source';source.mkdir();target=base/'target';target.mkdir()
                payload=os.urandom(8*1024*1024+19);(source/'原文件.bin').write_bytes(payload)
                (source/'空文件夹').mkdir();(source/'zero.bin').touch()
                task='fd-manual-'+uuid4().hex
                sender.share(task,source,receiver.device_id,[receiver.local_address('127.0.0.1')],send=True)
                receiver.share(task,target,sender.device_id,[sender.local_address('127.0.0.1')],send=False)
                end=time.monotonic()+20;path=target/'原文件.bin'
                while time.monotonic()<end:
                    if path.is_file() and (target/'空文件夹').is_dir() and (target/'zero.bin').is_file():break
                    time.sleep(.025)
                self.assertTrue(path.is_file(),'Original bytes did not arrive')
                self.assertEqual(hashlib.sha256(path.read_bytes()).digest(),hashlib.sha256(payload).digest())
                self.assertTrue(any(row['connected'] for row in sender.connections().values()))
                for engine in (sender,receiver):engine.unshare(task)
                self.assertEqual((source/'原文件.bin').read_bytes(),payload)
                self.assertEqual(path.read_bytes(),payload)
                self.assertEqual(receiver.request('GET','/rest/config/folders'),[])
                processes=[sender.process,receiver.process]
            finally:sender.close();receiver.close()
            self.assertTrue(all(process.poll() is not None for process in processes))


if __name__=='__main__':unittest.main()
