"""Check incoming history and local-only paste on two isolated hosted desktops."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

from PySide6.QtCore import QMimeData,QUrl
from PySide6.QtGui import QColor,QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from account_service import AccountService
from ci_clipboard_history_native import __file__ as target_program,current_process
from ci_shelf_direct_live import MemorySessions
from clipboard_history_native import insert_image
from clipboard_history_runtime import ClipboardHistory
from clipboard_history_store import ClipboardHistoryStore
from clipboard_sync import ClipboardSync
from cloud_events import CloudEvents
from sync_scheduler import SyncScheduler


def run(root,credentials):
    app=QApplication([])
    app.setQuitOnLastWindowClosed(False)
    side='mac' if sys.platform=='darwin' else 'win'
    if side=='mac':
        from platform_native import configure_menu_bar_application
        configure_menu_bar_application()
    board=app.clipboard()
    account=AccountService(root/'account',store=MemorySessions())
    account.device_name='FlickDock-incoming-history-QA-'+side
    events=CloudEvents(account)
    scheduler=SyncScheduler(app,connected=False)
    client=ClipboardSync(account,events,root/'profile',scheduler=scheduler)
    history=ClipboardHistory(SimpleNamespace(app=app,clipboard_sync=client),root/'profile')
    report={'ok':False,'side':side,'phase':'login','received':[]}
    mutations=[]
    original_request=account.api.request

    def track(method,path,callback,**options):
        if (method,path)==('POST','/v1/clipboard'):mutations.append(True)
        return original_request(method,path,callback,**options)
    account.api.request=track

    def wait(predicate,seconds=70):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            app.processEvents()
            if predicate():return
            time.sleep(.008)
        raise TimeoutError(report['phase'])

    def publish(kind,fixture):
        previous=client._seen
        mime=QMimeData()
        if kind=='text':mime.setText(fixture);mime.setHtml(f'<p style="font-weight:700">{fixture}</p>')
        if kind=='link':mime.setText(fixture)
        if kind=='image':
            image=QImage(47,29,QImage.Format_ARGB32);image.fill(QColor(fixture));mime.setImageData(image)
        if kind=='file':
            file=root/fixture;file.write_bytes(b'verified original file bytes')
            mime.setUrls([QUrl.fromLocalFile(str(file))])
        board.setMimeData(mime)
        wait(lambda:client._seen!=previous and client._pending is None and client.view['phase']=='synced')

    def saved(kind,fixture):
        for row in history.entries:
            if row['kind']!=kind:continue
            if kind in ('text','file') and row['title']!=fixture:continue
            value=history.store.execute('payload',row['id'])[1]
            if kind=='text' and value['text']==fixture and 'font-weight' in value['html']:return value
            if kind=='link' and value['text']==fixture:return value
            if kind=='image' and QImage.fromData(value).pixelColor(2,2).name()==fixture:return value
            if kind=='file' and Path(value[0]).read_bytes()==b'verified original file bytes':return value
        return None

    def paste_received_image(data,fixture):
        target_dir=root/'native-paste-target';target_dir.mkdir()
        target=subprocess.Popen([sys.executable,target_program,'target',str(target_dir)],
                                env=dict(os.environ,FD_CLIPBOARD_QA_KEEP_PASTEBOARD='1'))
        before=client._seen,len(mutations),history.total
        try:
            wait(lambda:(target_dir/'pid').exists(),8)
            pid=int((target_dir/'pid').read_text())
            wait(lambda:current_process()==pid,8)
            with ThreadPoolExecutor(max_workers=1) as workers:
                result=workers.submit(insert_image,data,pid)
                wait(lambda:result.done() and (target_dir/'image-incoming.json').exists(),8)
                assert result.result()
            assert json.loads((target_dir/'image-incoming.json').read_bytes())['color']==fixture
            original=board.mimeData().imageData()
            if hasattr(original,'toImage'):original=original.toImage()
            assert original.pixelColor(2,2).name()==fixture
            QTest.qWait(800)
            assert (client._seen,len(mutations),history.total)==before
        finally:
            target.terminate();target.wait(timeout=5)

    run_id=os.environ['GITHUB_RUN_ID']
    moves=(('mac','text','Mac normal rich text '+run_id),('win','image','#397ea4'),
           ('mac','link','https://example.test/mac-clipboard/'+run_id),('win','text','Windows normal rich text '+run_id),
           ('mac','image','#9768a1'),('win','file',f'Windows cloud attachment 中文 {run_id}.txt'),
           ('mac','text','Mac verified the Windows attachment '+run_id))
    try:
        history.set_enabled(True)
        account.login(credentials['username'],credentials['password'])
        wait(lambda:account.signed_in or not account.busy,35)
        assert account.signed_in
        wait(lambda:scheduler.connected,35)
        for other,kind,fixture in moves:
            report['phase']=f'{other} {kind}'
            print('Clipboard history phase',side,report['phase'],flush=True)
            if side==other:
                publish(kind,fixture)
            else:
                wait(lambda:saved(kind,fixture) is not None)
                report['received'].append(kind)
                if side=='mac' and kind=='image':
                    print('Pasting image from saved history',flush=True)
                    paste_received_image(saved(kind,fixture),fixture)
                    report['native_paste_retained_original_with_no_upload']=True

        expected=sum(side==sender for sender,_,_ in moves)
        assert len(mutations)==expected,(len(mutations),expected)
        wait(lambda:history.total==len(moves) and not history.jobs)
        history.close()
        store=ClipboardHistoryStore(root/'profile',include_file_references=side!='mac')
        assert store.execute('page',12,0)[1]==len(moves)
        report.update(ok=True,phase='complete',history_items=len(moves),uploads_from_explicit_copy_only=len(mutations))
    finally:
        print(json.dumps(report),flush=True)
        history.close();client.close();events.close();scheduler.close();account.close()


if __name__=='__main__':
    assert os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted'
    os.environ['QT_QPA_PLATFORM']='cocoa' if sys.platform=='darwin' else 'windows'
    credentials=json.loads(os.environ.pop('FLICKDOCK_CLIPBOARD_HISTORY_QA'))
    assert credentials['username'].startswith('history-qa-')
    with tempfile.TemporaryDirectory(prefix='fd-clipboard-history-cloud-',dir=os.environ['RUNNER_TEMP']) as folder:
        run(Path(folder),credentials)
