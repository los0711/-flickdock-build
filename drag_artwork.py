"""Shelf-owned QDrag artwork: a content card above a readable filename pill.

External Explorer/browser drag-source artwork belongs to those applications.
This module paints only the pixmap supplied to FlickDock's own QDrag objects.
The application's reference platform font must already have been selected.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen, QPixmap

from content_stack import stack_pixmap
from flickdock_icons import device_pixel_ratio
import ui_theme


DRAG_WIDTH = 140
DRAG_HEIGHT = 124
_STACK_SIZE = 110
_LABEL_Y = 98
_LABEL_HEIGHT = 24
_LABEL_PADDING = 10
_TEXT_WIDTH = DRAG_WIDTH - _LABEL_PADDING * 2 - 2


def _label_font() -> QFont:
    # Keep drag labels on the same platform font as the reference interface.
    from native_ui_common import reference_font
    font = reference_font()
    font.setPixelSize(12)
    font.setWeight(QFont.Weight.Medium)
    font.setHintingPreference(QFont.HintingPreference.PreferVerticalHinting)
    return font


def _elide_filename(text: str, metrics: QFontMetricsF, width: float,
                    suffix: str = "") -> str:
    text = " ".join(str(text).split())
    if metrics.horizontalAdvance(text) <= width:
        return text
    # Keep the actual extension together. Eliding the full text alone may
    # discard part of a longer extension such as .markdown on a narrow pill.
    if suffix and text.casefold().endswith(suffix.casefold()):
        remaining = width - metrics.horizontalAdvance(suffix)
        if remaining >= metrics.horizontalAdvance("…"):
            return metrics.elidedText(text[:-len(suffix)], Qt.TextElideMode.ElideMiddle,
                                      remaining) + text[-len(suffix):]
    return metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, width)


def _label_for(repository, refs, metrics: QFontMetricsF) -> str:
    if len(refs) > 1:
        return _elide_filename(f"{len(refs)} 项内容", metrics, _TEXT_WIDTH)
    ref = refs[0]
    text = repository.title(ref)
    suffix = Path(ref).suffix if not repository.is_text(ref) else ""
    return _elide_filename(text, metrics, _TEXT_WIDTH, suffix)


def drag_pixmap(repository, refs, preview_cache=None, dpr=None) -> QPixmap:
    """Return a transparent 140×124 logical-pixel, theme-aware drag image.

    Single files also use the colorful content-stack format card. Images and
    cached covers keep their real thumbnail through stack_pixmap(). Multiple
    selections retain the same latest-first stack and a ``N 项内容`` label.
    """
    refs = tuple(refs or ())
    ratio = device_pixel_ratio(dpr)
    pixmap = QPixmap(round(DRAG_WIDTH * ratio), round(DRAG_HEIGHT * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    if not refs:
        return pixmap

    artwork = stack_pixmap(repository, refs, _STACK_SIZE, dpr=ratio,
                           preview_cache=preview_cache)
    font = _label_font()
    metrics = QFontMetricsF(font)
    label = _label_for(repository, refs, metrics)
    pill_width = min(DRAG_WIDTH - 2, max(44, metrics.horizontalAdvance(label) + _LABEL_PADDING * 2))
    pill = QRectF((DRAG_WIDTH - pill_width) / 2, _LABEL_Y, pill_width, _LABEL_HEIGHT)

    painter = QPainter(pixmap)
    painter.setRenderHints(QPainter.RenderHint.Antialiasing |
                           QPainter.RenderHint.SmoothPixmapTransform |
                           QPainter.RenderHint.TextAntialiasing)
    # The stack's front card is centered at 58/100 of its canvas width.
    # Align the visible card with the filename, rather than its empty canvas.
    artwork_x = DRAG_WIDTH / 2 - _STACK_SIZE * .58
    painter.drawPixmap(QRectF(artwork_x, 0, _STACK_SIZE, _STACK_SIZE), artwork,
                       QRectF(artwork.rect()))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(7, 15, 28, 32 if ui_theme.current_theme() == "dark" else 22))
    painter.drawRoundedRect(pill.translated(0, 1.5), 9, 9)
    painter.setBrush(QColor(ui_theme.color("surface")))
    painter.setPen(QPen(QColor(ui_theme.color("border")), .8))
    painter.drawRoundedRect(pill, 9, 9)
    painter.setFont(font)
    painter.setPen(QColor(ui_theme.color("text")))
    painter.drawText(pill.adjusted(_LABEL_PADDING, 0, -_LABEL_PADDING, 0),
                     Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextSingleLine, label)
    painter.end()
    return pixmap
