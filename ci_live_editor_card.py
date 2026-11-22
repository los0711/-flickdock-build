"""Verify the inactive translation card against a separate native editor."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event
from time import monotonic

from PySide6.QtCore import QPoint, QRect, QTimer
from PySide6.QtWidgets import QApplication, QWidget

from ci_live_editor_native import wait_for
from clipboard_history_native import front_target
from live_translation import FOOTER_HEIGHT, LiveEditorTranslation
from live_translation_editor import focused_editor
from translator_store import TranslatorStore


class FixedTranslationService:
    def __init__(self):
        self.calls = []

    def request(self, _method, route, finish, *, data, **_options):
        if route == '/v1/align':
            empty=dict(protocol_version='1',offset_unit='unicode_codepoint',model_version='fixture',
                       alignment_version='1', alignments=[],reverse_alignments=[])
            QTimer.singleShot(50, lambda: finish(empty, None))
            return object()
        self.calls.append(monotonic())
        reply = dict(operation=data['operation'], units=[
            dict(id=row['id'], text='Please confirm the shipping address.')
            for row in data['units']
        ])
        QTimer.singleShot(50, lambda: finish(reply, None))
        return object()

    def cancel(self, _request):
        pass


def pointer(point, click=False):
    x, y = point.toTuple()
    if sys.platform == 'darwin':
        import Quartz
        kinds = [Quartz.kCGEventMouseMoved]
        if click:
            kinds.extend((Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp))
        for kind in kinds:
            event = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), Quartz.kCGMouseButtonLeft)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    else:
        user = ctypes.windll.user32
        assert user.SetCursorPos(x, y)
        if click:
            user.mouse_event(0x0002, 0, 0, 0, 0)
            user.mouse_event(0x0004, 0, 0, 0, 0)


def verify(report, folder):
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    if sys.platform == 'darwin':
        from platform_native import configure_menu_bar_application
        configure_menu_bar_application()
    # A previously visible FlickDock window must never retake focus on click.
    translator = QWidget()
    translator.setWindowTitle('Previously used translator')
    translator.setGeometry(180, 40, 460, 280)
    translator.show()
    translator.activateWindow()
    app.processEvents()
    translator.hide()
    store = TranslatorStore(folder, autoload=False)
    service = FixedTranslationService()
    latencies = []
    failures = []

    def probe(cancel, **options):
        start = monotonic()
        value = focused_editor(cancel, **options)
        if value is None and sys.platform == 'darwin' and not options and len(failures) < 4:
            from live_translation_editor import _mac_editor
            _mac_editor(cancel,None,None,False,rejected=failures.append)
        if value is not None and not options:
            latencies.append(round(1000 * (monotonic() - start), 1))
        return value

    live = LiveEditorTranslation(service, store, probe=probe)
    script = Path(__file__).with_name('ci_live_editor_native.py')
    child = subprocess.Popen([sys.executable, str(script), '--fixture', str(folder)])
    command = folder / 'command'

    def state():
        try:
            return json.loads((folder / 'state.json').read_bytes())
        except (OSError, ValueError):
            return {}

    def observed(predicate):
        app.processEvents()
        return predicate()

    try:
        assert wait_for(lambda: observed(lambda: state().get('active')))
        target = front_target()
        assert (target if sys.platform == 'darwin' else target[1]) == child.pid
        initial = monotonic()
        command.write_text('card', encoding='utf-8')
        assert wait_for(lambda: observed(lambda: state().get('text') == '请确认收货地址。'))
        live.start('en')
        result = wait_for(lambda: observed(lambda: live.draft.ready and live.editor is not None))
        if not result:
            report.update(focused=front_target()==target, success_probes=latencies, rejected=failures,
                          editor_length=len(live.editor.text) if live.editor is not None else None,
                          request_count=len(service.calls), draft_error=live.draft.error)
        assert result, report
        report['first_request_ms'] = round((service.calls[0] - initial) * 1000)
        report['first_result_ms'] = round((monotonic() - initial) * 1000)
        card = live.card
        bounds = QRect(*live.editor.bounds)
        report['editor_probe_ms'] = latencies
        pointer(bounds.center())
        assert wait_for(lambda: observed(lambda: card.motion.values['y'] == 0 and card.footer_clip.isHidden()))
        frame = card.geometry()
        assert not frame.intersects(bounds)
        body = card.mapToGlobal(QPoint(35, card.surface.height() // 2))
        pointer(body)
        report['hover_opens'] = bool(wait_for(lambda: observed(
            lambda: card.footer_clip.height() == FOOTER_HEIGHT), seconds=2))
        pointer(body, click=True)
        wait_for(lambda: observed(lambda: False), seconds=.25)
        report['body_preserves_focus'] = front_target() == target and not translator.isVisible()
        assert report['hover_opens'] and report['body_preserves_focus'], report
        assert card.geometry() == frame
        pointer(card.language.mapToGlobal(card.language.rect().center()), click=True)
        assert wait_for(lambda: observed(card.popup.isVisible)), 'Target language menu did not open'
        report['menu_preserves_focus'] = front_target() == target and not translator.isVisible()
        assert not translator.isVisible(), report
        card.popup.hide()
        pointer(bounds.center())
        assert wait_for(lambda: observed(card.footer_clip.isHidden))
        assert wait_for(lambda: observed(lambda: front_target() == target)), report
        pointer(body)
        assert wait_for(lambda: observed(lambda: card.footer_clip.height() == FOOTER_HEIGHT))
        assert wait_for(lambda: observed(card.insert.isEnabled))
        pointer(card.insert.mapToGlobal(card.insert.rect().center()), click=True)
        report['inserted'] = bool(wait_for(lambda: observed(
            lambda: state().get('text') == 'Please confirm the shipping address.'), seconds=4))
        assert report['inserted'] and front_target() == target and not translator.isVisible(), report
    finally:
        live.stop()
        live.pool.waitForDone(2000)
        store.close()
        command.write_text('exit', encoding='utf-8')
        try:
            child.wait(timeout=6)
        except subprocess.TimeoutExpired:
            child.terminate()
            child.wait(timeout=5)


if __name__ == '__main__':
    if os.environ.get('GITHUB_ACTIONS') != 'true' or sys.platform not in ('darwin', 'win32'):
        raise RuntimeError('Run the native mouse and focus test on an isolated CI desktop.')
    result = dict(platform=sys.platform)
    try:
        with tempfile.TemporaryDirectory(prefix='flickdock-card-native-') as scratch:
            verify(result, Path(scratch))
    finally:
        Path(sys.argv[1]).write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps(result), flush=True)
