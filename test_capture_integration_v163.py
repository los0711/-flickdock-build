"""Real MIME intake -> owned copy/reference -> silent native media -> clear."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['QT_SCALE_FACTOR'] = '1.25'
os.environ['QT_MEDIA_BACKEND'] = 'windows'

from contextlib import contextmanager
import hashlib
from pathlib import Path
import shutil
import tempfile
import threading
import time
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QMimeData, QPoint, QTimer, QUrl
from PySide6.QtWidgets import QApplication, QSystemTrayIcon


def wait_until(predicate, timeout=8):
    started = time.monotonic()
    while not predicate() and time.monotonic() - started < timeout:
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        loop = QEventLoop();QTimer.singleShot(5, loop.quit);loop.exec()
    assert predicate(), 'Intake integration condition timed out'


def mime(path):
    value = QMimeData();value.setUrls([QUrl.fromLocalFile(str(path))]);return value


with tempfile.TemporaryDirectory(prefix='flickdock-copy-integration-') as temporary:
    root = Path(temporary);os.environ['APPDATA'] = str(root / 'profile')
    import app_settings
    import capture_files
    import flickdock as subject
    import media_preview
    from global_hotkeys import _NoopBackend
    app = QApplication([]);subject.load_font(app)
    fixture = Path(os.getenv('FLICKDOCK_TEST_MEDIA', str(Path(__file__).parent / 'media-test.mp4')))
    if not fixture.is_file():
        print('SKIP native intake media integration: set FLICKDOCK_TEST_MEDIA to a test video')
        raise SystemExit(0)
    store = root / 'captured';store.mkdir()
    with patch.object(subject, 'motion_enabled', return_value=False), \
         patch.object(QSystemTrayIcon, 'show'), patch.object(QSystemTrayIcon, 'hide'), \
         patch.object(subject.ctypes.windll.user32, 'GetAsyncKeyState', return_value=0), \
         patch.object(subject.ctypes.windll.user32, 'RegisterHotKey', side_effect=AssertionError('Native hotkey registration')), \
         patch.object(subject.ctypes.windll.user32, 'UnregisterHotKey', side_effect=AssertionError('Native hotkey unregistration')), \
         patch.object(subject, 'native_window_move_active', return_value=False), \
         patch.object(subject.QCursor, 'pos', return_value=QPoint(20, 20)), \
         patch.object(app_settings, '_read_startup_value'), patch.object(app_settings, '_write_startup_value'):
        manager = subject.ShelfManager(app);manager.timer.stop();shelf = manager.shelf;shelf.pinned = True
        assert isinstance(manager.hotkeys.backend, _NoopBackend)
        assert manager.settings.capture_mode == 'reference'
        manager.settings.update(capture_mode='copy', capture_dir=str(store))
        # Exercise the production 20 ms poll while disk I/O is still pending.
        polled_source = root / 'polled.bin';polled_source.write_bytes(b'poll remains active')
        original_copy = capture_files._copy_file
        entered, release = threading.Event(), threading.Event()
        def held_copy(*args):
            entered.set();assert release.wait(5);return original_copy(*args)
        waits, beats = [], []
        def watched_wait(job, timeout_ms=-1):
            waits.append(timeout_ms)
            return False
        with patch.object(capture_files, '_copy_file', side_effect=held_copy):
            assert shelf.receive_mime('permanent', mime(polled_source))
            wait_until(entered.is_set)
            epoch = shelf._copy_epoch
            heartbeat = QTimer();heartbeat.setInterval(16);heartbeat.timeout.connect(lambda: beats.append(time.perf_counter()));heartbeat.start()
            threading.Timer(.25, release.set).start()
            with patch.object(capture_files.CopyFilesJob, 'wait', watched_wait):
                manager.timer.start(20)
                wait_until(lambda: not shelf.copy_jobs)
                manager.timer.stop()
            heartbeat.stop()
            assert not waits and shelf._copy_epoch == epoch
            assert len(beats) >= 5 and len(shelf.files['permanent']) == 1
            assert Path(shelf.files['permanent'][0]).read_bytes() == polled_source.read_bytes()
        shelf.remove_all('permanent')
        print('PASS live 20 ms manager poll keeps copy running, makes no GUI wait call and leaves heartbeat active')
        source = root / 'original-copy.mp4';shutil.copyfile(fixture, source)
        fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
        assert shelf.receive_mime('permanent', mime(source))
        wait_until(lambda: not shelf.copy_jobs and bool(shelf.files['permanent']))
        copied = Path(shelf.files['permanent'][0])
        assert copied != source and copied.is_relative_to(store / 'imports')
        assert hashlib.sha256(copied.read_bytes()).hexdigest() == fingerprint
        assert source.is_file()
        source.unlink()
        shelf._show_at(QPoint(60, 80), 'manual', QPoint())
        shelf.show_contents();viewer = shelf.popup.preview.media;viewer.toggle_mute()
        wait_until(lambda: not viewer.surface.image.isNull() or viewer._failed)
        assert not viewer._failed, viewer.status_label.toolTip()
        assert viewer.path == str(copied) and viewer.audio_output.isMuted()
        viewer.toggle_playback()
        wait_until(lambda: viewer._reported_playing)
        assert viewer.audio_output.isMuted() and not source.exists()
        viewer.stop()
        print('PASS actual MIME copy intake keeps original unchanged; after original deletion owned MP4 previews and plays silently')

        shelf.remove_all('permanent')
        manager.settings.update(capture_mode='reference')
        reference = root / 'original-reference.mp4';shutil.copyfile(fixture, reference)
        assert shelf.receive_mime('permanent', mime(reference))
        assert shelf.files['permanent'] == [str(reference)] and not shelf.copy_jobs
        shelf._show_at(QPoint(60, 80), 'manual', QPoint());shelf.show_contents()
        viewer = shelf.popup.preview.media
        wait_until(lambda: viewer.path == str(reference) and (not viewer.surface.image.isNull() or viewer._failed))
        assert not viewer._failed
        viewer.stop()
        def delete_reference():
            try:reference.unlink(missing_ok=True);return True
            except PermissionError:return False
        wait_until(delete_reference, 3)
        with patch.object(media_preview, 'QMediaPlayer', side_effect=AssertionError('Deleted reference reopened native player')):
            viewer.toggle_playback()
            shelf.popup.preview.show_path(str(reference), force=True)
            assert viewer.player is None and viewer._failed and '删除' in viewer.status_label.text()
            assert viewer.surface.image.isNull() and not viewer.surface.fallback.isNull()
        print('PASS default reference intake keeps exact source path; deletion fails inline without reopening native media')

        shelf.remove_all('permanent');manager.settings.update(capture_mode='copy')
        large = root / 'large.bin';large.write_bytes(bytes(range(256)) * 65536)
        before = set((store / 'imports').iterdir())
        entered, release = threading.Event(), threading.Event()
        original_reader = capture_files._source_reader
        class Reader:
            def __init__(self, stream):self.stream = stream;self.calls = 0
            def read(self, count):
                self.calls += 1
                if self.calls == 2:entered.set();assert release.wait(5)
                return self.stream.read(count)
        @contextmanager
        def held_reader(path):
            with original_reader(path) as stream:yield Reader(stream)
        with patch.object(capture_files, '_source_reader', side_effect=held_reader):
            assert shelf.receive_mime('permanent', mime(large))
            wait_until(entered.is_set)
            assert shelf.copy_jobs and not shelf.files['permanent']
            shelf.remove_all('permanent');release.set()
            wait_until(lambda: not shelf.copy_jobs)
            assert not shelf.files['permanent'] and set((store / 'imports').iterdir()) == before
        assert large.is_file() and large.stat().st_size == 16 * 1024 * 1024
        print('PASS clear during a partially copied 16 MiB file cancels, cleans this job and keeps originals')

        late = root / 'late.txt';late.write_text('late success must not restore cleared refs')
        assert shelf.receive_mime('permanent', mime(late))
        job = next(iter(shelf.copy_jobs))
        # The worker has succeeded, but its finished signal is still queued.
        assert job.wait(3000)
        shelf.remove_all('permanent')
        wait_until(lambda: not shelf.copy_jobs)
        assert not shelf.files['permanent']
        print('PASS success queued before clear cannot resurrect any reference afterward')
        shelf.remove_all('permanent');manager.quit()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
print('CAPTURE_INTEGRATION_V163_OK')
