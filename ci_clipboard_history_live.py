"""Validate native Mac history paste against the real service and a Windows runner."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtGui import QColor,QImage
from PySide6.QtWidgets import QApplication

from account_service import AccountService
from clipboard_history_runtime import ClipboardHistory
from clipboard_sync import ClipboardSync
from cloud_events import CloudEvents
from sync_scheduler import SyncScheduler
from ci_clipboard_history_native import check_windows_bridge,current_process,IMAGE_COLOR,TEXT
from clipboard_history_store import encode_png
from ci_shelf_direct_live import MemorySessions


def run(root, credentials):
    on_mac=sys.platform=='darwin'
    app=QApplication([])
    app.setQuitOnLastWindowClosed(False)
    if on_mac:
        from platform_native import configure_menu_bar_application
        configure_menu_bar_application()
    account=AccountService(root/'account',store=MemorySessions())
    account.device_name='FlickDock-history-QA-Mac' if on_mac else 'FlickDock-history-QA-Windows'
    events=CloudEvents(account)
    scheduler=SyncScheduler(app,connected=False)
    board=app.clipboard()
    client=ClipboardSync(account,events,root/'client',clipboard=board,scheduler=scheduler)
    history=ClipboardHistory(SimpleNamespace(app=app,clipboard_sync=client),root/'client') if on_mac else None
    report={'ok':False,'platform':sys.platform,'phase':'login'}
    mutations=[]
    received=[]
    observed=[]
    client.copy_observed.connect(observed.append)
    target=None
    request=account.api.request

    def capture(method,path,callback,**kwargs):
        if (method,path)==('POST','/v1/clipboard') or method=='PUT' and path.endswith('/content'):
            mutations.append(method)
        return request(method,path,callback,**kwargs)

    account.api.request=capture
    client.cloud_files_received.connect(lambda identity,paths,directory:received.append((identity,Path(paths[0]).name)))
    seed='flickdock-history-win-seed-'+os.environ['GITHUB_RUN_ID']+'.txt'
    ready='flickdock-history-ready-'+os.environ['GITHUB_RUN_ID']
    finish='flickdock-history-mac-manual-'+os.environ['GITHUB_RUN_ID']+'.txt'

    def wait(predicate,timeout=80):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            app.processEvents()
            if predicate():return
            time.sleep(.008)
        raise TimeoutError(report['phase'])

    def latest():
        result=[]
        request('GET','/v1/clipboard',lambda payload,error:result.append((payload,error)))
        wait(lambda:bool(result),12)
        payload,error=result[0]
        assert error is None,getattr(error,'code','')
        return payload.get('clipboard') or {}

    def write_file(name,content):
        path=root/name
        path.write_text(content,encoding='utf8')
        mime=QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path))])
        board.setMimeData(mime)

    try:
        if history:history.set_enabled(True)
        account.login(credentials['username'],credentials['password'])
        wait(lambda:account.signed_in or not account.busy,35)
        assert account.signed_in
        wait(lambda:client.scheduler.connected,35)
        if on_mac:
            mime=QMimeData();mime.setText(ready);board.setMimeData(mime)
            report['phase']='Windows fixture and history retention'
            wait(lambda:any(name==seed for _,name in received) and any(row['kind']=='file' for row in history.entries))
            wait(lambda:not history.jobs,8)
            seed_id=next(identity for identity,name in received if name==seed)
            assert latest()['id']==seed_id
            entries={row['id'] for row in history.entries}
            item=next(row for row in history.entries if row['kind']=='file' and row['title']==seed)
            kind,files=history.store.execute('payload',item['id'])
            assert kind=='file' and len(files)==1

            from AppKit import NSPasteboard, NSPasteboardItem
            from Foundation import NSURL
            from concurrent.futures import ThreadPoolExecutor
            from clipboard_history_native import insert_files,insert_image,insert_text
            from ci_clipboard_history_native import __file__ as target_program

            target_data=root/'target';target_data.mkdir()
            target=subprocess.Popen([sys.executable,target_program,'target',str(target_data)],
                                    env=dict(os.environ,FD_CLIPBOARD_QA_KEEP_PASTEBOARD='1'))
            wait(lambda:(target_data/'pid').exists(),10)
            pid=int((target_data/'pid').read_text())
            wait(lambda:current_process()==pid,10)
            original=board.mimeData().urls()[0].toLocalFile()
            alias=root/'unmarked-reference.txt';os.link(files[0],alias)
            board_data=NSPasteboard.generalPasteboard()
            native_url=str(NSURL.fileURLWithPath_(str(alias)).fileReferenceURL().absoluteString())
            mutations.clear()
            observed.clear()
            picture=QImage(47,29,QImage.Format_ARGB32);picture.fill(QColor(IMAGE_COLOR))
            report['phase']='native text, image and file pastes'
            with ThreadPoolExecutor(max_workers=1) as worker:
                for _ in range(3):
                    output=target_data/'files-incoming.json'
                    output.unlink(missing_ok=True)
                    insertion=worker.submit(insert_files,files,pid)
                    wait(lambda:insertion.done() and output.exists(),8)
                    assert insertion.result() and json.loads(output.read_text())==['Windows fixture']
                    assert board.mimeData().urls()[0].toLocalFile()==original
                for insert,payload,marker in ((insert_text,TEXT,'target.json'),
                                              (insert_image,encode_png(picture),'image-inserted')):
                    done=target_data/marker
                    insertion=worker.submit(insert,payload,pid)
                    wait(lambda:insertion.done() and done.exists(),8)
                    assert insertion.result() and board.mimeData().urls()[0].toLocalFile()==original

            report['phase']='image rematerialized under a new file name'
            picture.setText('clipboard-export','fresh file identity')
            for index in range(3):
                generated=root/f'republished-{index}_d.png'
                assert picture.save(str(generated),'PNG')
                mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(generated))]);board.setMimeData(mime)
                sequence=client._sequence()
                wait(lambda:client._last_sequence==sequence,8)
                assert client._native_format_issue=='PRIVATE_MAC_PASTEBOARD'
                assert latest()['id']==seed_id

            report['phase']='unmarked AppKit file-reference publication'
            for _ in range(3):
                native_item=NSPasteboardItem.alloc().init()
                assert native_item.setString_forType_(native_url,'public.file-url')
                assert board_data.clearContents()>0 and board_data.writeObjects_([native_item])
                sequence=int(board_data.changeCount())
                wait(lambda:client._last_sequence==sequence,8)
                assert client._native_format_issue=='PRIVATE_MAC_PASTEBOARD'
                assert latest()['id']==seed_id
            assert not mutations,mutations
            assert not observed,observed
            assert {row['id'] for row in history.entries}==entries
            report.update(native_history_pastes=5,unmarked_reference_publications=3,reencoded_images=3,
                          history_cloud_mutations=len(mutations),original_clipboard_restored=True)

            report['phase']='ordinary copy after history paste'
            write_file(finish,'Mac ordinary copy')
            wait(lambda:client._seen and client._seen!=seed_id and client._pending is None)
            assert latest()['id']==client._seen
            report['phase']='ordinary Windows copy after blocking bridge traffic'
            wait(lambda:board.mimeData().text()=='Native ordinary copy fixture')
        else:
            report['phase']='Mac peer ready'
            wait(lambda:board.mimeData().hasText() and board.mimeData().text()==ready)
            received.clear()
            report['phase']='Windows genuine file copy'
            write_file(seed,'Windows fixture')
            wait(lambda:client._seen and client._pending is None and client.view.get('phase')=='synced')
            report['phase']='watch for leaked references, then a genuine Mac copy'
            wait(lambda:bool(received))
            assert [name for _,name in received]==[finish],received
            assert Path(board.mimeData().urls()[0].toLocalFile()).read_text(encoding='utf8')=='Mac ordinary copy'
            baseline=latest()['id'];mutations.clear();observed.clear()
            report['phase']='Windows native relay cache and clipboard owner'
            generated=Path.home()/'AppData'/'LocalLow'/'Tencent'/'WeType'/'ClipboardTmp'/'1795276507432_d.png'
            generated.parent.mkdir(parents=True,exist_ok=True)
            picture=QImage(47,29,QImage.Format_ARGB32);picture.fill(QColor(IMAGE_COLOR))
            assert not generated.exists() and picture.save(str(generated),'PNG')
            mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(generated))]);board.setMimeData(mime)
            sequence=client._sequence();wait(lambda:client._last_sequence==sequence,8)
            assert client._native_format_issue=='WETYPE_CLIPBOARD_RELAY'
            assert not observed and not mutations and latest()['id']==baseline

            def check_cloud():
                wait(lambda:client._native_format_issue=='WETYPE_CLIPBOARD_RELAY' and
                     client._last_sequence==client._sequence(),8)
                assert not observed and not mutations and latest()['id']==baseline
            check_windows_bridge(root,app,check_cloud,verify_reader_roundtrip=False)
            wait(lambda:client._seen and client._seen!=baseline and client._pending is None)
            report.update(history_echoes_received=0,ordinary_mac_copy_received=True,
                          bridge_image_blocked=True,bridge_text_blocked=True,ordinary_windows_copy_synced=True)
        report.update(ok=True,phase='complete')
    finally:
        print(json.dumps(report),flush=True)
        if target is not None:
            target.terminate();target.wait(timeout=5)
        if history:history.close()
        client.close();events.close();scheduler.close();account.close()


if __name__=='__main__':
    assert os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted'
    import tempfile
    from PySide6.QtCore import QCoreApplication
    assert QCoreApplication.instance() is None
    os.environ['QT_QPA_PLATFORM']='cocoa' if sys.platform=='darwin' else 'windows'
    credentials=json.loads(os.environ.pop('FLICKDOCK_CLIPBOARD_HISTORY_QA'))
    assert credentials['username'].startswith('history-qa-')
    with tempfile.TemporaryDirectory(prefix='flickdock-history-live-',dir=os.environ['RUNNER_TEMP']) as folder:
        run(Path(folder),credentials)
