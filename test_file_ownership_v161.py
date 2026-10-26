"""Actual shelf nonmedia lifecycle and Windows source-handle ownership.

Only temporary copies are used. Qt is offscreen; tray/registry/native input
registration are disabled. CreateFileW probes temporary files, never windows.
Use --probe-zip-remove to report the pre-fix ZIP lifecycle without asserting
cancellation; normal mode requires removal to cancel the affected export.
"""
from contextlib import ExitStack
import ctypes
from ctypes import wintypes
import gzip
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
from unittest.mock import patch
import zipfile

os.environ["QT_QPA_PLATFORM"] = "offscreen"
profile = tempfile.TemporaryDirectory(prefix="flickdock-ownership-profile-")
os.environ["APPDATA"] = profile.name

from PySide6.QtCore import QCoreApplication, QEvent, QPoint
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSystemTrayIcon
import app_settings
import flickdock as subject
import flickdock_icons
from global_hotkeys import _NoopBackend


if sys.platform != "win32":
    raise SystemExit("This regression requires Windows file sharing semantics")
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                              wintypes.HANDLE]
kernel.CreateFileW.restype = wintypes.HANDLE
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
kernel.CloseHandle.restype = wintypes.BOOL
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def exclusive_error(path):
    """Require read/write/delete access with *no* sharing granted to others."""
    handle = kernel.CreateFileW(str(path), 0x80000000 | 0x40000000 | 0x00010000,
                                0, None, 3, 0x80, None)
    if handle == INVALID_HANDLE_VALUE:
        return ctypes.get_last_error()
    assert kernel.CloseHandle(handle)
    return 0


def flush():
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        flush()
        QTest.qWait(5)
    assert predicate(), "fixture operation did not settle"


def require_free(path, phase):
    error = exclusive_error(path)
    assert error == 0, f"{path.suffix} source held at {phase}: WinError {error}"


app = QApplication([])
subject.load_font(app)
manager = None
release_zip = threading.Event()
try:
    with tempfile.TemporaryDirectory(prefix="flickdock-ownership-files-") as temporary, ExitStack() as mocks:
        root = Path(temporary)
        mocks.enter_context(patch.object(subject, "motion_enabled", return_value=False))
        mocks.enter_context(patch.object(QSystemTrayIcon, "show"))
        mocks.enter_context(patch.object(QSystemTrayIcon, "hide"))
        registry_read = mocks.enter_context(patch.object(app_settings, "_read_startup_value"))
        registry_write = mocks.enter_context(patch.object(app_settings, "_write_startup_value"))
        native_icons = mocks.enter_context(patch.object(flickdock_icons, "_native_icon", wraps=flickdock_icons._native_icon))
        manager = subject.ShelfManager(app)
        manager.timer.stop()
        assert isinstance(manager.hotkeys.backend, _NoopBackend)
        assert not manager.hotkeys._filter_installed
        shelf = manager.shelf
        image = QImage(120, 80, QImage.Format.Format_ARGB32)
        image.fill(0xff34a981)
        png = root / "图片.png"
        assert image.save(str(png), "PNG")
        svg_data = b'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80"><rect width="120" height="80" fill="#34a981"/></svg>'
        svg, svgz = root / "矢量.svg", root / "压缩矢量.svgz"
        svg.write_bytes(svg_data)
        svgz.write_bytes(gzip.compress(svg_data))
        text = root / "文本.txt"
        text.write_text("完整文本\n第二行", encoding="utf-8")
        pdf = root / "文档.pdf"
        pdf.write_bytes(b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n")
        exe = root / "图标只读测试.exe"
        shutil.copyfile(sys.executable, exe)  # Read its icon only; never execute.
        unsupported = root / "未知格式.ownership-fixture"
        unsupported.write_bytes(bytes(range(256)) * 10)

        for source in (png, svg, svgz, text, pdf, exe, unsupported):
            ref = str(source.resolve())
            shelf.add_paths("permanent", [ref])
            shelf.show_transient(QPoint(400, 350))
            shelf.show_contents("permanent")
            popup = shelf.popup
            popup.select_item(ref)
            popup.preview.show_path(ref, force=True)
            assert popup.preview.current_path == ref
            assert not shelf.item_pixmap(ref, 90).isNull()
            flush()
            if source.suffix in (".png", ".svg", ".svgz"):
                if source.suffix=='.png': wait_until(lambda: popup.preview.image.image is not None)
                assert popup.preview.stack.currentWidget() is popup.preview.image
                assert popup.preview.image.image is not None if source.suffix == ".png" else popup.preview.image.renderer is not None
            elif source.suffix == ".txt":
                assert popup.preview.stack.currentWidget() is popup.preview.text
                assert popup.preview.text.toPlainText() == "完整文本\n第二行"
            else:
                assert popup.preview.stack.currentWidget() is popup.preview.icon
            # Even a retained preview must own pixels/bytes, not the source.
            retained_image = popup.preview.image.image
            retained_renderer = popup.preview.image.renderer
            require_free(source, "preview visible")
            shelf.remove_path("permanent", ref)
            assert ref not in shelf.files["permanent"]
            require_free(source, "immediately after shelf removal")
            renamed = source.with_name("已移除-" + source.name)
            source.rename(renamed)
            require_free(renamed, "renamed after shelf removal")
            renamed.unlink()
            assert not source.exists() and not renamed.exists()
            # Keep memory previews alive through the exclusive/rename/unlink
            # checks, so GC cannot hide a source-file ownership problem.
            _ = retained_image, retained_renderer
            print("PASS add→preview→remove→exclusive/rename/unlink " + source.suffix)
        assert native_icons.call_count >= 2 and flickdock_icons._provider is not None
        native_ref = shelf.content.add_text("原生文字不创建磁盘文档")
        shelf.add_paths("permanent", [native_ref])
        shelf.show_contents("permanent")
        shelf.popup.preview.show_path(native_ref)
        assert shelf.popup.preview.text.toPlainText() == "原生文字不创建磁盘文档"
        shelf.remove_path("permanent", native_ref)
        assert not list(root.glob("*.txt"))
        print("PASS QImageReader/QSvgRenderer/native-icon branches return memory artwork; native text creates no file")

        # Hold an actual archive block at a scheduling boundary. The real ZIP
        # reader is open at this point; only worker timing is controlled.
        large = root / "正在打包.bin"
        large.write_bytes(os.urandom(2 * 1024 * 1024))
        ref = str(large.resolve())
        shelf.add_paths("permanent", [ref])
        entered = threading.Event()
        original_write = zipfile._ZipWriteFile.write
        gui_thread = threading.get_ident()
        def pause_first_block(writer, data):
            result = original_write(writer, data)
            if threading.get_ident() != gui_thread and not entered.is_set():
                entered.set()
                assert release_zip.wait(5), "archive fixture barrier expired"
            return result
        target = root / "所选文件.zip"
        with patch.object(zipfile._ZipWriteFile, "write", pause_first_block), \
             patch.object(shelf, "choose_zip_path", return_value=str(target)):
            assert shelf.compress_refs([ref])
            job = shelf.archive_job
            wait_until(entered.is_set)
            before = exclusive_error(large)
            epoch = shelf._archive_epoch
            shelf.remove_path("permanent", ref)
            cancelled = job._cancel_event.is_set()
            after_remove = exclusive_error(large)
            epoch_changed = shelf._archive_epoch != epoch
            # The reader is deliberately still open at this barrier. Deletion
            # must already work, without making the GUI wait for compression.
            renamed_while_reading=large.with_name('读取期间已移除.bin')
            large.rename(renamed_while_reading)
            renamed_while_reading.unlink()
            assert not large.exists() and not renamed_while_reading.exists()
            release_zip.set()
            wait_until(lambda: shelf.archive_job is None)
            assert job.wait(1000)
        print(f"ZIP_REMOVE_OBSERVED before={before} after_remove={after_remove} cancel={cancelled} epoch_changed={epoch_changed} deleted_while_reader_open=True output_added={str(target) in shelf.files['permanent']}")
        if "--probe-zip-remove" not in sys.argv:
            assert cancelled and epoch_changed, "Removing an active ZIP input must cancel and invalidate that export"
            assert not target.exists() and str(target) not in shelf.files["permanent"]
        assert not list(root.glob("*.part"))
        registry_read.assert_not_called()
        registry_write.assert_not_called()
        manager.quit()
        manager = None
        flush()
        print("FILE_OWNERSHIP_V161_OK")
finally:
    release_zip.set()
    if manager is not None:
        manager.quit()
        flush()
    profile.cleanup()
