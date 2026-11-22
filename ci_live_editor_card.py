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

from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QCursor
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
        assert Quartz.CGWarpMouseCursorPosition((x, y)) == 0
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
    if sys.platform == 'darwin':
        from platform_native import configure_media_backend
        configure_media_backend()
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
    updates = []

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
    observed_live = live._observed
    def log_observed(value, error, sampling, checking_empty=False):
        observed_live(value,error,sampling,checking_empty)
        if len(updates)<18:
            updates.append(dict(observed=value is not None, active=front_target(),
                                visible=live.card.isVisible(), ready=live.draft.ready))
    live._observed = log_observed
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
        assert wait_for(lambda: observed(lambda: card.motion.values['y'] == 0
            and card.motion.values['opacity'] == 1 and card.footer_clip.isHidden()))
        frame = card.screen_frame()
        assert not frame.intersects(bounds)
        body = card.screen_point(card.surface,QPoint(35,card.surface.height() // 2))
        pointer(body)
        report['hover_opens'] = bool(wait_for(lambda: observed(
            lambda: card.footer_clip.height() == FOOTER_HEIGHT), seconds=2))
        if not report['hover_opens']:
            report.update(cursor=QCursor.pos().toTuple(), requested=body.toTuple(),
                          surface=card.surface.geometry().getRect(), window=card.geometry().getRect(),
                          hover_timer=card._hover_timer.isActive(), local=card.mapFromGlobal(QCursor.pos()).toTuple(),
                          extent=card.footer_clip.height(), footer_open=card._footer_open,
                          cocoa=card._cocoa_card, visible=card.isVisible(), probe_rejections=failures, updates=updates)
        if sys.platform == 'darwin' and card._cocoa_host is not None:
            import Quartz
            native=card._cocoa_host.panel
            panel_frame=native.frame()
            report.update(pointer_accepted=not native.ignoresMouseEvents(), native_visible=native.isVisible(),
                          native_frame=(panel_frame.origin.x,panel_frame.origin.y,panel_frame.size.width,panel_frame.size.height),
                          native_level=native.level(),native_alpha=native.alphaValue())
            point=body.toTuple()
            windows=Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly,Quartz.kCGNullWindowID)
            covering=[]
            for value in windows:
                window_bounds=value.get(Quartz.kCGWindowBounds,{})
                if window_bounds.get('X',0)<=point[0]<window_bounds.get('X',0)+window_bounds.get('Width',0) and (
                        window_bounds.get('Y',0)<=point[1]<window_bounds.get('Y',0)+window_bounds.get('Height',0)):
                    covering.append((value.get(Quartz.kCGWindowNumber),value.get(Quartz.kCGWindowOwnerName),
                                     value.get(Quartz.kCGWindowOwnerPID),value.get(Quartz.kCGWindowLayer)))
            report.update(hits=covering,panel_number=native.windowNumber(),body=point)
            subprocess.run(['/usr/sbin/screencapture','-x','native-card.png'],check=False,timeout=6)
        pointer(body, click=True)
        wait_for(lambda: observed(lambda: False), seconds=.25)
        report['body_preserves_focus'] = front_target() == target and not translator.isVisible()
        if sys.platform == 'darwin' and not report['body_preserves_focus']:
            import AppKit
            import objc
            native=objc.objc_object(c_void_p=ctypes.c_void_p(int(card.winId()))).window()
            active=AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
            report.update(before_target=target, after_target=front_target(), active_process=active.processIdentifier(),
                          own_pid=os.getpid(), is_native_host=bool(card._cocoa_host and native==card._cocoa_host.panel),
                          panel_class=native.className(), panel_mask=int(native.styleMask()),
                          app_key=bool(native.isKeyWindow()), card_visible=card.isVisible(),
                          translator_visible=translator.isVisible(), updates=updates)
        assert report['hover_opens'] and report['body_preserves_focus'], report
        assert card.screen_frame() == frame
        pointer(card.screen_point(card.language,card.language.rect().center()),click=True)
        assert wait_for(lambda: observed(card.popup.isVisible)), 'Target language menu did not open'
        report['menu_preserves_focus'] = front_target() == target and not translator.isVisible()
        assert not translator.isVisible(), report
        menu=card.popup._passive_frame if card.popup._passive_host is not None else card.popup.geometry()
        anchor=QRect(card.screen_point(card.language,QPoint()),card.language.size())
        report['menu_position']=menu.getRect()
        assert card.anchor_area.contains(menu) and not menu.intersects(bounds) and not menu.intersects(anchor), report
        assert menu.left()<=anchor.right() and menu.right()>=anchor.left(), report
        if sys.platform=='darwin':
            assert card.popup._passive_host.panel.level()>card._cocoa_host.panel.level(), report
        choices=card.popup.list
        option=next(choices.item(index) for index in range(choices.count()) if choices.item(index).data(Qt.UserRole)=='ja')
        choices.scrollToItem(option)
        observed(lambda: True)
        option_rect=choices.visualItemRect(option)
        local=QPoint(option_rect.left()+45,option_rect.center().y())
        point=(menu.topLeft()+choices.viewport().mapTo(card.popup,local) if card.popup._passive_host is not None
               else choices.viewport().mapToGlobal(local))
        pointer(point,click=True)
        assert wait_for(lambda: observed(lambda: not card.popup.isVisible() and live.draft.target=='ja')), report
        pointer(bounds.center())
        assert wait_for(lambda: observed(card.footer_clip.isHidden))
        assert wait_for(lambda: observed(lambda: front_target() == target)), report
        pointer(body)
        assert wait_for(lambda: observed(lambda: card.footer_clip.height() == FOOTER_HEIGHT))
        assert wait_for(lambda: observed(card.insert.isEnabled))
        pointer(card.screen_point(card.insert,card.insert.rect().center()),click=True)
        report['inserted'] = bool(wait_for(lambda: observed(
            lambda: state().get('text') == 'Please confirm the shipping address.'), seconds=4))
        assert report['inserted'] and front_target() == target and not translator.isVisible(), report
        if sys.platform == 'darwin':
            from AppKit import NSWorkspace
            path=folder/'FlickDock verification document.txt'
            path.write_text('这个测试文档应排除在自动翻译之外。',encoding='utf-8')
            subprocess.run(['/usr/bin/open','-a','TextEdit',str(path)],check=True,timeout=8)
            assert wait_for(lambda: observed(lambda:
                NSWorkspace.sharedWorkspace().frontmostApplication().bundleIdentifier()=='com.apple.TextEdit'),
                seconds=8), 'The actual TextEdit document did not come to the foreground'
            assert focused_editor(Event()) is None, 'TextEdit was eligible for automatic translation'
            report['native_document_excluded']=True
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
