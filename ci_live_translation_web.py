"""Native insertion into a separate Chromium-embedded app on disposable CI."""
from __future__ import annotations

from dataclasses import replace
from html import escape
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event
import time

from live_translation_editor import focused_editor


DRAFT = 'Original test message.'
TRANSLATION = '译文 ✓ Hello 😀'
CLIPBOARD_SAMPLE = 'FlickDock test original clipboard'
FIELDS = ('plain', 'multiline', 'rich', 'embedded', 'protected', 'read-only')
MARKUP = (
    '<input id="plain" aria-label="Ordinary input" value="{draft}">'
    '<textarea id="multiline" aria-label="Multiline input">{draft}</textarea>'
    '<div id="rich" contenteditable="true" role="textbox" aria-label="Chat editor">{draft}</div>'
    '<input id="protected" type="password" value="{draft}">'
    '<input id="read-only" readonly value="{draft}">'
)
EMBEDDED = '<div id="embedded" contenteditable="true" role="textbox" aria-label="Embedded chat">{draft}</div>'


def fixture(folder):
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QAccessible
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWebEngineWidgets import QWebEngineView

    app = QApplication([])
    QAccessible.setActive(True)
    app.clipboard().setText(CLIPBOARD_SAMPLE)
    view = QWebEngineView()
    view.setWindowTitle('FlickDock isolated browser editor')
    view.setGeometry(120, 120, 650, 450)
    view.show()
    view.raise_()
    view.activateWindow()
    view.setFocus()
    if sys.platform == 'darwin':
        from AppKit import NSApplication
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    nested = '<iframe title="Embedded chat" id="frame" srcdoc="' + escape(EMBEDDED.format(draft=DRAFT), quote=True) + '"></iframe>'
    style = '<style>input,textarea,div,iframe{display:block;margin:12px;width:480px} [contenteditable]{padding:8px;border:1px solid #888}</style>'
    view.setHtml(style + MARKUP.format(draft=DRAFT) + nested)

    last = None
    ready = False
    pending = False

    def loaded(ok):
        nonlocal ready
        ready = ok

    view.loadFinished.connect(loaded)

    def record(json_value):
        nonlocal pending
        pending = False
        if not json_value:
            return
        record = json.loads(json_value)
        record['active'] = view.isActiveWindow()
        temporary = folder / 'state.tmp'
        temporary.write_text(json.dumps(record), encoding='utf-8')
        try:
            temporary.replace(folder / 'state.json')
        except PermissionError:
            pass

    def advance():
        nonlocal last, pending
        if pending or not ready:
            return
        command_path = folder / 'command'
        command = command_path.read_text(encoding='utf-8') if command_path.exists() else 'plain'
        if command == 'clipboard':
            (folder / 'clipboard').write_text(app.clipboard().text(), encoding='utf-8')
            return
        if command == 'exit':
            app.quit()
            return
        changed = command != last
        if changed:
            view.raise_()
            view.activateWindow()
            view.setFocus()
            if sys.platform == 'darwin':
                NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        script = '''
            (() => {
                const command = %s;
                const field = command === "embedded"
                  ? document.getElementById("frame").contentDocument?.getElementById(command)
                  : document.getElementById(command);
                if (!field) return "";
                if (!field.dataset.bound) {
                    field.addEventListener("input", () => {
                        field.dataset.inputs = String(Number(field.dataset.inputs || "0") + 1);
                    });
                    field.dataset.bound = "1";
                }
                if (%s) {
                    field.focus();
                    if (field.isContentEditable) {
                        const range = field.ownerDocument.createRange();
                        range.selectNodeContents(field);
                        range.collapse(false);
                        const selection = field.ownerDocument.getSelection();
                        selection.removeAllRanges();
                        selection.addRange(range);
                    } else {
                        field.setSelectionRange(field.value.length, field.value.length);
                    }
                }
                return JSON.stringify({
                    field: command, text: field.isContentEditable ? field.innerText : field.value,
                    inputs: Number(field.dataset.inputs || "0"),
                    selected: field.isContentEditable ? field.ownerDocument.getSelection().toString().length
                                                     : field.selectionEnd - field.selectionStart,
                    focused: field === field.ownerDocument.activeElement
                });
            })()
        ''' % (json.dumps(command), str(changed).lower())
        last, pending = command, True
        view.page().runJavaScript(script, record)

    timer = QTimer(view, interval=50)
    timer.timeout.connect(advance)
    timer.start()
    return app.exec()


def wait_for(probe, interval=4):
    deadline = time.monotonic() + interval
    while time.monotonic() < deadline:
        result = probe()
        if result:
            return result
        time.sleep(.06)
    return None


def verify_field(folder, field):
    folder.mkdir()
    env = dict(os.environ, QTWEBENGINE_CHROMIUM_FLAGS='--force-renderer-accessibility --disable-gpu',
               QT_LOGGING_RULES='qt.accessibility.*=false')
    command = folder / 'command'
    command.write_text(field, encoding='utf-8')
    child = subprocess.Popen([sys.executable, __file__, '--fixture', str(folder)], env=env)
    try:
        def state():
            try:
                return json.loads((folder / 'state.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return {}

        cancel = Event()
        assert wait_for(lambda: state().get('field') == field and state().get('focused') and state().get('active'),
                        interval=9), state()
        protected = field in ('protected', 'read-only')
        observed = wait_for(lambda: focused_editor(cancel),interval=1 if protected else 4)
        if observed is None and not protected and sys.platform == 'darwin':
            from live_translation_editor import _mac_editor
            _mac_editor(cancel,None,None,False,rejected=lambda reason:print(field,reason,flush=True))
        if protected:
            attempted = focused_editor(cancel,expected=observed,insertion=TRANSLATION) if observed else False
            row = dict(blocked=not attempted and state().get('text') == DRAFT,readable=observed is not None)
        else:
            row = dict(read=observed is not None,original_length=len(observed.text) if observed else None)
            if observed is not None:
                stale = replace(observed,text='A stale draft.')
                row['stale_rejected'] = not focused_editor(cancel,expected=stale,insertion=TRANSLATION)
                translation = TRANSLATION+'\nSecond line.' if field=='multiline' else TRANSLATION
                inserted = focused_editor(cancel,expected=observed,insertion=translation)
                applied = wait_for(lambda: state().get('text') == translation and state().get('inputs',0)>0)
                current = focused_editor(cancel)
                row.update(request_accepted=bool(inserted),updated=bool(applied),input_event=state().get('inputs',0)>0,
                           final_length=len(state().get('text','')),native_selection=current.selection if current else None,
                           dom_selected=state().get('selected'))
        command.write_text('clipboard',encoding='utf-8')
        restored = folder/'clipboard'
        if wait_for(restored.exists):
            row['clipboard_preserved'] = restored.read_text(encoding='utf-8') == CLIPBOARD_SAMPLE
        print(field,row,flush=True)
        return row
    finally:
        command.write_text('exit',encoding='utf-8')
        try:
            child.wait(timeout=6)
        except subprocess.TimeoutExpired:
            child.terminate()
            child.wait(timeout=4)


def verify(output):
    if os.environ.get('GITHUB_ACTIONS') != 'true' or sys.platform not in ('win32', 'darwin'):
        raise RuntimeError('Requires an isolated native Windows or macOS desktop.')
    report = {'platform': sys.platform, 'fields': {}}
    try:
        with tempfile.TemporaryDirectory(prefix='flickdock-embedded-web-') as location:
            for field in FIELDS:
                report['fields'][field] = verify_field(Path(location)/field,field)
    finally:
        output.write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report),flush=True)


if __name__ == '__main__':
    if sys.argv[1] == '--fixture':
        raise SystemExit(fixture(Path(sys.argv[2])))
    verify(Path(sys.argv[1]))
