"""Real Windows/Apple-silicon peers on separate, disposable GitHub runners.

Only synthetic fixtures and public device identities enter artifacts. Account
and GitHub tokens remain in a private runner directory and are never reported.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from urllib.error import HTTPError,URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler,Request,build_opener
from uuid import NAMESPACE_URL,uuid5
import zipfile
import io

REPOSITORY='los0711/-flickdock-build'
BOOTSTRAP_TIMEOUT_SECONDS=480
STEP_TIMEOUT_SECONDS=150
POLL_SECONDS=2
MIB=1024*1024


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self,request,fp,code,message,headers,newurl):
        if urlsplit(newurl).scheme!='https':raise ValueError('Insecure artifact redirect')
        result=super().redirect_request(request,fp,code,message,headers,newurl)
        if result is not None and urlsplit(request.full_url).netloc!=urlsplit(newurl).netloc:result.remove_header('Authorization')
        return result


def artifact(side,run,attempt,pair):
    name=f'dual-bootstrap-{pair}-{side}-{attempt}'
    token=os.environ['GITHUB_TOKEN'];opener=build_opener(_SafeRedirect())
    endpoint=f'https://api.github.com/repos/{REPOSITORY}/actions/runs/{run}/artifacts?per_page=100'
    deadline=time.monotonic()+BOOTSTRAP_TIMEOUT_SECONDS
    while time.monotonic()<deadline:
        try:
            request=Request(endpoint,headers={'Authorization':'Bearer '+token,'User-Agent':'FlickDock-disposable-QA','Accept':'application/vnd.github+json'})
            with opener.open(request,timeout=20) as reply:listing=json.loads(reply.read(1024*1024))
            row=next((value for value in listing['artifacts'] if value['name']==name and not value['expired']),None)
            if row:
                url=row['archive_download_url']
                if not re.fullmatch(r'https://api\.github\.com/repos/los0711/-flickdock-build/actions/artifacts/\d+/zip',url):raise ValueError('Invalid artifact URL')
                with opener.open(Request(url,headers={'Authorization':'Bearer '+token,'User-Agent':'FlickDock-disposable-QA'}),timeout=30) as reply:raw=reply.read(128*1024)
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    if archive.namelist()!=['public.json'] or archive.getinfo('public.json').file_size>32768:raise ValueError('Invalid bootstrap artifact')
                    value=json.loads(archive.read('public.json'))
                if value['side']!=side or value['run']!=run or value['attempt']!=attempt or value['pair']!=pair:raise ValueError('Wrong peer bootstrap')
                return value
        except HTTPError as error:
            if error.code not in (404,429,500,502,503,504):raise
        except (URLError,TimeoutError):pass
        time.sleep(POLL_SECONDS)
    raise TimeoutError('Peer bootstrap artifact unavailable')


def fixture(path,size,seed):
    digest=hashlib.sha256();remaining=size;index=0
    with Path(path).open('wb') as stream:
        while remaining:
            block=hashlib.shake_256(seed.encode()+index.to_bytes(4,'big')).digest(min(MIB,remaining))
            stream.write(block);digest.update(block);remaining-=len(block);index+=1
    return digest.hexdigest()


def kernel_diagnostics(engine,peer):
    """Record categories and counts, never device certificates, addresses or logs."""
    import ipaddress
    from collections import Counter
    def category(error):
        text=str(error or '').lower()
        return [word for word in ('timeout','refused','unreachable','network not allowed','no route','certificate','tls','unexpected device','not found','403','429') if word in text]
    try:
        status=engine.request('GET','/rest/system/status');discovery=engine.request('GET','/rest/system/discovery')
        sources=status.get('discoveryStatus',{});listeners=status.get('connectionServiceStatus',{})
        counts=Counter()
        for address in discovery.get(peer,[]):
            url=urlsplit(address)
            try:scope='public' if ipaddress.ip_address(url.hostname or '').is_global else 'private'
            except ValueError:scope='hostname'
            counts[url.scheme+'_'+scope]+=1
        configuration=engine.request('GET','/rest/config/devices/'+peer)
        return dict(discovery_enabled=status.get('discoveryEnabled'),discovery_methods=len(sources),
            peer_paused=configuration.get('paused'),dynamic_address='dynamic' in configuration.get('addresses',[]),
            discovery_cache_devices=len(discovery),
            discovery_errors=[category(value.get('error')) for value in sources.values() if value.get('error')],
            listeners=[dict(protocol=urlsplit(key).scheme,error=category(value.get('error')),
                            wan_addresses=len(value.get('wanAddresses',[]))) for key,value in listeners.items()],
            discovered_peer_addresses=dict(counts),dial_attempts=len(status.get('lastDialStatus',{})),
            dial_errors=[category(value.get('error')) for value in status.get('lastDialStatus',{}).values()],
            connected=engine.connections().get(peer,{}).get('connected',False))
    except Exception as error:return dict(diagnostic_error_type=type(error).__name__)


def main():
    if (os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('GITHUB_REPOSITORY')!=REPOSITORY
            or os.environ.get('RUNNER_OS') not in ('Windows','macOS')):
        raise RuntimeError('Native dual-device QA requires the authorized disposable CI runners')
    run=os.environ['GITHUB_RUN_ID'];attempt=os.environ['GITHUB_RUN_ATTEMPT'];platform=os.environ['RUNNER_OS']
    side=os.environ['FD_QA_SIDE'];pair=os.environ['FD_QA_PAIR']
    if side not in ('a','b') or pair not in ('win-mac','win-win','mac-mac'):raise ValueError('Invalid cross-device pair')
    if not run.isdigit() or not attempt.isdigit():raise ValueError('Invalid workflow identity')
    other='b' if side=='a' else 'a'
    cloud_only=os.environ.get('FD_QA_CLOUD_ONLY')=='true'
    if cloud_only and os.environ.get('FD_QA_KERNEL_ONLY')=='true':raise ValueError('Select either cloud-only or kernel-only verification')
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=('bootstrap','run'));parser.add_argument('--root',type=Path,required=True)
    args=parser.parse_args();root=args.root.resolve();root.mkdir(parents=True,exist_ok=True)
    if not root.is_relative_to(Path(os.environ['RUNNER_TEMP']).resolve()):raise ValueError('QA profile must belong to this runner')
    secret=json.loads(os.environ.pop('FLICKDOCK_QA_SYNC_JSON'))
    if (set(secret)!=set(('username','password','probe')) or not re.fullmatch(r'cross-qa-[a-f0-9]{16}',secret['username'])
            or not re.fullmatch(r'[a-f0-9]{32}',secret['probe'])):raise ValueError('Only disposable QA identities are accepted')
    secret['username']='cross-qa-'+hashlib.sha256((secret['username']+run+pair).encode()).hexdigest()[:16]
    # Reuse the independent native pasteboard reader without changing the
    # native platform selected for this actual CI process.
    from verify_clipboard import assert_native_paste
    os.environ['QT_QPA_PLATFORM']='windows' if platform=='Windows' else 'cocoa'
    from PySide6.QtCore import QMimeData,QUrl
    from PySide6.QtGui import QImage,QColor
    from PySide6.QtWidgets import QApplication
    from account_service import AccountService
    from cloud_events import CloudEvents
    from clipboard_sync import ClipboardSync,snapshot
    from shelf_sync import ShelfSync
    from syncthing_engine import SyncthingEngine
    from syncthing_jobs import ReceiveJob,deliver
    from manual_payload import prepare
    app=QApplication.instance() or QApplication([]);app.setQuitOnLastWindowClosed(False)
    from ci_vpn import verify_qt_proxy
    proxy=verify_qt_proxy()
    phase='initializing';failures=[]
    report=dict(ok=False,side=side,pair=pair,platform=platform,run=run,attempt=attempt,source_commit=os.environ['GITHUB_SHA'],
        native_clipboard=True,separate_operating_systems=True,region_asserted=False,os_credentials_written=False,
        cloud_only_automatic_sync=True,checks=[],api_failures=failures,http_payloads=[],proxy=proxy,
        proxy_pair_mode=os.environ.get('FD_QA_VPN_MODE','none'),
        timing_scope='Per-peer step elapsed includes coordination; HTTP payload timings are recorded separately',
        manual_scope='Not run: isolated cloud-only verification' if cloud_only else 'Original Syncthing engine + finite job adapter; application manual-transfer entry not yet wired')
    report_path=root/'report.json'
    sys.excepthook=lambda kind,value,trace:failures.append(dict(phase=phase,callback_error=kind.__name__))
    def until(predicate,seconds=STEP_TIMEOUT_SECONDS):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            app.processEvents()
            if predicate():return
            time.sleep(.005)
        raise TimeoutError('QA phase timed out: '+phase)
    def pump(seconds):
        end=time.monotonic()+seconds
        while time.monotonic()<end:app.processEvents();time.sleep(.005)
    class Store:
        def load(self):return None
        def save(self,value):pass
        def clear(self):pass
    class Adapter:
        def __init__(self):self.view=dict(permanent=[],texts={},metadata={})
        def snapshot(self):
            import copy
            return copy.deepcopy(self.view)
        def restore(self,value):self.view=value
        def title(self,ref):return Path(ref).name
    account=AccountService(root/'account',store=Store());account.device_name=f'FD2-{run}-{attempt}-{pair}-{side}-0'
    transport=account.api.request
    def traced(method,path,callback,**options):
        started=time.perf_counter();size=0
        if options.get('source') is not None:
            size=options.get('source_size')
            if size is None:size=Path(options['source']).stat().st_size
        elif options.get('destination') is not None:size=(options.get('expected') or {}).get('size',0)
        elif options.get('raw') is not None:size=len(options['raw'])
        def done(result,error):
            if size:
                seconds=time.perf_counter()-started
                report['http_payloads'].append(dict(phase=phase,method=method,bytes=size,ok=error is None,
                    seconds=round(seconds,4),mib_per_second=round(size/MIB/max(.0001,seconds),3),
                    timing_scope='HTTP request through verified response, including connect and storage work'))
            if error:failures.append(dict(phase=phase,method=method,route=path.split('?')[0].split('/')[1:3],
                                         status=getattr(error,'status',0),code=getattr(error,'code','')))
            callback(result,error)
        return transport(method,path,done,**options)
    account.api.request=traced
    def call(method,path,**options):
        result=[];account.api.request(method,path,lambda value,error:result.append((value,error)),**options)
        until(lambda:bool(result),seconds=45)
        value,error=result[0]
        if error:raise RuntimeError('Cloud request failed: '+str(getattr(error,'code','')))
        return value
    events=None;clipboard=None;shelf=None;engine=None
    try:
        if args.mode=='bootstrap':
            phase='authentication'
            if side=='b':artifact('a',run,attempt,pair)
            if side=='a' and attempt=='1':account.register(secret['username'],secret['password'])
            else:account.login(secret['username'],secret['password'])
            until(lambda:account.signed_in or not account.busy,seconds=60)
            if not account.signed_in:raise RuntimeError('Disposable account authentication failed')
            private=dict(token=account.api.token,device_id=account.device_id,account=account.account)
            session=root/'session.json';session.write_text(json.dumps(private),encoding='utf8');session.chmod(0o600)
            public=dict(side=side,pair=pair,platform=platform,run=run,attempt=attempt,account_device_id=account.device_id)
            if not cloud_only:
                phase='kernel_bootstrap';engine=SyncthingEngine(root/'kernel',mode='lan');engine.start()
                source=root/'manual-source.bin';fixture(source,32*MIB,f'{run}-{attempt}-{pair}-{side}-manual')
                payload=prepare([source],root/'payload',threading.Event())
                public.update(kernel_id=engine.device_id,descriptor=payload['descriptor'],total=payload['total'],count=payload['count'],names=payload['names'])
            (root/'public.json').write_text(json.dumps(public),encoding='utf8')
            print(json.dumps(dict(ok=True,side=side,phase='bootstrap',synthetic_fixture=True)),flush=True)
            return 0
        phase='peer_bootstrap';peer=artifact(other,run,attempt,pair);own=json.loads((root/'public.json').read_bytes())
        session=json.loads((root/'session.json').read_bytes());account.api.token=session['token'];account.device_id=session['device_id'];account.account=session['account']
        if not cloud_only:
            phase='kernel_prepare';engine=SyncthingEngine(root/'kernel',mode='remote');engine.start()
            assert engine.device_id==own['kernel_id']
        events=CloudEvents(account);clip=app.clipboard();clipboard=ClipboardSync(account,events,root/'clipboard',clipboard=clip)
        adapter=Adapter();shelf=ShelfSync(root/'shelf',account,adapter)
        shelf.attach_clipboard(clipboard);clipboard.activity_changed.connect(shelf.set_clipboard_priority)
        from transfer_priority import set_clipboard_priority
        clipboard.activity_changed.connect(set_clipboard_priority)
        events.content_changed.connect(shelf.request_refresh)
        events.socket.connected.connect(lambda:shelf.set_event_stream_online(True))
        events.socket.disconnected.connect(lambda:shelf.set_event_stream_online(False))
        account.account_changed.emit(dict(account.account))
        def barrier(stage):
            nonlocal phase
            call('PATCH','/v1/devices/'+account.device_id,data=dict(name=f'FD2-{run}-{attempt}-{pair}-{side}-{stage}'))
            end=time.monotonic()+STEP_TIMEOUT_SECONDS
            prefix=f'FD2-{run}-{attempt}-{pair}-{other}-'
            while time.monotonic()<end:
                rows=call('GET','/v1/devices')['devices']
                row=next((value for value in rows if value['id']==peer['account_device_id']),None)
                if row and row['name'].startswith(prefix) and row['name'][len(prefix):].isdigit() and int(row['name'][len(prefix):])>=stage:return
                pump(POLL_SECONDS)
            raise TimeoutError('Peer coordination timed out')
        from PySide6.QtNetwork import QAbstractSocket
        phase='event_socket';until(lambda:events.socket.state()==QAbstractSocket.SocketState.ConnectedState,seconds=60)
        kernel_only=os.environ.get('FD_QA_KERNEL_ONLY')=='true';report['cloud_tested']=not kernel_only
        def await_announcement():
            deadline=time.monotonic()+20
            while True:
                status=engine.request('GET','/rest/system/status')
                if any(key.startswith('global@') and value.get('error') is None for key,value in status.get('discoveryStatus',{}).items()):return
                if time.monotonic()>deadline:raise TimeoutError('Original kernel did not finish its first announcement')
                pump(.25)
        if kernel_only:phase='kernel_announcement';await_announcement()
        barrier(1)
        stage=1
        def record(name,sender,started,**fields):
            report['checks'].append(dict(name=name,sender=sender,receiver='b' if sender=='a' else 'a',
                                         ok=True,local_role='sender' if side==sender else 'receiver',seconds=round(time.perf_counter()-started,3),**fields))
            report_path.write_text(json.dumps(report,indent=2),encoding='utf8')
        for sender in (() if kernel_only else ('a','b')):
            for kind in ('text','image','file'):
                phase=f'clipboard_{sender}_{kind}';start=time.perf_counter();seed=f'FD2-{run}-{attempt}-{pair}-{sender}-{kind}'
                expected=None;mime=QMimeData()
                if kind=='text':expected=seed+' 中文\r\nhttps://example.invalid/'+secret['probe'];mime.setText(expected)
                elif kind=='image':
                    expected=QImage(640,360,QImage.Format.Format_ARGB32);expected.fill(QColor(24,104,176,177));mime.setImageData(expected)
                else:
                    source=root/(seed+'.bin');digest=fixture(source,16*MIB,seed)
                    mime.setUrls([QUrl.fromLocalFile(str(source))])
                before=clipboard._seen
                if side==sender:clip.setMimeData(mime)
                def ready():
                    value=snapshot(clip)
                    if not value:return False
                    if kind=='text':return value[0]=='text' and value[1]['text']==expected
                    if kind=='image':
                        if not clip.mimeData().hasImage():return False
                        actual=clip.mimeData().imageData()
                        return isinstance(actual,QImage) and actual.convertToFormat(QImage.Format.Format_RGBA8888)==expected.convertToFormat(QImage.Format.Format_RGBA8888)
                    return value[0]=='files' and len(value[1])==1 and Path(value[1][0]).name==source.name and Path(value[1][0]).is_file()
                until(ready)
                if kind=='file':
                    received=Path(snapshot(clip)[1][0]);assert received.stat().st_size==16*MIB
                    with received.open('rb') as stream:assert hashlib.file_digest(stream,'sha256').hexdigest()==digest
                    assert_native_paste('files',str(received))
                elif kind=='text':assert_native_paste('text',expected)
                else:assert_native_paste('image')
                if side==sender:until(lambda:clipboard._seen!=before and clipboard._pending is None and clipboard.view.get('backed_up') is True)
                else:
                    until(lambda:clipboard.view.get('phase')=='synced' and clipboard.view.get('value') is not None)
                assert clipboard.view.get('value') is not None,'Native content must also appear in clipboard preview'
                record('clipboard_'+kind,sender,start,bytes=16*MIB if kind=='file' else None,native_readback=True)
                stable=clipboard._seen;pump(1)
                assert clipboard._seen==stable and clipboard._pending is None,'A received clipboard must not create an echo upload'
                stage+=1;barrier(stage)
            phase=f'shelf_{sender}';start=time.perf_counter();seed=f'FD2-{run}-{attempt}-{pair}-{sender}-shelf'
            source=root/(seed+'.bin');digest=fixture(source,32*MIB,seed)
            if side==sender:adapter.view['permanent'].append(str(source));shelf.local_changed()
            def shelf_ready():
                return next((Path(ref) for ref in adapter.view['permanent'] if not ref.startswith('flickdock:')
                             and Path(ref).name==source.name and Path(ref).is_file()),None)
            if side==sender:until(lambda:shelf.sync_status_for_ref(str(source))=='synced')
            else:
                until(shelf_ready);received=shelf_ready()
                with received.open('rb') as stream:assert hashlib.file_digest(stream,'sha256').hexdigest()==digest
            record('shelf_add',sender,start,bytes=32*MIB,sha256_verified=True)
            stage+=1;barrier(stage)
            phase=f'shelf_delete_{sender}';start=time.perf_counter()
            if side==sender:
                shelf.cancel_upload(str(source),remove=True)
                adapter.view['permanent'].remove(str(source));shelf.local_changed()
            until(lambda:not shelf_ready())
            assert source.is_file(),'Removing a shelf item deleted a local original'
            record('shelf_delete',sender,start,original_preserved=True)
            stage+=1;barrier(stage)
        # Cloud tests finish before the direct-only kernel test, so a CI NAT
        # limitation cannot be misreported as a clipboard or R2 regression.
        if not cloud_only:
            phase='kernel_start';assert engine.device_id==own['kernel_id']
            if not kernel_only:await_announcement()
        def worker(action):
            finished=[]
            def execute():
                try:finished.append((action(),None))
                except BaseException as error:finished.append((None,error))
            thread=threading.Thread(target=execute,daemon=True);thread.start()
            until(lambda:bool(finished),seconds=240);thread.join(1)
            if finished[0][1]:raise finished[0][1]
            return finished[0][0]
        for sender in (() if cloud_only else ('a','b')):
            phase='manual_'+sender;start=time.perf_counter();task='fd-manual-'+uuid5(NAMESPACE_URL,f'{run}/{attempt}/{pair}/{sender}').hex
            cancel=threading.Event();job=None;shared=False;error=None;received_bytes=0;diagnostics=None
            try:
                if side==sender:
                    engine.share(task,root/'payload',peer['kernel_id'],['dynamic'],send=True);shared=True
                else:
                    stage_root=root/('manual-receive-'+sender);stage_root.mkdir();network=stage_root/'network';network.mkdir()
                    destination=root/('delivered-'+sender);destination.mkdir()
                    job=ReceiveJob(engine,task,network,peer['kernel_id'],['dynamic'],peer['descriptor'],cancel=cancel,progress=lambda count,total:None)
                    payload=worker(job.receive);job.close()
                    paths=worker(lambda:deliver(payload,stage_root,destination,peer['descriptor'],peer,cancel))
                    received=Path(paths[0]);expected=root/('expected-'+sender+'.bin')
                    digest=fixture(expected,32*MIB,f'{run}-{attempt}-{pair}-{sender}-manual')
                    with received.open('rb') as stream:assert hashlib.file_digest(stream,'sha256').hexdigest()==digest
                    received_bytes=received.stat().st_size
            except BaseException as failure:
                error=type(failure).__name__;diagnostics=worker(lambda:kernel_diagnostics(engine,peer['kernel_id']))
            finally:
                if job is not None:job.close()
            report['checks'].append(dict(name='manual_original_syncthing',sender=sender,ok=error is None,
                local_role='sender' if side==sender else 'receiver',error_type=error,verified_bytes=received_bytes,seconds=round(time.perf_counter()-start,3),public_relays=False,
                verified_by_peer_report_required=side==sender,diagnostics=diagnostics))
            stage+=1;barrier(stage)
            if shared:engine.unshare(task)
            report_path.write_text(json.dumps(report,indent=2),encoding='utf8')
        phase='cleanup'
        if side=='a' and not kernel_only:
            before=clipboard._seen;mime=QMimeData();mime.setText('FlickDock disposable QA finished '+run);clip.setMimeData(mime)
            until(lambda:clipboard._seen!=before and clipboard._pending is None and clipboard.view.get('backed_up'))
        stage+=1;barrier(stage)
        call('POST','/v1/auth/logout',data={})
        report['ok']=all(check['ok'] for check in report['checks']) and len(report['checks'])==(10 if cloud_only else 2 if kernel_only else 12)
        report['phase']='completed';report_path.write_text(json.dumps(report,indent=2),encoding='utf8')
        print(json.dumps(dict(ok=report['ok'],side=side,checks=len(report['checks']))),flush=True)
        return 0 if report['ok'] else 1
    except BaseException as error:
        report.update(phase=phase,error_type=type(error).__name__)
        report_path.write_text(json.dumps(report,indent=2),encoding='utf8')
        print(json.dumps(dict(ok=False,side=side,phase=phase,error_type=type(error).__name__)),flush=True)
        return 1
    finally:
        if clipboard is not None:clipboard.close()
        if shelf is not None:shelf.close()
        if events is not None:events.close()
        if engine is not None:engine.close()
        account.close()


if __name__=='__main__':raise SystemExit(main())
