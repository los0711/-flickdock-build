"""Offscreen shelf QDrag artwork at standard and fractional display scales."""
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
profile = tempfile.TemporaryDirectory(prefix="flickdock-drag-artwork-profile-")
os.environ["APPDATA"] = profile.name

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetricsF, QImage, QPainter
from PySide6.QtWidgets import QApplication
import drag_artwork as module
from drag_artwork import drag_pixmap
from shelf_content import ContentRepository
import ui_theme


app = QApplication([])
font_file = Path(__file__).parent / "fonts" / "HarmonyOS_Sans_SC_Regular.ttf"
font_id = QFontDatabase.addApplicationFont(str(font_file))
assert font_id >= 0 and "HarmonyOS Sans SC" in QFontDatabase.applicationFontFamilies(font_id)
app.setFont(QFont("HarmonyOS Sans SC", 11))
metrics = QFontMetricsF(module._label_font())
assert module._label_font().family() == "HarmonyOS Sans SC"


def color_at(pixmap, x, y):
    ratio = pixmap.devicePixelRatio()
    return pixmap.toImage().pixelColor(round(x * ratio), round(y * ratio))


def opaque_pixels(pixmap, top, bottom):
    image = pixmap.toImage()
    ratio = pixmap.devicePixelRatio()
    return sum(image.pixelColor(x, y).alpha() > 200
               for y in range(round(top * ratio), round(bottom * ratio))
               for x in range(image.width()))


try:
    with tempfile.TemporaryDirectory(prefix="flickdock-drag-artwork-") as temporary:
        root = Path(temporary)
        repository = ContentRepository()
        long = root / "项目交付资料与所有修改记录的最终确认版本-特别特别长的文件名称.markdown"
        long.write_text("# Complete content\n", encoding="utf-8")
        refs = []
        for suffix in (".pdf", ".png", ".xlsx", ".mp4", ".zip", ".mp3"):
            source = root / ("item" + suffix)
            source.write_bytes(b"fixture")
            refs.append(str(source))
        image = QImage(80, 60, QImage.Format.Format_ARGB32)
        image.fill(QColor("#31b67c"))
        assert image.save(refs[1], "PNG")
        cached = QImage(80, 60, QImage.Format.Format_ARGB32)
        cached.fill(QColor("#8157c2"))

        caption = module._label_for(repository, [str(long)], metrics)
        assert caption.endswith(".markdown") and "…" in caption
        assert not caption.startswith("…") and not caption.endswith("…")
        assert metrics.horizontalAdvance(caption) <= module._TEXT_WIDTH
        assert repository.title(str(long)) == long.name
        assert module._label_for(repository, refs, metrics) == "6 项内容"
        native = repository.add_text("原生文字的第一行\n第二行保留")
        assert "\n" not in module._label_for(repository, [native], metrics)
        assert repository.text(native) == "原生文字的第一行\n第二行保留"
        print("PASS single-line middle elision preserves filename extension and native text content")

        for theme in ("light", "dark"):
            ui_theme.set_theme(theme)
            for ratio in (1.0, 1.25, 1.5, 2.0):
                with patch.object(module, "stack_pixmap", wraps=module.stack_pixmap) as stack:
                    single = drag_pixmap(repository, [str(long)], dpr=ratio)
                    assert stack.call_count == 1 and stack.call_args.args[1] == (str(long),)
                assert single.width() == round(140 * ratio) and single.height() == round(124 * ratio)
                assert single.devicePixelRatio() == ratio
                assert single.deviceIndependentSize().width() <= 140
                assert single.deviceIndependentSize().height() == 124
                assert color_at(single, 0, 0).alpha() == 0
                assert opaque_pixels(single, 0, 95) > 1000 * ratio * ratio
                assert opaque_pixels(single, 98, 122) > 1500 * ratio * ratio
                assert color_at(single, 70, 101).name() == ui_theme.color("surface")
                multi = drag_pixmap(repository, refs, dpr=ratio)
                assert bytes(multi.toImage().constBits()) != bytes(single.toImage().constBits())
                actual = drag_pixmap(repository, [refs[1]], dpr=ratio)
                assert color_at(actual, 70, 50).name() == "#31b67c"
                cached_result = drag_pixmap(repository, [refs[1]], {refs[1]: cached}, dpr=ratio)
                assert color_at(cached_result, 70, 50).name() == "#8157c2"
                empty = drag_pixmap(repository, [], dpr=ratio)
                assert all(value == 0 for value in bytes(empty.toImage().constBits()))
        print("PASS colorful single/multiple stack, real/cached thumbnails and transparent edges at four DPI scales")
        print("PASS white/dark filename backgrounds are opaque and label pixels remain in a 140×124 DIP canvas")

        # Save only inside the temporary fixture, enabling PNG round-trip checks
        # without leaving any UI window or artifact on the user's desktop.
        ui_theme.set_theme("light")
        sample = drag_pixmap(repository, [str(long)], dpr=2)
        assert sample.save(str(root / "drag-preview.png"), "PNG")
        assert QImage(str(root / "drag-preview.png")).size() == sample.size()
        print("DRAG_ARTWORK_V161_OK")
finally:
    ui_theme.set_theme("light")
    profile.cleanup()
