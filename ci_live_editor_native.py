
"""Exercise the actual accessibility providers on a disposable CI desktop."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event
import time

from live_translation_editor import focused_editor


def fixture(folder):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QLineEdit, QVBoxLayout, QWidget

    app = QApplication([])
    window = QWidget()
    window.setWindowTitle('FlickDock isolated accessibility fixture')
    window.setGeometry(120, 140, 480, 120)
    body = QVBoxLayout(window)
    field = QLineEdit(window)
    field.setAccessibleName('FlickDock synthetic edit')
    field.setText('Synthetic text')
    field.setSelection(0, 9)
    body.addWidget(field)
    window.show()
    window.raise_()
    window.activateWindow()
    field.setFocus()

    previous = None

    def advance():
        nonlocal previous
        command_file = folder / 'command'
        command = command_file.read_text(encoding='utf8') if command_file.exists() else 'observe'
        if command != previous:
            previous = command
            if command == 'secure':
                field.setEchoMode(QLineEdit.Password)
                field.setText('Do not read this fixture')
            if command == 'insert':
                field.setEchoMode(QLineEdit.Normal)
                field.setText('abc')
                field.setCursorPosition(3)
            if command == 'exit':
                app.quit()
                return
        result = dict(text=field.text(), active=window.isActiveWindow(), selected=field.selectedText())
        pending = folder / 'state.tmp'
        pending.write_text(json.dumps(result), encoding='utf8')
        try:
            pending.replace(folder / 'state.json')
        except PermissionError:
            return  # Windows may briefly hold the previous report open.

    timer = QTimer(window, interval=40)
    timer.timeout.connect(advance)
    timer.start()
    return app.exec()


def wait_for(predicate, seconds=10):
    limit = time.monotonic() + seconds
    while time.monotonic() < limit:
        result = predicate()
        if result:
            return result
        time.sleep(.07)
    return None


def verify(output):
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise RuntimeError('Run on a disposable CI desktop.')
    result = dict(platform=sys.platform, native_checked=False)
    if sys.platform == 'darwin':
        from translation_focus import _mac_api
        if not _mac_api()[0].AXIsProcessTrusted():
            result['reason'] = 'The runner did not grant the Python process Accessibility access.'
            output.write_text(json.dumps(result, indent=2), encoding='utf8')
            print(result)
            return
    with tempfile.TemporaryDirectory(prefix='flickdock-live-editor-') as path:
        folder = Path(path)
        cmd = folder / 'command'
        process = subprocess.Popen([sys.executable, __file__, '--fixture', path])
        try:
            def state():
                try:
                    return json.loads((folder / 'state.json').read_text(encoding='utf8'))
                except (OSError, ValueError):
                    return {}

            cancel = Event()
            assert wait_for(lambda: state().get('active')), 'Fixture never received native editor focus'
            editor = wait_for(lambda: focused_editor(cancel))
            assert editor is not None and editor.text == 'Synthetic text', (editor,state())
            assert editor.selection == (0, 9), editor.selection
            sample = focused_editor(cancel, sample=True)
            assert sample is not None and sample.text == 'Synthetic', sample
            cmd.write_text('secure', encoding='utf8')
            assert wait_for(lambda: state().get('text') == 'Do not read this fixture')
            assert focused_editor(cancel) is None
            assert focused_editor(cancel, sample=True) is None
            assert not focused_editor(cancel, expected=editor, insertion='hidden ')
            cmd.write_text('insert', encoding='utf8')
            assert wait_for(lambda: state().get('text') == 'abc')
            editor = wait_for(lambda: focused_editor(cancel))
            assert editor is not None and editor.text == 'abc'
            assert focused_editor(cancel, expected=editor, insertion='123')
            assert wait_for(lambda: state().get('text') == 'abc123'), state()
            result['native_checked'] = True
        finally:
            cmd.write_text('exit', encoding='utf8')
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=4)
    output.write_text(json.dumps(result, indent=2), encoding='utf8')
    print(result)


if __name__ == '__main__':
    if sys.argv[1] == '--fixture':
        raise SystemExit(fixture(Path(sys.argv[2])))
    verify(Path(sys.argv[1]))
