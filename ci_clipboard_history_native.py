"""Paste history cards into another process using native keyboard input."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QImage, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTextEdit

from clipboard_history_native import front_target
from clipboard_history_panel import ClipboardHistoryPanel
from clipboard_history_store import ClipboardHistoryStore


SENTINEL='Existing clipboard fixture'
TEXT='FlickDock 历史 🙂 — café'
IMAGE_COLOR='#37a65b'


def wait_for(predicate):
    deadline=time.monotonic()+8
    while not predicate():
        if time.monotonic()>deadline:raise AssertionError('Native text target timed out')
        QTest.qWait(20)


def target(root):
    app=QApplication([])
    class ImageTarget(QTextEdit):
        def keyPressEvent(self,event):
            if event.matches(QKeySequence.StandardKey.Paste):
                print(json.dumps({'target_received_paste_key':True,
                                  'clipboard_formats':app.clipboard().mimeData().formats()}),flush=True)
            super().keyPressEvent(event)

        def canInsertFromMimeData(self,mime):
            return mime.hasImage() or super().canInsertFromMimeData(mime)

        def insertFromMimeData(self,mime):
            if not mime.hasImage():
                super().insertFromMimeData(mime)
                return
            image=mime.imageData()
            if hasattr(image,'toImage'):image=image.toImage()
            assert isinstance(image,QImage) and not image.isNull(),mime.formats()
            self.textCursor().insertImage(image)
            assert self.document().characterAt(self.textCursor().position()-1)=='\ufffc'
            data={'color':image.pixelColor(2,2).name(),'size':[image.width(),image.height()]}
            (root/'image-incoming.json').write_text(json.dumps(data))
            (root/'image-inserted').touch()
    editor=ImageTarget()
    editor.setWindowTitle('FlickDock isolated clipboard target')
    editor.setGeometry(85,80,380,260)
    editor.show();editor.raise_();editor.activateWindow();editor.setFocus()
    if sys.platform=='darwin':
        import AppKit
        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    app.clipboard().setText(SENTINEL)
    (root/'pid').write_text(str(os.getpid()))
    observations=0
    def record():
        nonlocal observations
        payload=dict(text=editor.toPlainText(),clipboard=app.clipboard().text())
        observations+=1
        pending=root/f'observed-{observations:04}.tmp'
        pending.write_text(json.dumps(payload))
        pending.replace(pending.with_suffix('.json'))
        if payload['text']!=TEXT:return
        (root/'incoming.json').write_text(json.dumps(payload))
        (root/'incoming.json').replace(root/'target.json')
    editor.textChanged.connect(record)
    QTimer.singleShot(40000,app.quit)
    return app.exec()


def current_process():
    value=front_target()
    return value[-1] if isinstance(value,tuple) else value


def run(root):
    from types import SimpleNamespace
    from PySide6.QtCore import QObject, Signal
    from clipboard_history_runtime import ClipboardHistory
    from platform_native import configure_menu_bar_application
    import clipboard_history_native as native
    import clipboard_history_runtime as history

    paste_traces=[]
    if sys.platform=='win32':
        original_snapshot=native._snapshot_windows_clipboard
        original_send=native._send_windows_paste
        def snapshot(user,kernel,ole,output):
            result=original_snapshot(user,kernel,ole,output)
            paste_traces.append({'clipboard_backup_completed':result,'formats':list(output)})
            return result
        def send(user,target):
            result=original_send(user,target)
            paste_traces.append({'system_paste_key_dispatched':result})
            return result
        native._snapshot_windows_clipboard=snapshot
        native._send_windows_paste=send

    original_insert=history.insert_image
    def insert(*args,**kwargs):
        try:
            result=original_insert(*args,**kwargs)
        except Exception as error:
            paste_traces.append({'paste_error':repr(error)})
            raise
        paste_traces.append({'paste_requested':result})
        return result
    history.insert_image=insert

    class ClipboardObservation(QObject):
        copy_observed=Signal(object)

        def set_local_observation(self, enabled, *, source):
            return

    class PassiveShelf:
        def hide_animated(self, *, explicit):
            return

    app=QApplication([])
    configure_menu_bar_application()
    procedure=subprocess.Popen([sys.executable,__file__,'target',str(root)])
    runtime=None
    report={'ok':False,'platform':sys.platform}
    try:
        wait_for(lambda:(root/'pid').is_file())
        pid=int((root/'pid').read_text())
        wait_for(lambda:current_process()==pid)
        manager=SimpleNamespace(app=app,clipboard_sync=ClipboardObservation(),shelf=PassiveShelf())
        runtime=ClipboardHistory(manager,root/'saved')
        runtime.set_enabled(True)
        runtime.observe(('text',{'text':TEXT}))
        wait_for(lambda:len(runtime.entries)==1)
        runtime._create_panel()
        panel=runtime.panel
        panel.setGeometry(515,110,332,260)
        panel.show()
        QTest.qWait(150)
        assert current_process()==pid, 'History panel took focus from the native target'
        panel.repaint()
        row=next(rect for part,key,rect in panel._hits if part=='card')
        activated=[]
        panel.activated.connect(activated.append)
        QTest.mouseClick(panel,Qt.LeftButton,Qt.NoModifier,row.center().toPoint())
        assert activated, 'The card body did not activate'
        def received_text():
            path=root/'target.json'
            if not path.is_file():return False
            data=json.loads(path.read_bytes())
            return data['text']==TEXT and data['clipboard']==SENTINEL
        wait_for(received_text)
        assert ClipboardHistoryStore(root/'saved').execute('list',3,0)[0]['title']==TEXT

        image=QImage(47,29,QImage.Format_ARGB32)
        image.fill(QColor(IMAGE_COLOR))
        runtime.observe(('image',{'image':image}))
        wait_for(lambda:len(runtime.entries)==2)
        panel.show_all()
        wait_for(lambda:panel._stack_progress==1.)
        panel.repaint()
        key=next(entry['id'] for entry in runtime.entries if entry['kind']=='image')
        rect=next(rect for part,identity,rect in panel._hits if part=='card' and identity==key)
        assert current_process()==pid
        QTest.mouseClick(panel,Qt.LeftButton,Qt.NoModifier,rect.center().toPoint())
        assert activated[-1]==key and len(activated)==2,activated
        wait_for(lambda:(root/'image-inserted').is_file())
        actual=json.loads((root/'image-incoming.json').read_bytes())
        assert actual=={'color':IMAGE_COLOR,'size':[47,29]},actual
        wait_for(lambda:runtime._insert_job is None and app.clipboard().text()==SENTINEL)
        report.update(ok=True,input_matches=True,image_inserted=True,original_clipboard_restored=True)
    finally:
        if not report['ok']:
            observed=sorted(root.glob('observed-*.json'))
            report.update(front_process=current_process(),history_feedback=runtime.panel._feedback if runtime and runtime.panel else None,
                          observed=json.loads(observed[-1].read_bytes()) if observed else None)
        if runtime:runtime.close()
        procedure.terminate()
        procedure.wait(timeout=5)
        report['paste_traces']=paste_traces
        print(json.dumps(report),flush=True)
    return 0


if __name__=='__main__':
    assert os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted'
    directory=Path(sys.argv[2]).resolve()
    assert directory.is_relative_to(Path(os.environ['RUNNER_TEMP']).resolve())
    directory.mkdir(parents=True,exist_ok=True)
    raise SystemExit(target(directory) if sys.argv[1]=='target' else run(directory))
