"""Measure the real clear-button/child-event-loop path without desktop I/O.

Run with bundled Python and PYTHONPATH=py;.; writes isolated local files/profile.
Native cursor reads, startup registry, hotkeys, tray, resolver are mocked.
Only this independent benchmark/report is written; production modules untouched.
"""
import contextlib
import json
import os
from pathlib import Path
import statistics
import tempfile
import time
import wave
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['QT_SCALE_FACTOR'] = '1.25'

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QObject, QPoint, QRect, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSystemTrayIcon


class Screen:
    def availableGeometry(self): return QRect(0, 0, 1920, 1080)
    def devicePixelRatio(self): return 1.25


class LocalResolver(QObject):
    def __init__(self, *args, parent=None, **kwargs): super().__init__(parent)
    @property
    def pending_refs(self): return frozenset()
    def cancel(self, ref): pass
    def cancel_all(self): pass
    def close(self): pass


class ObservedApplication(QApplication):
    measurement = None
    def notify(self, watched, event):
        measured = self.measurement
        if measured is None or not measured.active or event.type() != QEvent.Type.DeferredDelete:
            return super().notify(watched, event)
        start = time.perf_counter()
        try: return super().notify(watched, event)
        finally:
            measured.metrics.setdefault('deferred_delete_notify', []).append((time.perf_counter()-start)*1000)


class Measurement(QObject):
    def __init__(self, app):
        super().__init__()
        self.active = False
        self.metrics = {}
        self.beats = []
        self.paint_times = []
        self.last = time.perf_counter()
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(4)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        app.installEventFilter(self)

    def tick(self):
        now = time.perf_counter()
        if self.active: self.beats.append((now-self.last)*1000)
        self.last = now

    def eventFilter(self, watched, event):
        if self.active and event.type() == QEvent.Type.Paint and watched is self.card:
            self.paint_times.append(time.perf_counter())
        return False

    def begin(self, card):
        self.metrics = {}
        self.beats = []
        self.paint_times = []
        self.card = card
        self.active = True
        self.last = time.perf_counter()

    def wrap(self, original, label):
        def wrapped(*args, **kwargs):
            start = time.perf_counter()
            try: return original(*args, **kwargs)
            finally:
                if self.active:
                    self.metrics.setdefault(label, []).append((time.perf_counter()-start)*1000)
        return wrapped


def flush(app):
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def wait_until(app, condition, timeout=5):
    deadline = time.perf_counter()+timeout
    while time.perf_counter()<deadline and not condition():
        app.processEvents(); QTest.qWait(5)
    assert condition(), 'UI did not settle'
    QTest.qWait(60); flush(app)


with tempfile.TemporaryDirectory(prefix='flickdock-clear-bench-') as temporary:
    os.environ['APPDATA'] = str(Path(temporary)/'profile')
    import flickdock as subject
    import app_settings
    from confirm_clear import ClearConfirmOverlay, confirmation_active
    from global_hotkeys import _NoopBackend
    from media_preview import MediaPreview
    from shelf_content import ContentRepository
    from content_grid import IconItem

    app = ObservedApplication([])
    subject.load_font(app)
    measurement = Measurement(app)
    app.measurement = measurement
    with contextlib.ExitStack() as stack:
        for owner, name, value in [
            (subject, 'motion_enabled', lambda: True),
            (subject, 'ContentResolver', LocalResolver),
            (subject.QCursor, 'pos', lambda *args: QPoint(1400, 350)),
            (subject.QGuiApplication, 'screenAt', lambda *args: Screen()),
            (subject.QGuiApplication, 'primaryScreen', lambda *args: Screen()),
            (subject.ctypes.windll.user32, 'GetAsyncKeyState', lambda key: 0),
            (subject, 'native_window_move_active', lambda: False),
            (app_settings, '_read_startup_value', lambda: None),
            (app_settings, '_write_startup_value', lambda *args: None),
            (QSystemTrayIcon, 'show', lambda self: None),
            (QSystemTrayIcon, 'hide', lambda self: None),
        ]: stack.enter_context(patch.object(owner, name, value))
        for owner, name, label in [
            (ClearConfirmOverlay, '_finish', 'overlay_finish'),
            (subject.ShelfWindow, 'remove_all', 'remove_all'),
            (subject.ShelfWindow, 'refresh', 'refresh'),
            (subject.ContentsPopup, 'rebuild', 'popup_rebuild'),
            (subject.ContentsPopup, '_clear_layout', 'clear_layout'),
            (MediaPreview, 'release_source', 'release_media'),
            (MediaPreview, 'stop', 'stop_media'),
            (subject.ShelfManager, 'save', 'save'),
            (ContentRepository, 'prune', 'prune'),
        ]: stack.enter_context(patch.object(owner, name, measurement.wrap(getattr(owner, name), label)))

        variant = os.getenv('FLICKDOCK_CLEAR_BENCH_VARIANT', 'natural-baseline')
        media_case = bool(os.getenv('FLICKDOCK_CLEAR_BENCH_MEDIA'))
        if media_case:
            def forbid_play(self): raise AssertionError('Media benchmark must not play audio')
            import media_preview
            stack.enter_context(patch.object(media_preview.QMediaPlayer, 'play', forbid_play))
        if variant == 'batch':
            retired = []
            cleanup = QTimer()
            cleanup.setSingleShot(True)
            def retire_batch():
                for _ in range(min(12, len(retired))): retired.pop().deleteLater()
                if retired: cleanup.start(8)
            cleanup.timeout.connect(retire_batch)
            def clear_in_batches(self, layout):
                while layout.count():
                    item = layout.takeAt(0)
                    if item.widget():
                        item.widget().hide(); retired.append(item.widget())
                    elif item.layout():
                        clear_in_batches(self, item.layout()); item.layout().deleteLater()
                if retired and not cleanup.isActive(): cleanup.start(8)
            stack.enter_context(patch.object(subject.ContentsPopup, '_clear_layout', measurement.wrap(clear_in_batches, 'clear_layout_batch')))

        manager = subject.ShelfManager(app)
        manager.timer.stop()
        shelf = manager.shelf
        shelf.pinned = True
        assert isinstance(manager.hotkeys.backend, _NoopBackend)
        paths = []
        for index in range(250):
            path = Path(temporary)/f'original-{index:04}.txt'
            path.write_text(f'Local clear benchmark original {index}', encoding='utf8')
            paths.append(str(path))
        if media_case:
            audio_path = Path(temporary)/'silent-source.wav'
            with wave.open(str(audio_path), 'wb') as wav:
                wav.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
                wav.writeframes(b'\0\0'*8000)
            paths[124] = str(audio_path)

        results = []
        cases = [(125, 'list'), (125, 'icons')] if media_case else [(125, 'list'), (125, 'icons'), (250, 'list'), (250, 'icons')]
        for count, view in cases:
            for repeat in range(3):
                manager.settings.update(view_mode=view)
                shelf.add_paths('permanent', paths[:count])
                manager.show_overview()
                popup = shelf.popup
                item_class = subject.FileRow if view == 'list' else IconItem
                wait_until(app, lambda: len(popup.rows_body.findChildren(item_class)) == count and not shelf.geometry_morph.active)
                if media_case:
                    popup.preview.current_path = None
                    popup.preview.show_path(paths[124])
                    wait_until(app, lambda: popup.preview.media._loaded)
                    assert popup.preview.media.player is not None
                    assert popup.preview.media.audio_output.isMuted()
                assert abs(shelf.devicePixelRatioF()-1.25)<.01
                old_items = popup.rows_body.findChildren(item_class)
                destroyed_at = []
                for item in old_items: item.destroyed.connect(lambda _=None: destroyed_at.append(time.perf_counter()))
                case = {'count': count, 'view': view, 'repeat': repeat, 'dpr': shelf.devicePixelRatioF(), 'media_source_loaded': media_case}

                def invoke(accept):
                    measurement.begin(shelf.card)
                    click = {}
                    outer = QEventLoop()
                    def interact():
                        overlay = next(item for item in shelf.findChildren(ClearConfirmOverlay) if item.active)
                        measurement.beats = []; measurement.last = time.perf_counter()
                        click['start'] = time.perf_counter()
                        (overlay.delete_button if accept else overlay.cancel_button).click()
                        click['end'] = time.perf_counter()
                    def entry():
                        QTimer.singleShot(30, interact)
                        click['entry_start'] = time.perf_counter()
                        popup.clear_button.click()
                        click['returned'] = time.perf_counter()
                        QTimer.singleShot(350, outer.quit)
                    QTimer.singleShot(0, entry)
                    outer.exec()
                    measurement.active = False
                    return {
                        'entry_until_return_ms': (click['returned']-click['entry_start'])*1000,
                        'delete_click_ms': (click['end']-click['start'])*1000,
                        'after_click_to_return_ms': (click['returned']-click['end'])*1000,
                        'first_card_paint_after_click_ms': next(((stamp-click['start'])*1000 for stamp in measurement.paint_times if stamp>=click['start']), None),
                        'heartbeat_max_ms': max(measurement.beats, default=0),
                        'metrics_ms': {label: {'sum': sum(values), 'max': max(values), 'calls': len(values)} for label, values in measurement.metrics.items()},
                    }

                case['cancel'] = invoke(False)
                assert len(shelf.files['permanent']) == count
                case['delete'] = invoke(True)
                case['destroyed_rows'] = len(destroyed_at)
                assert not shelf.files['permanent'] and not confirmation_active(shelf)
                assert json.loads(subject.STATE_FILE.read_text(encoding='utf8'))['shelf']['permanent'] == []
                assert all(Path(path).is_file() for path in paths)
                assert len(destroyed_at) == count
                results.append(case)
                print(json.dumps(case), flush=True)
        output = Path(__file__).with_name(f'clear-confirm-{variant}{"-media" if media_case else ""}.json')
        output.write_text(json.dumps(results, indent=2), encoding='utf8')
        manager.quit(); flush(app)
        print('CLEAR_CONFIRM_BENCH_OK', str(output))
