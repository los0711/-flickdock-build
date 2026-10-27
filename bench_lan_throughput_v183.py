"""Offscreen loopback throughput probe; no discovery or desktop interaction."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import tempfile
import time

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtWidgets import QApplication
from lan_transfer import LanTransferService


def run(size_mib=128):
    app=QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory(prefix='flickdock-lan-throughput-') as folder:
        root=Path(folder);source=root/'original.bin';destination=root/'received';destination.mkdir()
        block=os.urandom(1024*1024)
        with source.open('wb') as output:
            for _ in range(size_mib):output.write(block)
        sender=LanTransferService(device_name='Loopback sender',bind_host='127.0.0.1',discovery=False)
        receiver=LanTransferService(device_name='Loopback receiver',bind_host='127.0.0.1',discovery=False)
        offers=[];received=[];sent=[];errors=[]
        receiver.offer_received.connect(offers.append)
        receiver.completed.connect(received.append);sender.completed.connect(sent.append)
        receiver.status_changed.connect(errors.append);sender.status_changed.connect(errors.append)
        connection={}
        def sample_connection(identity):
            transfer=sender._transfers.get(identity)
            stream=transfer.socket if transfer else None
            if not stream or connection:return
            try:
                connection.update(tls_version=stream.version(),tls_cipher=stream.cipher()[0],
                                  send_buffer_bytes=stream.getsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF),
                                  receive_buffer_bytes=stream.getsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF))
            except OSError:
                pass  # A very fast transfer may close before this sample.
        def wait(predicate,seconds=120,identity=None):
            deadline=time.monotonic()+seconds
            while time.monotonic()<deadline:
                app.processEvents()
                if identity:sample_connection(identity)
                if predicate():return
                time.sleep(.002)
            raise TimeoutError('Loopback transfer did not finish')
        try:
            sender.start();receiver.start();wait(lambda:sender.port and receiver.port,15)
            sender.remember_peer(receiver.own_peer());receiver.remember_peer(sender.own_peer())
            identity=sender.send(receiver.device_id,[source]);wait(lambda:offers,15)
            started=time.perf_counter();receiver.accept(identity,destination)
            wait(lambda:received and sent,120,identity)
            elapsed=time.perf_counter()-started
            actual=Path(received[0]['paths'][0])
            def digest(path):
                h=hashlib.sha256()
                with path.open('rb') as stream:
                    for data in iter(lambda:stream.read(1024*1024),b''):h.update(data)
                return h.hexdigest()
            if actual.parent!=destination or actual.stat().st_size!=size_mib*1024*1024 or digest(source)!=digest(actual):
                raise AssertionError('Received original bytes differ')
            return {'ok':True,'size_mib':size_mib,'seconds':round(elapsed,3),
                    'mib_per_second':round(size_mib/elapsed,2),'sha256_verified':True,
                    'path_direct':True,'transport':'TLS over 127.0.0.1',
                    'platform':platform.platform(),'python':platform.python_version(),
                    'socket':connection,'scope':'local cached-disk loopback; not Wi-Fi throughput'}
        finally:
            sender.close();receiver.close()
            for service in (sender,receiver):
                for thread in list(service._threads):thread.join(timeout=1.2)
            app.processEvents()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--size-mib',type=int,default=128)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    if not 1<=args.size_mib<=1024:raise SystemExit('size-mib must be between 1 and 1024')
    try:result=run(args.size_mib)
    except Exception as error:result={'ok':False,'error':str(error)[:200]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(result,ensure_ascii=False))
    raise SystemExit(0 if result['ok'] else 1)
