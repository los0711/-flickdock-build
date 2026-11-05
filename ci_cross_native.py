"""Two real operating systems/networks; synthetic bytes and isolated clipboards."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import re

os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtCore import QMimeData,QTimer,QUrl
from PySide6.QtWidgets import QApplication
from account_service import AccountService
from cloud_events import CloudEvents
from clipboard_sync import ClipboardSync
from peer_sync import PeerSync
from peer_clipboard import ClipboardPeerBridge
from wan_transfer import WanTransferService
from app_version import APP_VERSION


class Store:
    def load(self):return None
    def save(self,value):pass
    def clear(self):pass


def main():
    if os.environ.get('FD_QA_CROSS_NATIVE')!='1':raise RuntimeError('Explicit isolated cross-platform QA is required')
    parser=argparse.ArgumentParser();parser.add_argument('--role',choices=('source','receiver'),required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    secret=json.loads(os.environ.pop('FLICKDOCK_QA_SYNC_JSON'))
    if set(secret)!={'username','password','probe'}:raise ValueError('Invalid isolated QA credentials')
    if not isinstance(secret['username'],str) or not secret['username'].startswith('cross-qa-') or not re.fullmatch('[a-f0-9]{32}',str(secret['probe'])):
        raise ValueError('Only disposable QA accounts are allowed')
    app=QApplication.instance() or QApplication([]);app.setQuitOnLastWindowClosed(False)
    temporary=tempfile.TemporaryDirectory(prefix='flickdock-cross-native-');root=Path(temporary.name)
    account=AccountService(root/'account',store=Store());events=CloudEvents(account)
    wan=WanTransferService(root/'wan',device_name='Cross platform QA '+args.role);wan.attach_account(account)
    mesh=PeerSync(account,wan)
    from PySide6.QtCore import QObject,Signal
    class Clipboard(QObject):
        dataChanged=Signal()
        def __init__(self):super().__init__();self.mime=QMimeData()
        def mimeData(self):return self.mime
        def setMimeData(self,value):self.mime=value;self.dataChanged.emit()
        def clear(self):self.setMimeData(QMimeData())
    clip=Clipboard();sync=ClipboardSync(account,events,root/'clipboard',clipboard=clip)
    sync.peer_bridge=ClipboardPeerBridge(sync,mesh)
    seed=hashlib.sha256(secret['probe'].encode()).digest();body=seed*(1024*1024//len(seed))
    source=root/'native-32MiB.bin'
    with source.open('wb') as stream:
        for _ in range(32):stream.write(body)
    with source.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
    small=root/'clipboard-512KiB.bin';small.write_bytes(body[:512*1024])
    text='FlickDock cross platform text '+secret['probe'];cleanup='FlickDock cross platform complete '+secret['probe']
    record=dict(id='cross-'+secret['probe'],size=source.stat().st_size,sha256=digest)
    report=dict(ok=False,version=APP_VERSION,source_commit=os.environ.get('FD_QA_SOURCE_COMMIT',os.environ.get('GITHUB_SHA','')),
                role=args.role,platform=sys.platform,isolated_clipboards=True,os_credentials_written=False,
                native_candidates={},routes=[],verified_bytes=0,text_received=False,file_received=False,pulses=0)
    counters=Counter();phase='discover';finished=False;job=None;peer_seen=None;stable_at=None;last_pulse=0
    started=time.monotonic();published=False;receiver_finished_at=None;transfer_started=None

    def finish(error=''):
        nonlocal finished
        if finished:return
        finished=True;timer.stop();deadline.stop()
        report.update(ok=not error,error=error,seconds=round(time.monotonic()-started,3),native_candidates=dict(counters))
        report.update(authenticated=account.signed_in,signaling_connected=wan._connected,
                      account_bound=wan._bound_identity is not None,phase=phase)
        sync.close();mesh.close();wan.close();events.close();account.close()
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,indent=2),encoding='utf8')
        print(json.dumps({key:report[key] for key in ('ok','role','platform','error','verified_bytes','text_received','file_received','pulses','seconds')}),flush=True)
        app.exit(1 if error else 0)

    def candidate(direction,value):
        candidate=value.get('candidate','')
        fields=candidate.split()
        if 'typ' in fields:
            index=fields.index('typ')+1
            if index<len(fields) and fields[index] in ('host','srflx','prflx','relay'):counters[direction+'_'+fields[index]]+=1

    def native(value):
        event=value.get('event',{})
        if event.get('type')=='candidate':candidate('local',event)
        if event.get('type')=='state':counters['state_'+str(event.get('state'))]+=1
    mesh._native.connect(native)
    wan.sync_signal.connect(lambda value:candidate('remote',value.get('payload',{})) if value.get('type')=='sync_signal' else None)

    def offered(topic,value,peer):
        nonlocal job,phase,transfer_started
        if args.role!='receiver' or topic!='shelf':return
        if value.get('id')==record['id'] and job is None:
            transfer_started=time.monotonic()
            if value.get('sha256')!=digest or value.get('size')!=record['size']:finish('INVALID_QA_OFFER');return
            def downloaded(path,error):
                nonlocal phase
                if error:finish('NATIVE_FILE_'+type(error).__name__.upper());return
                with Path(path).open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
                if actual!=digest or job.route!='p2p':finish('NATIVE_FILE_HASH_OR_ROUTE');return
                report.update(verified_bytes=record['size'],sha256=actual)
                report['file_seconds']=round(time.monotonic()-transfer_started,3)
                report['file_mib_per_second']=round(record['size']/1024**2/max(.001,time.monotonic()-transfer_started),3)
                mesh.acknowledge(peer,'shelf',dict(value,delivery_route=job.route));phase='text'
            job=mesh.fetch('shelf',value,root/'received.bin',downloaded,preferred=peer)
            if job is None:finish('NATIVE_FILE_NO_SOURCE')
        elif value.get('id','').startswith('pulse-'+secret['probe']):
            def pulsed(path,error):
                if error:finish('NATIVE_PULSE_FAILED');return
                report['pulses']+=1
                mesh.acknowledge(peer,'shelf',value)
            mesh.fetch('shelf',value,root/'pulse.bin',pulsed,preferred=peer)
    mesh.offered.connect(offered)

    def acknowledged(topic,value,peer):
        nonlocal phase,stable_at
        if args.role!='source':return
        if topic=='shelf' and value.get('id')==record['id']:
            report.update(verified_bytes=record['size'],sha256=digest);phase='text'
            mime=QMimeData();mime.setText(text);clip.setMimeData(mime)
        elif topic=='clipboard' and value.get('id')==sync.view.get('id'):
            if phase=='text':
                report['text_received']=True;phase='file';mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(small))]);clip.setMimeData(mime)
            elif phase=='file':report['file_received']=True;phase='stable';stable_at=time.monotonic()
            elif phase=='cleanup':phase='cleanup_delivered'
        elif topic=='shelf' and value.get('id','').startswith('pulse-'):report['pulses']+=1
    mesh.applied.connect(acknowledged)

    def poll():
        nonlocal phase,published,peer_seen,last_pulse,receiver_finished_at
        try:
            if args.role=='receiver':
                value=sync.view.get('value')
                if value and value[0]=='text' and value[1]['text']==cleanup:
                    if not report['file_received'] or not report['text_received'] or report['verified_bytes']!=record['size']:
                        finish('INCOMPLETE_CROSS_PLATFORM_PROOF')
                    else:
                        if receiver_finished_at is None:receiver_finished_at=time.monotonic()
                        if time.monotonic()-receiver_finished_at>=.5:
                            report['stable_seconds']=round(time.monotonic()-(peer_seen or started),3);finish()
                    return
            elif phase in ('cleanup','cleanup_delivered'):
                if phase=='cleanup_delivered' and sync.view.get('backed_up'):finish()
                return
            available=mesh.available()
            if any(row.get('account_device') and row.get('online') for row in wan.peers()) and peer_seen is None:peer_seen=time.monotonic()
            for state in available:
                route=state['rpc'].stream.route
                if not report['routes'] or report['routes'][-1]!=route:report['routes'].append(route)
                if route!='p2p':finish('DIRECT_P2P_UNAVAILABLE_'+route.upper());return
            if published and not available and phase not in ('cleanup','cleanup_delivered'):finish('ESTABLISHED_NATIVE_LINK_DROPPED');return
            if available and not published:
                report['connection_seconds']=round(time.monotonic()-(peer_seen or started),3);published=True
                if args.role=='source':mesh.publish('shelf',record,source)
            if args.role=='receiver':
                value=sync.view.get('value')
                if value and value[0]=='text' and value[1]['text']==text:report['text_received']=True
                if value and value[0]=='files':
                    if Path(value[1][0]).read_bytes()!=small.read_bytes():finish('CLIPBOARD_FILE_HASH_MISMATCH');return
                    report['file_received']=True
            elif phase=='stable':
                now=time.monotonic()
                if now-last_pulse>=5:
                    payload=str(report['pulses']).encode();identifier='pulse-'+secret['probe']+'-'+str(report['pulses'])
                    mesh.publish('shelf',dict(id=identifier,size=len(payload),sha256=hashlib.sha256(payload).hexdigest()),payload);last_pulse=now
                if now-stable_at>=120:
                    report['stable_seconds']=round(now-stable_at,3);phase='cleanup';mime=QMimeData();mime.setText(cleanup);clip.setMimeData(mime)
        except (OSError,ValueError,RuntimeError) as error:finish(type(error).__name__.upper())
    timer=QTimer();timer.setInterval(50);timer.timeout.connect(poll)
    sys.excepthook=lambda kind,value,trace:finish('QA_CALLBACK_'+kind.__name__.upper())
    deadline=QTimer();deadline.setSingleShot(True);deadline.timeout.connect(lambda:finish('CROSS_PLATFORM_TIMEOUT_'+phase.upper()))
    deadline.start(600000 if args.role=='source' else 240000);timer.start()
    wan.start()
    if args.role=='source' and os.environ.get('FD_QA_CROSS_LOGIN')!='1':account.register(secret['username'],secret['password'])
    else:account.login(secret['username'],secret['password'])
    QTimer.singleShot(30000,lambda:finish('QA_AUTH_FAILED') if not account.signed_in else None)
    result=app.exec()
    sync.pool.waitForDone(10000);temporary.cleanup();return result


if __name__=='__main__':raise SystemExit(main())
