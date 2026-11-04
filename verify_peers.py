"""Real account signaling, native mesh and bytes against an isolated Worker."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time


def run(argv,load_font):
    if os.environ.get('QT_QPA_PLATFORM')!='offscreen':raise RuntimeError('Peer verification requires offscreen')
    import logging
    logging.basicConfig(level=logging.WARNING)
    for name in ('cloud_pipe','adaptive_stream','peer_sync'):logging.getLogger(name).setLevel(logging.INFO)
    from PySide6.QtCore import Signal
    from PySide6.QtWidgets import QApplication
    from app_version import APP_VERSION
    from clipboard_sync import ClipboardSync
    from peer_clipboard import ClipboardPeerBridge
    from peer_sync import PeerSync
    from shelf_sync import ShelfSync
    from verify_clipboard import Account,Clipboard,Events
    from wan_transfer import WanTransferService
    from urllib.parse import urlsplit
    from cloud_config import DEFAULT_CLOUD_URL
    connection=json.loads(Path(argv[argv.index('--verify-connection')+1]).read_text(encoding='utf8'))
    origin=connection['origin'];url=urlsplit(origin)
    remote='--peer-remote' in argv;force_relay='--peer-force-relay' in argv
    bootstrap='--peer-cloud-bootstrap' in argv;force_cloud='--peer-force-cloud' in argv or bootstrap
    if (origin!=DEFAULT_CLOUD_URL or not remote) and (url.scheme!='http' or url.hostname!='127.0.0.1' or url.username or url.password):
        raise ValueError('Peer verification is restricted to an isolated loopback Worker')
    app=QApplication.instance() or QApplication([argv[0]]);app.setQuitOnLastWindowClosed(False);load_font(app)
    if remote:
        from PySide6.QtNetwork import QNetworkProxyFactory
        QNetworkProxyFactory.setUseSystemConfiguration(True)
    output=Path(argv[argv.index('--verify-output')+1]);report=dict(ok=False,version=APP_VERSION,
        frozen=bool(getattr(sys,'frozen',False)),scope='three isolated clients, real local Worker/D1/R2/WebRTC/TLS',
        os_clipboard=False,installer_executed=False)
    report.update(production_service=remote,forced_relay=force_relay,forced_cloud=force_cloud)
    report['native_signaling_blocked']=bootstrap
    def until(predicate,seconds=35):
        deadline=time.monotonic()+seconds
        while not predicate() and time.monotonic()<deadline:app.processEvents();time.sleep(.003)
        if not predicate():raise TimeoutError('Peer runtime verification did not settle')
    class QaAccount(Account):devices_changed=Signal(object)
    class Adapter:
        def __init__(self):self.view=dict(permanent=[],texts={},metadata={})
        def snapshot(self):return copy.deepcopy(self.view)
        def restore(self,view):self.view=copy.deepcopy(view)
        def title(self,ref):return self.view['metadata'].get(ref,{}).get('title') or Path(ref).name
    with tempfile.TemporaryDirectory(prefix='flickdock-peer-runtime-') as directory:
        root=Path(directory);wans=[];meshes=[];shelves=[];clips=[];syncs=[];accounts=[];held=[];blocked=True;body_gets=[];signaling=[]
        try:
            adapters=[Adapter() for _ in range(3)]
            for index,session in enumerate(connection['sessions']):
                account=QaAccount(origin,session);accounts.append(account);original=account.api.request
                def request(method,path,callback,original=original,**options):
                    if method=='GET' and ('/parts/' in path or path.endswith('/content')):body_gets.append(path)
                    if blocked and not path.startswith('/v1/wan/cloud/') and (method=='PUT' or method=='POST' and path=='/v1/clipboard'):
                        held.append((original,method,path,callback,options));return object()
                    return original(method,path,callback,**options)
                account.api.request=request
                wan=WanTransferService(root/str(index),origin=origin,allow_test_origin=not remote,device_name='Peer QA '+str(index))
                wan.attach_account(account);wans.append(wan);mesh=PeerSync(account,wan,qa_force_relay=force_relay);meshes.append(mesh)
                wan.sync_signal.connect(lambda event,index=index:signaling.append(dict(client=index,type=event.get('type'),id=str(event.get('id',''))[:8],message=event.get('message',''))) if event.get('type') in ('sync_error','sync_closed','sync_begin') else None)
                if bootstrap:
                    original_signal=wan._send_signal
                    def send(value,original=original_signal):
                        payload=value.get('payload',{})
                        if value.get('type') in ('description','candidate') or value.get('type')=='sync_signal' and payload.get('type') in ('description','candidate'):return True
                        return original(value)
                    wan._send_signal=send
                shelf=ShelfSync(root/str(index),account,adapters[index],poll_ms=600000);shelf.attach_peer(mesh)
                shelf.select_account(account.account);shelves.append(shelf)
                clip=Clipboard();clips.append(clip);client=ClipboardSync(account,Events(),root/str(index),clipboard=clip)
                client.peer_bridge=ClipboardPeerBridge(client,mesh);syncs.append(client);wan.start()
            until(lambda:all(len(mesh.available())==2 for mesh in meshes),seconds=90 if force_relay else 60 if bootstrap else 35)
            if force_cloud:
                for mesh in meshes:
                    for state in mesh.available():
                        adaptive=state['adaptive'];adaptive.secondary=None
                        if state['initiator'] and 'cloud' not in adaptive._paths:mesh._submit(adaptive._open_cloud)
                until(lambda:all('cloud' in state['adaptive']._paths for mesh in meshes for state in mesh.available()),seconds=45)
                for mesh in meshes:
                    for state in mesh.available():
                        adaptive=state['adaptive']
                        for key,path in list(adaptive._paths.items()):
                            if key!='cloud':
                                if hasattr(path.stream,'shutdown'):path.stream.shutdown()
                                else:path.stream.close()
                until(lambda:all(len(mesh.available())==2 and all(state['adaptive'].route=='cloud' for state in mesh.available()) for mesh in meshes))
                until(lambda:all(len(mesh.available())==2 and all(state['route']=='cloud' for state in mesh.available()) for mesh in meshes))
            report['routes']=[state['route'] for mesh in meshes for state in mesh.available()]
            if force_relay:assert set(report['routes'])=={'relay'}
            if force_cloud:assert set(report['routes'])=={'cloud'}
            report['all_pairs_connected']=True
            report['native_processes']=len({mesh.broker._process.pid for mesh in meshes})
            assert report['native_processes']==3
            until(lambda:all(not shelf.running for shelf in shelves))
            payload=os.urandom(1024*1024+23);source=root/'original.bin';source.write_bytes(payload)
            adapters[0].view['permanent']=[str(source)];shelves[0].local_changed();shelves[0].tick()
            until(lambda:all(adapter.view['permanent'] for adapter in adapters))
            for adapter in adapters[1:]:assert Path(adapter.view['permanent'][0]).read_bytes()==payload
            until(lambda:bool(held))
            assert not body_gets,'Cloud file bytes were requested before direct delivery'
            mime=clips[0].mimeData().__class__();mime.setText('同账号原生直连验证\n完整文本');clips[0].setMimeData(mime)
            until(lambda:all(clip.mimeData().text()=='同账号原生直连验证\n完整文本' for clip in clips))
            assert syncs[1].view['phase']==syncs[2].view['phase']=='peer'
            report.update(peer_before_cloud=True,verified_bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest(),
                          cloud_body_downloads_before_delivery=len(body_gets),clipboard_text=True)
            blocked=False
            for original,method,path,callback,options in held:original(method,path,callback,**options)
            until(lambda:not shelves[0].running and syncs[0]._pending is None)
            for client in syncs:client.refresh()
            until(lambda:all(client.view['phase']=='synced' for client in syncs))
            assert all(len(adapter.view['permanent'])==1 for adapter in adapters)
            report['cloud_backup_confirmed']=True
            if '--peer-manual' in argv:
                from flickdock import ShelfManager
                from types import SimpleNamespace
                destination=root/'automatic-receive';destination.mkdir();done=[[],[]];prompts=[]
                manager=SimpleNamespace(account=accounts[1],wan=wans[1],settings=SimpleNamespace(lan_receive_dir=str(destination)),
                    show_wan_dialog=lambda:prompts.append('unexpected confirmation'))
                wans[1].offer_received.connect(lambda offer:ShelfManager._wan_offer(manager,offer))
                for index in (0,1):wans[index].completed.connect(lambda value,index=index:done[index].append(value))
                manual=root/'automatic-original.bin';manual_body=os.urandom(384*1024+13);manual.write_bytes(manual_body)
                wans[0].send(wans[1].device_id,[str(manual)])
                until(lambda:all(done),seconds=70)
                assert not prompts and Path(done[1][0]['paths'][0]).read_bytes()==manual_body
                assert manual.read_bytes()==manual_body
                report.update(same_account_automatic_receive=True,manual_original_bytes=len(manual_body),manual_route=done[1][0].get('route',wans[1]._pending.get(done[1][0]['id'],{}).get('route')))
            accounts[0].signed_in=False;accounts[0].account_changed.emit(None)
            until(lambda:not meshes[0]._states and all(len(mesh.available())==1 for mesh in meshes[1:]))
            report.update(logout_revoked=True,ok=True)
        except Exception as error:
            import traceback
            report['error']=type(error).__name__+': '+str(error)
            report['traceback']=traceback.format_exc()
            report['connections']=[dict(peers=len(mesh.available()),states=[state['route'] for state in mesh._states.values()]) for mesh in meshes]
            report['cloud_connection_diagnostics']=[[dict(route=state['route'],attempts=state.get('cloud_attempts',0),
                error=state.get('cloud_error',''),starting=state.get('cloud_starting',False),
                rpc_error=state['rpc'].error if state['rpc'] else '',
                paths=[dict(key=path.key,dead=path.dead,error=getattr(path.stream,'error','')) for path in state['adaptive']._paths.values()] if state.get('adaptive') else []) for state in mesh._states.values()] for mesh in meshes]
            identities=[wan.device_id for wan in wans]
            report['presence']=[dict(connected=wan._connected,bound=wan._bound_identity is not None,
                peers=[dict(client=identities.index(row['id']) if row['id'] in identities else -1,online=row.get('online'),account=row.get('account_device')) for row in wan.peers()]) for wan in wans]
            report['signaling']=signaling[-50:]
        finally:
            for mesh in meshes:mesh.close()
            for wan in wans:wan.close()
            for client in syncs:client.close()
            for shelf in shelves:shelf.close()
            until(lambda:all(not client._jobs for client in syncs) and all(not shelf.jobs for shelf in shelves))
            for account in accounts:account.api.close()
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return 0 if report['ok'] else 1
