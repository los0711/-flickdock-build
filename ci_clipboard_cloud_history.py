"""Check incoming history and local-only paste on two isolated hosted desktops."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

from PySide6.QtCore import QMimeData,QPoint,QPointF,QUrl,Qt
from PySide6.QtGui import QColor,QCursor,QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from account_service import AccountService
from ci_clipboard_history_native import __file__ as target_program,current_process
from ci_shelf_direct_live import MemorySessions
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
    successful_pastes=[]
    shelf=SimpleNamespace(hide_animated=lambda **_:successful_pastes.append(True))
    history=ClipboardHistory(SimpleNamespace(app=app,clipboard_sync=client,shelf=shelf),root/'profile')
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
            if kind=='text' and value['text']==fixture and 'font-weight' in value['html']:return row['id']
            if kind=='link' and value['text']==fixture:return row['id']
            if kind=='image' and QImage.fromData(value).pixelColor(2,2).name()==fixture:return row['id']
            if kind=='file' and Path(value[0]).read_bytes()==b'verified original file bytes':return row['id']
        return None

    def paste_from_history(key,kind,fixture,original):
        target_dir=root/f'native-paste-target-{kind}';target_dir.mkdir()
        target=subprocess.Popen([sys.executable,target_program,'target',str(target_dir)],
                                env=dict(os.environ,FD_CLIPBOARD_QA_KEEP_PASTEBOARD='1'))
        before=client._seen,len(mutations),history.total
        prior=len(successful_pastes)
        history._create_panel()
        panel=history.panel
        activated=[]
        record_activation=activated.append
        panel.activated.connect(record_activation)
        try:
            wait(lambda:(target_dir/'pid').exists(),8)
            pid=int((target_dir/'pid').read_text())
            wait(lambda:current_process()==pid,8)
            panel.setGeometry(515,110,332,640);panel.show()
            if side=='win':QCursor.setPos(panel.mapToGlobal(QPoint(140,34)))
            QTest.qWait(120)
            assert current_process()==pid, 'clipboard history took focus'
            panel.show_all()
            wait(lambda:panel._stack_progress==1.,2)
            panel.repaint()
            card=next(rect for part,identity,rect in panel._hits if part=='card' and identity==key)
            visible=card.intersected(panel._scroll_rect().adjusted(0,2,0,-2))
            assert not visible.isEmpty()
            point=(panel._paint_origin()+visible.center()*panel._panel_scale).toPoint()
            if side=='win':
                QCursor.setPos(panel.mapToGlobal(point))
                QTest.qWait(130)
            assert panel._hit(QPointF(point))==('card',key), (point,panel._painted)
            QTest.mouseClick(panel,Qt.LeftButton,Qt.NoModifier,point)
            assert activated==[key], ('history card did not activate',len(activated),panel.expanded)
            wait(lambda:bool(panel._feedback) or (len(successful_pastes)==prior+1 and history._insert_job is None),8)
            assert len(successful_pastes)==prior+1, (panel._feedback,current_process()==pid)
            if kind=='image':
                wait(lambda:(target_dir/'image-inserted').is_file(),8)
                assert json.loads((target_dir/'image-incoming.json').read_bytes())['color']==fixture
            else:
                def received_text():
                    return any(json.loads(item.read_bytes())['text']==fixture
                               for item in target_dir.glob('observed-*.json'))
                wait(received_text,8)
            QTest.qWait(1200)
            assert board.text()==original
            assert (client._seen,len(mutations),history.total)==before
        finally:
            panel.activated.disconnect(record_activation)
            panel.hide()
            target.terminate();target.wait(timeout=5)

    run_id=os.environ['GITHUB_RUN_ID']
    moves=(('mac','text','Mac normal rich text '+run_id),('win','image','#397ea4'),
           ('mac','link','https://example.test/mac-clipboard/'+run_id),('win','text','Windows normal rich text '+run_id),
           ('mac','image','#9768a1'),('win','file',f'Windows cloud attachment 中文 {run_id}.txt'),
           ('mac','text','Mac verified the Windows attachment '+run_id))
    pastes={}
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
                if kind in ('text','image'):
                    pastes.setdefault(kind,(saved(kind,fixture),fixture))
                if kind=='text' and set(pastes)=={'text','image'} and len(successful_pastes)==0:
                    for paste_kind,(key,preview) in pastes.items():
                        print('Clicking a saved history card',side,paste_kind,flush=True)
                        paste_from_history(key,paste_kind,preview,fixture)
                    report['history_pastes_preserved_original_with_no_upload']=len(successful_pastes)

        assert len(successful_pastes)==2
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
