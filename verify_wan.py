"""Explicit isolated original-folder WebRTC verification; no cloud login."""
from pathlib import Path
import hashlib,json,os,sys,tempfile,time

def run(argv,load_font):
    if os.environ.get('QT_QPA_PLATFORM')!='offscreen':raise RuntimeError('WAN verification requires offscreen')
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from app_version import APP_VERSION
    from wan_transfer import WanTransferService
    from cloud_config import DEFAULT_CLOUD_URL
    app=QApplication.instance() or QApplication([argv[0]]);app.setQuitOnLastWindowClosed(False);load_font(app)
    output=Path(argv[argv.index('--verify-output')+1]);origin=os.environ.get('FLICKDOCK_QA_WAN_ORIGIN',DEFAULT_CLOUD_URL)
    local=origin.startswith('http://127.0.0.1:')
    if origin!=DEFAULT_CLOUD_URL and not local:raise ValueError('QA origin must be loopback')
    force_relay='--wan-force-relay' in argv
    report=dict(ok=False,version=APP_VERSION,frozen=bool(getattr(sys,'frozen',False)),account_login=False,origin=origin,installer_executed=False,forced_relay_test=force_relay)
    with tempfile.TemporaryDirectory(prefix='flickdock-wan-original-') as temporary:
        root=Path(temporary);source=root/'原文件夹';(source/'空目录').mkdir(parents=True)
        block=os.urandom(1024*1024);original=source/'原始素材.bin'
        with original.open('wb') as stream:
            for _ in range(32):stream.write(block)
        with original.open('rb') as stream:expected=hashlib.file_digest(stream,'sha256').hexdigest()
        destination=root/'接收';destination.mkdir()
        services=[WanTransferService(root/str(index),device_name='FlickDock QA '+str(index),origin=origin,allow_test_origin=local,qa_force_relay=force_relay) for index in range(2)]
        completed=[[],[]];offers=[];states=[];started=time.perf_counter();finished=False;identifier=''
        def finish(error=''):
            nonlocal finished
            if finished:return
            finished=True;timer.stop();deadline.stop()
            report.update(ok=not error,error=error,elapsed_seconds=time.perf_counter()-started)
            for service in services:service.close()
            output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');app.exit(1 if error else 0)
        def receive(offer):
            offers.append(offer)
            try:
                assert not list(destination.iterdir()),'Files written before consent'
                assert offer.get('device_code')==services[0].code,'Sender device code missing from the incoming request'
                assert services[1].accept(offer['id'],destination)
            except Exception as error:finish(type(error).__name__+': '+str(error))
        services[1].offer_received.connect(receive)
        def native_diagnostic(peer,value):
            event=value.get('event',{})
            if event.get('type')=='transport':
                report.setdefault('native_transports',[]).append(dict(peer=peer,generation=value.get('generation'),
                    transport=event.get('relayTransport'),proxy_used=bool(event.get('proxyUsed')),elapsed_seconds=time.perf_counter()-started))
        for index,service in enumerate(services):
            service.completed.connect(lambda value,index=index:completed[index].append(value))
            service.progress.connect(lambda value,index=index:states.append(dict(peer=index,elapsed_seconds=time.perf_counter()-started,
                **{key:value.get(key) for key in ('state','route','received','total')})))
            service._native.connect(lambda value,index=index:native_diagnostic(index,value))
            service.status_changed.connect(lambda value:print(value,flush=True))
        def poll():
            nonlocal identifier
            try:
                if not identifier and all(service.code for service in services):identifier=services[0].send(services[1].code,[str(source)])
                if all(completed) and all(service.peers() for service in services):
                    target=Path(completed[1][0]['paths'][0]);assert target.is_dir() and (target/'空目录').is_dir()
                    with (target/original.name).open('rb') as stream:assert hashlib.file_digest(stream,'sha256').hexdigest()==expected
                    with original.open('rb') as stream:assert hashlib.file_digest(stream,'sha256').hexdigest()==expected
                    assert len(offers)==1 and len(services[0].peers())==len(services[1].peers())==1
                    routes=[row.get('route') for row in states if row.get('state')=='complete' and row.get('route')]
                    assert routes and len(set(routes))==1,'Missing selected-candidate route'
                    if force_relay:assert routes[0]=='relay','Forced relay test did not use a TURN candidate'
                    report.update(verified_bytes=original.stat().st_size,sha256=expected,empty_folders_preserved=True,code_pairing=True,receiver_consent=True,recent_devices=True,route=routes[0],route_source='native-selected-candidate-pair')
                    finish()
                for service in services:
                    for transfer in service._transfers.values():
                        if transfer.cancel.is_set() and not transfer.finished:raise RuntimeError('WAN transfer failed; inspect states')
            except Exception as error:report['states']=states[-15:];finish(type(error).__name__+': '+str(error))
        timer=QTimer();timer.setInterval(25);timer.timeout.connect(poll);timer.start()
        deadline=QTimer();deadline.setSingleShot(True);deadline.timeout.connect(lambda:(report.update(states=states[-15:]),finish('WAN verification timed out')));deadline.start(180000)
        for service in services:service.start()
        return app.exec()
