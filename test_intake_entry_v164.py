"""Real manager/drop intake lifecycle; no desktop, registry or native input."""
import contextlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['QT_SCALE_FACTOR'] = '1.25'

from PySide6.QtCore import QEventLoop, QMimeData, QObject, QPoint, QPointF, QRect, QTimer, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QSystemTrayIcon


class Screen:
    def availableGeometry(self): return QRect(0, 0, 1920, 1080)
    def devicePixelRatio(self): return 1.25


class LocalResolver(QObject):
    def __init__(self, *args, parent=None, **kwargs): super().__init__(parent)
    @property
    def pending_refs(self): return frozenset()
    def candidates(self, mime): return []
    def cancel(self, ref): pass
    def cancel_all(self): pass
    def close(self): pass


def run_for(milliseconds):
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


def until(condition, seconds=6):
    deadline = time.monotonic()+seconds
    while not condition() and time.monotonic()<deadline: run_for(10)
    assert condition(), 'Intake entry did not reach its expected result'


def mime_for(path):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path))])
    return mime


failures = []
checks = []
def check(name, operation):
    selected = os.getenv('FLICKDOCK_INTAKE_TEST_ONLY', '')
    if selected and selected not in name: return
    try:
        operation()
    except BaseException as error:
        failures.append((name, repr(error)))
        checks.append({'name':name,'passed':False,'error':repr(error)})
        print('FAIL', name, repr(error), flush=True)
    else:
        checks.append({'name':name,'passed':True})
        print('PASS', name, flush=True)


with tempfile.TemporaryDirectory(prefix='flickdock-intake-entry-v164-') as temporary:
    root = Path(temporary)
    os.environ['APPDATA'] = str(root/'profile')
    import app_settings
    import capture_files
    import flickdock as subject
    from global_hotkeys import _NoopBackend
    app = QApplication([])
    subject.load_font(app)
    with contextlib.ExitStack() as mocks:
        for owner, name, value in [
            (subject, 'motion_enabled', lambda: True),
            (subject, 'ContentResolver', LocalResolver),
            (subject.QCursor, 'pos', lambda *args: QPoint(500, 400)),
            (subject.QGuiApplication, 'screenAt', lambda *args: Screen()),
            (subject.QGuiApplication, 'primaryScreen', lambda *args: Screen()),
            (subject.ctypes.windll.user32, 'GetAsyncKeyState', lambda key: 0),
            (subject, 'native_window_move_active', lambda: False),
            (app_settings, '_read_startup_value', lambda: None),
            (app_settings, '_write_startup_value', lambda *args: None),
            (QSystemTrayIcon, 'show', lambda self: None),
            (QSystemTrayIcon, 'hide', lambda self: None),
        ]: mocks.enter_context(patch.object(owner, name, value))
        for name in ('RegisterHotKey', 'UnregisterHotKey'):
            mocks.enter_context(patch.object(subject.ctypes.windll.user32, name,
                side_effect=AssertionError('Native hotkey registration is forbidden')))
        manager = subject.ShelfManager(app)
        manager.timer.stop()
        shelf = manager.shelf
        assert isinstance(manager.hotkeys.backend, _NoopBackend)
        mocks.enter_context(patch.object(subject.QCursor, 'pos', lambda *args:
            shelf.visual_geometry().center() if shelf.isVisible() else QPoint(10, 10)))
        mocks.enter_context(patch.object(manager, '_pointer_dragging', return_value=True))
        manager.settings.update(capture_dir=str(root/'saved'))
        sequence = 0

        def source(label):
            global sequence
            sequence += 1
            value = root/f'{sequence:02}-{label}.txt'
            value.write_text(f'Original payload {sequence} for {label}', encoding='utf8')
            return value

        def reset(mode='reference', pinned=False):
            shelf.hide_immediately()
            shelf.remove_all('permanent')
            until(lambda: not shelf.copy_jobs)
            shelf.pinned = pinned
            manager.auto_shown = None
            manager.edge_suppressed = False
            manager.settings.update(capture_mode=mode, trigger_view='compact')
            manager.timer.stop()

        def show(entry):
            if entry=='dock': manager.show_docked()
            elif entry=='drag': manager._content_drag_validated(QPoint(500, 400))
            else: manager.show_overview()
            until(lambda: not shelf.geometry_morph.active)
            run_for(50)
            assert shelf.isVisible()

        def drop(entry, path):
            mime = mime_for(path)
            if entry=='drag':
                assert manager._probe_drop(mime, QPoint(500, 400))
                return
            target = shelf.popup.preview.text.viewport() if entry=='overview' else shelf.zones['permanent']
            enter = QDragEnterEvent(QPoint(5, 5), Qt.DropAction.CopyAction, mime,
                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(target, enter)
            assert enter.isAccepted(), ('enter rejected', entry)
            move = QDragMoveEvent(QPoint(6, 6), Qt.DropAction.CopyAction, mime,
                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(target, move)
            assert move.isAccepted(), ('move rejected', entry)
            event = QDropEvent(QPointF(6, 6), Qt.DropAction.CopyAction, mime,
                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(target, event)
            assert event.isAccepted(), ('drop rejected', entry)

        def normal(entry, mode, pinned=False):
            reset(mode, pinned)
            selected_before = None
            if pinned:
                selected_before = str(source('selected-before-pinned-drop'))
                shelf.add_paths('permanent', [selected_before])
            show(entry)
            if selected_before:
                shelf.popup.select_item(selected_before)
                assert shelf.popup.preview.current_path == selected_before
            original = source(f'{entry}-{mode}')
            pulses, hides = [], []
            zone = shelf.zones['permanent']
            original_pulse = subject.StackTile.pulse
            original_hide = shelf.hide_animated
            def pulse(tile, kind='drop', finished=None):
                if kind=='drop' and tile in (zone.icon, getattr(shelf, '_intake_tile', None)):
                    pulses.append((shelf.expanded, shelf.geometry_morph.active, len(shelf.copy_jobs)))
                return original_pulse(tile, kind, finished)
            def hide(explicit=True):
                hides.append((len(shelf.copy_jobs), shelf.incoming_drop))
                return original_hide(explicit)
            with patch.object(subject.StackTile, 'pulse', pulse), patch.object(shelf, 'hide_animated', side_effect=hide):
                if mode=='copy':
                    release, entered = threading.Event(), threading.Event()
                    copy_file = capture_files._copy_file
                    def held(*args):
                        entered.set(); assert release.wait(4)
                        return copy_file(*args)
                    with patch.object(capture_files, '_copy_file', side_effect=held):
                        try:
                            drop(entry, original)
                            until(entered.is_set)
                            manager.timer.start(20)
                            run_for(180)
                            assert shelf.isVisible() and shelf.visible_state!='hiding'
                            assert shelf.incoming_drop and shelf.copy_jobs and not pulses and not hides
                        finally: release.set()
                        until(lambda: not shelf.copy_jobs)
                else: drop(entry, original)
                if pinned:
                    until(lambda: not shelf.incoming_drop)
                    assert shelf.isVisible() and shelf.visible_state!='hiding' and not hides
                    assert shelf.expanded == (entry=='overview')
                    if selected_before:
                        assert shelf.popup.primary_selection == selected_before
                        assert shelf.popup.preview.current_path == selected_before
                        assert shelf.popup.selected == {selected_before}
                else:
                    until(lambda: not shelf.isVisible())
                    assert hides and all(jobs==0 for jobs, _ in hides)
                manager.timer.stop()
                assert len(pulses)==1, pulses
                if entry=='overview' and not pinned:
                    assert pulses[0][:2] == (False, False), pulses
                assert len(shelf.files['permanent'])==(2 if pinned else 1)
                stored = Path(shelf.files['permanent'][-1])
                assert stored.read_bytes()==original.read_bytes()
                assert (stored==original) == (mode=='reference')

        for mode in ('reference', 'copy'):
            for entry in ('dock', 'drag', 'overview'):
                check(f'{mode}/{entry}: commit then landing then hide; copying holds window open',
                      lambda entry=entry, mode=mode: normal(entry, mode))
            check(f'{mode}/pinned overview: stays expanded and visible', lambda mode=mode: normal('overview', mode, True))

        def invalidation(action, queued_success=False):
            reset('copy')
            show('overview')
            original = source('invalidate-'+action)
            shown = []
            original_show = shelf._show_at
            def show_again(*args, **kwargs):
                shown.append(time.monotonic()); return original_show(*args, **kwargs)
            release, entered = threading.Event(), threading.Event()
            copy_file = capture_files._copy_file
            def held(*args):
                entered.set(); assert release.wait(4); return copy_file(*args)
            with patch.object(capture_files, '_copy_file', side_effect=held), patch.object(shelf, '_show_at', side_effect=show_again):
                try:
                    drop('overview', original)
                    until(entered.is_set)
                    if queued_success:
                        release.set()
                        job = next(iter(shelf.copy_jobs))
                        assert job.wait(2000), 'Fixture copy did not finish before queued signal test'
                    if action=='close': shelf.close_button.click()
                    else: shelf.remove_all('permanent')
                finally: release.set()
                until(lambda: not shelf.copy_jobs)
                run_for(1100)
                assert not shown, shown
                assert not shelf.incoming_drop and not shelf.zones['permanent'].icon._pulse_active
                if action=='close': assert not shelf.isVisible()
                else:
                    assert shelf.isVisible() and shelf.visible_state!='hiding'
                    assert not shelf.files['permanent']

        for action in ('close', 'clear'):
            for queued in (False, True):
                check(f'{action}: late {"queued success" if queued else "copy completion"} cannot reopen or restart landing',
                    lambda action=action, queued=queued: invalidation(action, queued))

        def interrupted_fold():
            reset('reference')
            show('overview')
            drop('overview', source('interrupted-fold'))
            assert not shelf.expanded and shelf.geometry_morph.active
            # The user opens overview again while the intake fold is active.
            manager.show_overview()
            run_for(1300)
            assert not shelf.incoming_drop, ('Fold retarget lost intake completion', shelf.expanded,
                shelf.geometry_morph.active, shelf.zones['permanent'].icon._pulse_active)
        check('overview retarget during intake fold cannot strand incoming_drop forever', interrupted_fold)

        def overlapping_copies():
            reset('copy')
            show('dock')
            first, second = source('held-first'), source('fast-second')
            release, entered = threading.Event(), threading.Event()
            copy_file = capture_files._copy_file
            def held(path, *args):
                if Path(path)==first:
                    entered.set(); assert release.wait(5)
                return copy_file(path, *args)
            with patch.object(capture_files, '_copy_file', side_effect=held):
                try:
                    drop('dock', first)
                    until(entered.is_set)
                    drop('dock', second)
                    until(lambda: len(shelf.copy_jobs)==1 and len(shelf.files['permanent'])==1)
                    run_for(1100)
                    assert shelf.isVisible() and shelf.visible_state!='hiding', (
                        'Second intake hid while first copy still pending', len(shelf.copy_jobs), shelf.visible_state)
                finally: release.set()
                until(lambda: not shelf.copy_jobs)
        check('overlapping copies cannot auto-hide before all pending intake data is committed', overlapping_copies)

        def cancelled_feedback():
            reset('reference')
            show('dock')
            drop('dock', source('cancel-feedback'))
            shelf.remove_all('permanent')
            run_for(850)
            tile = shelf.zones['permanent'].icon
            assert not shelf.files['permanent'] and shelf.isVisible()
            assert not tile._pulse_active
            assert tile.spring.values['check'] < .005, ('Cancelled intake retained green success badge on empty tile',
                tile.spring.values['check'], tile._kind)
        check('cancelled feedback cannot leave a success badge on the empty shelf', cancelled_feedback)

        def released_before_drop(mode):
            # A native mouse-up sample can beat the OLE DropEvent by one poll.
            # The shake-opened shelf is still visible but already hiding.
            reset(mode)
            show('drag')
            manager.auto_shown = None
            shelf.hide_animated(explicit=False)
            assert shelf.isVisible() and shelf.visible_state=='hiding'
            original=source('released-before-drop-'+mode)
            if mode=='copy':
                release,entered=threading.Event(),threading.Event()
                copy_file=capture_files._copy_file
                def held(*args):
                    entered.set();assert release.wait(4)
                    return copy_file(*args)
                with patch.object(capture_files,'_copy_file',side_effect=held):
                    try:
                        drop('dock',original)
                        until(entered.is_set)
                        run_for(250)
                        assert shelf.isVisible() and shelf.visible_state=='floating', (
                            'Accepted drop disappeared while copying',shelf.visible_state)
                    finally:release.set()
                    until(lambda:not shelf.copy_jobs)
            else:drop('dock',original)
            assert shelf.visible_state=='floating' and shelf.incoming_drop, (
                'Accepted drop kept the stale hiding animation',shelf.visible_state)
            tile=shelf.zones['permanent'].icon
            assert tile._pulse_active and tile.landing.state().name=='Running', (
                'Accepted drop lost its visible landing',tile._pulse_active,tile.landing.state())
            frames=[]
            tile.landing.valueChanged.connect(lambda value:frames.append(float(value)))
            run_for(80)
            assert shelf.isVisible() and shelf.windowOpacity()>.1 and any(.01<value<1.2 for value in frames), (
                'Landing did not produce visible intermediate frames',shelf.windowOpacity(),frames)
            until(lambda:not shelf.isVisible())
            assert len(shelf.files['permanent'])==1
        for mode in ('reference','copy'):
            check(f'{mode}/drag: mouse-up before OLE drop preserves visible landing',
                  lambda mode=mode:released_before_drop(mode))

        reset()
        manager.quit()
        run_for(20)

Path(__file__).with_name('intake-entry-v164.json').write_text(json.dumps(
    {'platform':'offscreen','checks':checks,'ok':not failures},ensure_ascii=False,indent=2),encoding='utf8')
if failures:
    raise AssertionError(f'{len(failures)} intake regressions: {failures!r}')
print('INTAKE_ENTRY_V164_OK')
