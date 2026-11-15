"""Disposable TLS loopback small-file benchmark, not a real-network result."""
import argparse
import hashlib
import inspect
import json
import os
from pathlib import Path
import tempfile
import time

os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtWidgets import QApplication
from lan_transfer import LanTransferService


def run(count=300,size_kib=32):
    app=QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory(prefix='flickdock-lan-small-') as folder:
        root=Path(folder);source=root/'source';destination=root/'received'
        source.mkdir();destination.mkdir()
        content=os.urandom(size_kib*1024);expected=hashlib.sha256(content).digest()
        for index in range(count):(source/f'{index:04d}.bin').write_bytes(content)
        supports_diag='diagnostics_base' in inspect.signature(LanTransferService).parameters
        sender=LanTransferService(device_name='Loopback sender',bind_host='127.0.0.1',discovery=False,**({'diagnostics_base':root/'sender'} if supports_diag else {}))
        receiver=LanTransferService(device_name='Loopback receiver',bind_host='127.0.0.1',discovery=False,**({'diagnostics_base':root/'receiver'} if supports_diag else {}))
        offers=[];sent=[];received=[]
        receiver.offer_received.connect(offers.append);sender.completed.connect(sent.append);receiver.completed.connect(received.append)
        def wait(predicate,timeout=120):
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                app.processEvents()
                if predicate():return
                time.sleep(.002)
            raise TimeoutError('Loopback transfer did not finish')
        try:
            sender.start();receiver.start();wait(lambda:sender.port and receiver.port,15)
            sender.remember_peer(receiver.own_peer());receiver.remember_peer(sender.own_peer())
            identifier=sender.send(receiver.device_id,[source]);wait(lambda:offers,15)
            started=time.perf_counter();receiver.accept(identifier,destination)
            wait(lambda:sent and received);seconds=time.perf_counter()-started
            files=list((destination/source.name).glob('*.bin'))
            if len(files)!=count or any(hashlib.sha256(path.read_bytes()).digest()!=expected for path in files):
                raise AssertionError('Received bytes differ')
            return dict(ok=True,files=count,file_bytes=len(content),seconds=round(seconds,3),files_per_second=round(count/seconds,2),
                        sha256_verified=True,scope='local cached-disk TLS loopback; not Wi-Fi throughput')
        finally:
            sender.close();receiver.close()
            for service in (sender,receiver):
                for thread in list(service._threads):thread.join(timeout=1.2)
            app.processEvents()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--count',type=int,default=300);parser.add_argument('--size-kib',type=int,default=32)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    if not 1<=args.count<=10000 or not 1<=args.size_kib<=1024:raise SystemExit('invalid file count or size')
    try:result=run(args.count,args.size_kib)
    except Exception as error:result=dict(ok=False,error=str(error)[:200])
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(result,ensure_ascii=False))
    raise SystemExit(0 if result['ok'] else 1)
