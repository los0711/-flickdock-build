"""Send a history card into another application's real native text control."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from clipboard_history_native import front_target
from clipboard_history_panel import ClipboardHistoryPanel
from clipboard_history_store import ClipboardHistoryStore


SENTINEL='Existing clipboard fixture'
TEXT='FlickDock 历史 🙂 — café'


def wait_for(predicate):
    deadline=time.monotonic()+8
    while not predicate():
        if time.monotonic()>deadline:raise AssertionError('Native text target timed out')
        QTest.qWait(20)


def target(root):
    app=QApplication([])
    editor=QPlainTextEdit()
    editor.setWindowTitle('FlickDock isolated clipboard target')
    editor.setGeometry(85,80,380,260)
    editor.show();editor.raise_();editor.activateWindow();editor.setFocus()
    if sys.platform=='darwin':
        import AppKit
        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    app.clipboard().setText(SENTINEL)
    (root/'pid').write_text(str(os.getpid()))
    def record():
        payload=dict(text=editor.toPlainText(),clipboard=app.clipboard().text())
        if payload['text']!=TEXT:return
        (root/'incoming.json').write_text(json.dumps(payload))
        (root/'incoming.json').replace(root/'target.json')
    editor.textChanged.connect(record)
    QTimer.singleShot(20000,app.quit)
    return app.exec()


def current_process():
    value=front_target()
    return value[-1] if isinstance(value,tuple) else value


def run(root):
    from types import SimpleNamespace
    from PySide6.QtCore import QObject, Signal
    from clipboard_history_runtime import ClipboardHistory
    from platform_native import configure_menu_bar_application

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
        QTest.mouseClick(panel,Qt.LeftButton,Qt.NoModifier,row.center().toPoint())
        def received_text():
            path=root/'target.json'
            if not path.is_file():return False
            data=json.loads(path.read_bytes())
            return data['text']==TEXT and data['clipboard']==SENTINEL
        wait_for(received_text)
        report.update(ok=True,input_matches=True,system_clipboard_untouched=True)
        assert ClipboardHistoryStore(root/'saved').execute('list',3,0)[0]['title']==TEXT
    finally:
        if runtime:runtime.close()
        procedure.terminate()
        procedure.wait(timeout=5)
        print(json.dumps(report),flush=True)
    return 0


if __name__=='__main__':
    assert os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted'
    directory=Path(sys.argv[2]).resolve()
    assert directory.is_relative_to(Path(os.environ['RUNNER_TEMP']).resolve())
    directory.mkdir(parents=True,exist_ok=True)
    raise SystemExit(target(directory) if sys.argv[1]=='target' else run(directory))
