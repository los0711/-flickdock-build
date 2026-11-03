"""Hosted-only real file protocol over native host ICE, without TURN/discovery."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import threading
import time
from uuid import uuid4


def run(size_mib):
    if (sys.platform!='win32' or os.environ.get('GITHUB_ACTIONS')!='true'
            or os.environ.get('RUNNER_ENVIRONMENT')!='github-hosted'):
        raise RuntimeError('Native benchmark requires an ephemeral hosted Windows runner')
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
    from PySide6.QtWidgets import QApplication
    from lan_transfer import LanTransferService,_Transfer
    from webrtc_stream import WebRTCStream,close_owned_streams
    app=QApplication.instance() or QApplication([])
    peers=[];closing=threading.Event();errors=[];events=[]
    def signal(index,event):
        if closing.is_set():return
        kind=event.get('type')
        if kind in ('candidate','description'):
            try:peers[1-index].control(event)
            except OSError as error:errors.append(type(error).__name__)
        elif kind in ('error','closed'):
            events.append(dict(peer=index,type=kind))
    services=[];thread=None;temporary=None
    try:
        peers.extend((WebRTCStream(lambda event:signal(0,event)),WebRTCStream(lambda event:signal(1,event))))
        connected=time.perf_counter()
        peers[1].control(dict(type='init',iceServers=[],initiator=False))
        peers[0].control(dict(type='init',iceServers=[],initiator=True))
        if not all(peer.opened.wait(12) for peer in peers):raise TimeoutError('Native host ICE did not connect')
        connection_seconds=time.perf_counter()-connected
        if errors or any(peer.route!='direct' for peer in peers):raise AssertionError('Not a healthy direct host ICE pair')
        temporary=tempfile.TemporaryDirectory(prefix='owned-native-throughput-')
        root=Path(temporary.name);source=root/'original.bin';destination=root/'received';destination.mkdir()
        block=os.urandom(1024*1024);expected=hashlib.sha256()
        with source.open('wb') as output:
            for _ in range(size_mib):output.write(block);expected.update(block)
        sender=LanTransferService(discovery=False);receiver=LanTransferService(discovery=False)
        services.extend((sender,receiver));sender.mode=receiver.mode='wan'
        sender._connect_peer=lambda *args:peers[0];sender._stream_code=lambda *args:''
        receiver._incoming_stream=lambda stream:stream;receiver._stream_code=lambda *args:''
        def accept(transfer,*args):transfer.destination=destination;transfer.accepted.set()
        receiver._incoming_offer=accept
        total=size_mib*1024*1024;samples=[];notify=receiver._notify;started=time.perf_counter()
        def observe(transfer,state,**values):
            count=transfer.done
            if count>0 and (not samples or count>samples[-1]['received']):
                samples.append(dict(received=count,seconds=time.perf_counter()-started))
            notify(transfer,state,**values)
        receiver._notify=observe
        transfer=_Transfer(uuid4().hex,'send','owned-native-peer',active=True)
        receiver._slots.acquire()
        thread=threading.Thread(target=receiver._incoming,args=(peers[1],'owned-native-peer'),daemon=True)
        thread.start();sender._outgoing(transfer,{},[source]);thread.join(15)
        seconds=time.perf_counter()-started
        if not transfer.finished or thread.is_alive() or errors:raise AssertionError('Native file protocol did not finish')
        target=destination/source.name
        with target.open('rb') as incoming:actual=hashlib.file_digest(incoming,'sha256').hexdigest()
        if target.stat().st_size!=total or actual!=expected.hexdigest():raise AssertionError('Original file SHA256 differs')
        if not samples or samples[-1]['received']!=total:raise AssertionError('Missing complete real file receive progress')
        return dict(ok=True,size_mib=size_mib,seconds=seconds,mib_per_second=size_mib/seconds,
            connection_seconds=connection_seconds,receive_samples=samples,
            sha256_verified=True,native_events=events,route='host ICE / native ordered WebRTC',
            platform=platform.platform(),python=platform.python_version(),
            scope='Same-host real file protocol including manifests, acknowledgements and disk verification; not public P2P, WiFi or physical devices.')
    finally:
        closing.set()
        for service in services:service.close()
        for peer in peers:peer.close(abort=True)
        if thread is not None:thread.join(3)
        if not close_owned_streams(timeout=5):raise RuntimeError('Owned native bridge did not exit')
        for service in services:service.deleteLater()
        app.processEvents()
        if temporary is not None:temporary.cleanup()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--size-mib',type=int,default=128)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    if not 8<=args.size_mib<=512:raise ValueError('Probe size must be 8 through512 MiB')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    try:result=run(args.size_mib)
    except Exception as error:
        result=dict(ok=False,error_type=type(error).__name__,error=str(error))
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))
    raise SystemExit(0 if result['ok'] else 1)
