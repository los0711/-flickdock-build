"""Bounded image decoding outside the GUI thread, with no retained file handle.

Workers own QImages only. QPixmaps and their DPR-aware optical framing are
created on the GUI thread after a queued completion signal.
"""
from __future__ import annotations

from collections import OrderedDict
import math
import os
from pathlib import Path
import threading

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, QRunnable, QSize, QThreadPool, Qt, Signal, Slot
from PySide6.QtGui import QImage, QImageReader, QPainter, QPainterPath, QPixmap

from flickdock_icons import device_pixel_ratio, document_pixmap
from selection_archive import _source_reader


RASTER_SUFFIXES = frozenset({'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.ico',
                             '.tif', '.tiff', '.heic', '.heif', '.avif'})


def _path_key(path):
    # Resolving symlinks/stat'ing every offscreen item would itself block the
    # GUI. The same lexical absolute name is used by both request and result.
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _side(value):
    value = max(1, min(2048, int(value)))
    return next((side for side in (128, 256, 512, 1024, 2048) if side >= value), 2048)


def _decode(path, side, cancelled):
    """Close DELETE-sharing source before decoding from a memory buffer."""
    if cancelled.is_set():
        return QImage()
    try:
        chunks = []
        with _source_reader(Path(path)) as incoming:
            while not cancelled.is_set():
                block = incoming.read(256 * 1024)
                if not block:
                    break
                chunks.append(block)
        if cancelled.is_set():
            return QImage()
        data = QByteArray(b''.join(chunks))
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
        reader.setAutoTransform(True)
        natural = reader.size()
        if natural.isValid() and max(natural.width(), natural.height()) > side:
            reader.setScaledSize(natural.scaled(QSize(side, side), Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read()
        buffer.close()
        if cancelled.is_set():
            return QImage()
        # Some plugins ignore scaledSize; keep cache memory bounded regardless.
        if not image.isNull() and max(image.width(), image.height()) > side:
            image = image.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        return image
    except (OSError, ValueError, RuntimeError):
        return QImage()


class _Signals(QObject):
    done = Signal(str, str, int, int, QImage)


class _DecodeJob(QRunnable):
    def __init__(self, path, key, side, generation, cancelled):
        super().__init__()
        self.path, self.key, self.side, self.generation = path, key, side, generation
        self.cancelled = cancelled
        self.signals = _Signals()

    def run(self):
        image = _decode(self.path, self.side, self.cancelled)
        if not self.cancelled.is_set():
            self.signals.done.emit(self.path, self.key, self.side, self.generation, image)


class ImageCache(QObject):
    """Immediate placeholders, deduplicated asynchronous requests and CPU images.

    ``ready(path)`` fires on this object's thread for successful and failed
    requests. ``image(path, max_side)`` is None until that request is ready;
    a null QImage means decoding failed. ``forget`` invalidates in-flight work.
    """
    ready = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._cancelled = threading.Event()
        self._closed = False
        self._images = OrderedDict()
        self._pixmaps = OrderedDict()
        self._placeholders = {}
        self._pending = {}
        self._generations = {}
        self._image_bytes = 0
        self._budget = 96 * 1024 * 1024

    def image(self, path, max_side=2048):
        if self._closed or not path:
            return None
        key, side = _path_key(path), _side(max_side)
        candidates = [(candidate[1], candidate) for candidate in self._images
                      if candidate[0] == key and candidate[1] >= side]
        if not candidates:
            return None
        candidate = min(candidates)[1]
        image = self._images.pop(candidate)
        self._images[candidate] = image
        return image

    def request(self, path, max_side=2048):
        if self._closed or not path:
            return False
        value = os.path.abspath(os.fspath(path))
        if Path(value).suffix.lower() not in RASTER_SUFFIXES:
            return False
        key, side = _path_key(value), _side(max_side)
        if self.image(value, side) is not None:
            return True
        generation = self._generations.get(key, 0)
        task_key = (key, side, generation)
        if task_key in self._pending:
            return True
        job = _DecodeJob(value, key, side, generation, self._cancelled)
        job.signals.done.connect(self._completed, Qt.ConnectionType.QueuedConnection)
        self._pending[task_key] = job
        self._pool.start(job, -1)
        return True

    def pixmap(self, path, size=90, dpr=None):
        size = max(1, int(size))
        ratio = device_pixel_ratio(dpr)
        key = (_path_key(path), size, ratio)
        if not self._closed and key in self._pixmaps:
            pixmap = self._pixmaps.pop(key)
            self._pixmaps[key] = pixmap
            return pixmap
        image = self.image(path, math.ceil(size * .84 * ratio))
        if image is not None and not image.isNull():
            pixmap = QPixmap(round(size * ratio), round(size * ratio))
            pixmap.setDevicePixelRatio(ratio)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            optical = size * .84
            scale = min(optical / image.width(), optical / image.height())
            width, height = image.width() * scale, image.height() * scale
            from PySide6.QtCore import QRectF
            rect = QRectF((size - width) / 2, (size - height) / 2, width, height)
            clip = QPainterPath()
            clip.addRoundedRect(rect, 5, 5)
            painter.setClipPath(clip)
            painter.drawImage(rect, image)
            painter.end()
            self._pixmaps[key] = pixmap
            while len(self._pixmaps) > 256:
                self._pixmaps.popitem(last=False)
            return pixmap
        if image is None:
            self.request(path, math.ceil(size * .84 * ratio))
        placeholder_key = (Path(path).suffix.lower(), size, ratio)
        if placeholder_key not in self._placeholders:
            self._placeholders[placeholder_key] = document_pixmap(Path(path).suffix, size, dpr=ratio)
        return self._placeholders[placeholder_key]

    @Slot(str, str, int, int, QImage)
    def _completed(self, path, key, side, generation, image):
        self._pending.pop((key, side, generation), None)
        if self._closed or self._generations.get(key, 0) != generation:
            return
        image_key = (key, side)
        old = self._images.pop(image_key, None)
        if old is not None:
            self._image_bytes -= old.sizeInBytes()
        self._images[image_key] = image
        self._image_bytes += image.sizeInBytes()
        while (self._image_bytes > self._budget and len(self._images) > 1) or len(self._images) > 512:
            _, removed = self._images.popitem(last=False)
            self._image_bytes -= removed.sizeInBytes()
        # Larger previews can also improve a small thumbnail cached earlier.
        for pixmap_key in [candidate for candidate in self._pixmaps if candidate[0] == key]:
            del self._pixmaps[pixmap_key]
        self.ready.emit(path)

    def forget(self, path):
        if not path:
            return
        key = _path_key(path)
        self._generations[key] = self._generations.get(key, 0) + 1
        for image_key in [candidate for candidate in self._images if candidate[0] == key]:
            self._image_bytes -= self._images.pop(image_key).sizeInBytes()
        for pixmap_key in [candidate for candidate in self._pixmaps if candidate[0] == key]:
            del self._pixmaps[pixmap_key]

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._cancelled.set()
        self.clear()

    def clear(self):
        """Forget everything without waiting; later requests remain usable."""
        keys = {key[0] for key in self._pending} | {key[0] for key in self._images}
        for key in keys:
            self._generations[key] = self._generations.get(key, 0) + 1
        self._pool.clear()
        self._images.clear()
        self._pixmaps.clear()
        self._pending.clear()
        self._placeholders.clear()
        self._image_bytes = 0
