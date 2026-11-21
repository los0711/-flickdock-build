"""125% offscreen image loading, cancellation, selection and Windows sharing."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['QT_SCALE_FACTOR'] = '1.25'

from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEventLoop, QThread, QTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

import async_images as subject
from image_preview import ImagePreview
from shelf_content import ContentRepository


def wait_until(predicate, timeout=5):
    started = time.monotonic()
    while not predicate() and time.monotonic() - started < timeout:
        loop = QEventLoop()
        QTimer.singleShot(5, loop.quit)
        loop.exec()
    assert predicate(), 'Asynchronous image request did not complete'


def settle(milliseconds=80):
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


app = QApplication([])
with tempfile.TemporaryDirectory(prefix='flickdock-images-test-') as temporary:
    root = Path(temporary)
    red, blue = root / 'red.jpg', root / 'blue.png'
    image = QImage(3000, 2000, QImage.Format.Format_RGB32)
    image.fill(QColor('#c93226'))
    assert image.save(str(red), 'JPG', 95)
    image = QImage(600, 900, QImage.Format.Format_RGB32)
    image.fill(QColor('#2366c4'))
    assert image.save(str(blue), 'PNG')
    original_decode = subject._decode

    cache = subject.ImageCache()
    ready, threads, calls = [], [], []
    cache.ready.connect(lambda path: (ready.append(path), threads.append(QThread.currentThread())))
    release, entered = threading.Event(), threading.Event()
    def blocked_decode(path, side, cancelled):
        assert QThread.currentThread() != app.thread(), 'Decode happened on GUI thread'
        calls.append((path, side))
        entered.set()
        assert release.wait(5)
        return original_decode(path, side, cancelled)
    with patch.object(subject, '_decode', side_effect=blocked_decode):
        before = time.perf_counter()
        placeholder = cache.pixmap(str(red), 64, 1.25)
        assert (time.perf_counter() - before) < .1
        assert placeholder.width() == 80 and placeholder.devicePixelRatioF() == 1.25
        assert not placeholder.isNull()
        for _ in range(10):
            cache.pixmap(str(red), 64, 1.25)
        wait_until(entered.is_set)
        heartbeat = []
        QTimer.singleShot(1, lambda: heartbeat.append(True))
        wait_until(lambda: bool(heartbeat))
        assert ready == [] and len(calls) == 1
        release.set()
        wait_until(lambda: len(ready) == 1)
    decoded = cache.image(str(red), 80)
    assert decoded is not None and not decoded.isNull()
    assert max(decoded.width(), decoded.height()) <= 128
    actual = cache.pixmap(str(red), 64, 1.25)
    assert actual.width() == 80 and actual.height() == 80 and actual.devicePixelRatioF() == 1.25
    assert actual.toImage() != placeholder.toImage()
    assert actual.toImage().pixelColor(40, 40).red() > 150
    assert actual.toImage().pixelColor(0, 0).alpha() == 0
    assert all(thread == app.thread() for thread in threads)
    cache.request(str(red), 2048)
    wait_until(lambda: cache.image(str(red), 2048) is not None)
    preview_image = cache.image(str(red), 2048)
    assert max(preview_image.width(), preview_image.height()) == 2048
    assert abs(preview_image.width() / preview_image.height() - 1.5) < .002
    print('PASS worker decode, deduplication, heartbeat, JPEG downsample and DPR optical framing')

    # Provider selection cannot stat or decode each raster before scheduling it.
    repository = ContentRepository()
    repository.thumbnail_provider = cache.pixmap
    text = repository.add_text('https://example.invalid/article')
    repository.metadata[text] = {'preview_image': str(red)}
    with patch.object(Path, 'is_file', side_effect=AssertionError('GUI stat')):
        assert not repository.item_pixmap(str(red), 64, dpr=1.25).isNull()
        assert not repository.item_pixmap(text, 64, dpr=1.25).isNull()
    print('PASS raster and website cover use optional asynchronous provider without file stat')

    # An old result cannot replace a newly selected path or clear_source().
    cache.forget(str(red))
    release, entered = threading.Event(), threading.Event()
    def red_waits(path, side, cancelled):
        if path == str(red):
            entered.set()
            assert release.wait(5)
        return original_decode(path, side, cancelled)
    preview = ImagePreview()
    preview.set_loader(cache)
    with patch.object(subject, '_decode', side_effect=red_waits):
        assert preview.set_source(str(red))
        wait_until(entered.is_set)
        assert preview.image is None
        assert preview.set_source(str(blue))
        wait_until(lambda: preview.image is not None)
        assert preview.image.pixelColor(100, 100).blue() > 150
        release.set()
        wait_until(lambda: cache.image(str(red), 2048) is not None)
        assert preview.image.pixelColor(100, 100).blue() > 150
        preview.clear_source()
        cache.ready.emit(str(red))
        cache.ready.emit(str(blue))
        assert preview.image is None and preview.renderer is None
    print('PASS delayed prior selection and removed preview cannot restore old image')

    # A file can be moved while the worker owns the read handle on Windows.
    copy = root / 'sharing.jpg'
    copy.write_bytes(red.read_bytes())
    moved = root / 'moved.jpg'
    original_reader = subject._source_reader
    opened, release = threading.Event(), threading.Event()
    @contextmanager
    def held_reader(path):
        with original_reader(path) as stream:
            opened.set()
            assert release.wait(5)
            yield stream
    with patch.object(subject, '_source_reader', side_effect=held_reader):
        cache.request(str(copy), 128)
        wait_until(opened.is_set)
        copy.rename(moved)
        moved.unlink()
        cache.forget(str(copy))
        before = list(ready)
        release.set()
        wait_until(lambda: not any(key[0] == subject._path_key(copy) for key in cache._pending))
        assert ready == before and cache.image(str(copy), 128) is None
    print('PASS Windows move/delete sharing and forgotten in-flight image invalidation')

    # close() returns promptly and suppresses the already running completion.
    closing = subject.ImageCache()
    received = []
    closing.ready.connect(received.append)
    entered, release = threading.Event(), threading.Event()
    def delayed_close(path, side, cancelled):
        entered.set()
        assert release.wait(5)
        return original_decode(path, side, cancelled)
    with patch.object(subject, '_decode', side_effect=delayed_close):
        closing.request(str(red), 128)
        wait_until(entered.is_set)
        started = time.perf_counter()
        closing.close()
        assert time.perf_counter() - started < .1
        release.set()
        wait_until(lambda: closing._pool.activeThreadCount() == 0)
        settle()
        assert received == [] and closing.image(str(red), 128) is None
    print('PASS close cancels queued publication without blocking the GUI')

    # clear() invalidates running results while permitting immediate new work.
    clearing = subject.ImageCache()
    cleared_results = []
    clearing.ready.connect(cleared_results.append)
    entered, release = threading.Event(), threading.Event()
    def delayed_clear(path, side, cancelled):
        if path == str(red):
            entered.set()
            assert release.wait(5)
        return original_decode(path, side, cancelled)
    with patch.object(subject, '_decode', side_effect=delayed_clear):
        clearing.request(str(red), 128)
        wait_until(entered.is_set)
        started = time.perf_counter()
        clearing.clear()
        assert time.perf_counter() - started < .1
        assert clearing.request(str(blue), 128)
        wait_until(lambda: clearing.image(str(blue), 128) is not None)
        release.set()
        wait_until(lambda: clearing._pool.activeThreadCount() == 0)
        settle()
        assert cleared_results == [str(blue)]
        assert clearing.image(str(red), 128) is None
    clearing.close()
    print('PASS clear rejects old results and allows new requests immediately')

    broken = root / 'broken.jpg'
    broken.write_bytes(b'not an image')
    cache.request(str(broken), 128)
    wait_until(lambda: cache.image(str(broken), 128) is not None)
    assert cache.image(str(broken), 128).isNull()
    for _ in range(10):
        assert not cache.pixmap(str(broken), 64, 1.25).isNull()
    assert not cache._pending
    print('PASS failed decode stays a usable placeholder without a retry loop')
    cache.close()
    preview.close()
    QCoreApplication.processEvents()
