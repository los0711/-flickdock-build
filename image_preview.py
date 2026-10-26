"""Raster and SVG/SVGZ preview; vector sources stay vectors at every scale."""
import gzip
from pathlib import Path
from PySide6.QtCore import QByteArray, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImageReader, QPainter, QPainterPath
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QWidget
import ui_theme


class ImagePreview(QWidget):
    source_ready = Signal(str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.renderer = None
        self.image = None
        self._loader = None
        self._source_path = None
        self._loading = False
        self._generation = 0
        self.setMinimumSize(80, 80)

    def set_loader(self, loader):
        if self._loader is loader:
            return
        if self._loader is not None:
            try:
                self._loader.ready.disconnect(self._image_ready)
            except (RuntimeError, TypeError):
                pass
        self._loader = loader
        if loader is not None:
            loader.ready.connect(self._image_ready)

    def clear_source(self):
        self._generation += 1
        self._source_path = None
        self._loading = False
        self.renderer = self.image = None
        self.update()

    def _image_ready(self, path):
        if self._loader is None or self._source_path is None:
            return
        from async_images import _path_key
        if _path_key(path) != _path_key(self._source_path):
            return
        image = self._loader.image(self._source_path, 2048)
        if image is None:
            return
        self._loading = False
        self.image = None if image.isNull() else image
        self.update()
        self.source_ready.emit(str(self._source_path), self.image is not None)

    def set_source(self, path):
        self.clear_source()
        if not path:
            return False
        source = Path(path)
        self._source_path = str(source)
        if self._loader is not None and source.suffix.lower() not in ('.svg', '.svgz'):
            image = self._loader.image(path, 2048)
            if image is not None:
                self.image = None if image.isNull() else image
                self.update()
                return self.image is not None
            self._loading = self._loader.request(path, 2048)
            self.update()
            return self._loading
        try:
            if source.suffix.lower() in ('.svg', '.svgz'):
                data = source.read_bytes()
                if source.suffix.lower() == '.svgz':
                    with gzip.open(source, 'rb') as stream:
                        data = stream.read(16 * 1024 * 1024 + 1)
                if len(data) > 16 * 1024 * 1024 or b'<!ENTITY' in data.upper():
                    return False
                renderer = QSvgRenderer(QByteArray(data))
                if renderer.isValid(): self.renderer = renderer
            else:
                reader = QImageReader(str(source))
                reader.setAutoTransform(True)
                size = reader.size()
                if size.width() * size.height() > 20_000_000:
                    size.scale(2560, 2560, Qt.AspectRatioMode.KeepAspectRatio)
                    reader.setScaledSize(size)
                image = reader.read()
                if not image.isNull(): self.image = image
        except (OSError, ValueError):
            pass
        self.update()
        return self.renderer is not None or self.image is not None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        bounds = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(ui_theme.color('surface')))
        painter.drawRoundedRect(bounds, 10, 10)
        available = bounds.adjusted(10, 10, -10, -10)
        natural = self.renderer.viewBoxF().size() if self.renderer else self.image.size() if self.image is not None else None
        if natural is not None and natural.width() > 0 and natural.height() > 0:
            factor = min(available.width()/natural.width(), available.height()/natural.height())
            width, height = natural.width()*factor, natural.height()*factor
            target = QRectF(available.center().x()-width/2, available.center().y()-height/2, width, height)
            clip = QPainterPath(); clip.addRoundedRect(available, 6, 6); painter.setClipPath(clip)
            if self.renderer: self.renderer.render(painter, target)
            else: painter.drawImage(target, self.image)
        painter.end()
