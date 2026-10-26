"""Prove native media handles release, with no desktop window or sound."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_MEDIA_BACKEND"] = "windows"

import argparse
import ctypes
from ctypes import wintypes
from pathlib import Path
import shutil
import sys
import tempfile
import time

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from media_preview import MediaPreview


def wait_until(app, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        if predicate():
            return True
        QTest.qWait(15)
    return bool(predicate())


def exclusive_error(path):
    """Use share=0 so a successful open proves all other handles are closed."""
    handle = create_file(str(path), 0xC0000000, 0, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        return ctypes.get_last_error()
    close_handle(handle)
    return 0


def assert_released(app, path):
    assert wait_until(app, lambda: exclusive_error(path) == 0, timeout=3), (
        f"Native player still owns {path.name}; Win32 error {exclusive_error(path)}")
    renamed = path.with_name(path.stem + "-renamed.mp4")
    path.rename(renamed)
    renamed.unlink()
    assert not renamed.exists(), "Released original must be deletable"


parser = argparse.ArgumentParser(description="Offscreen native media file-release regression")
parser.add_argument("--media", default=os.getenv("FLICKDOCK_TEST_MEDIA", str(Path(__file__).parent / "media-test.mp4")))
fixture = Path(parser.parse_args().media).expanduser().resolve()
if sys.platform != "win32" or not fixture.is_file():
    print("SKIP file-release test: Windows and an explicit --media VIDEO.mp4 fixture are required")
    raise SystemExit(0)

kernel = ctypes.WinDLL("kernel32", use_last_error=True)
create_file = kernel.CreateFileW
create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
create_file.restype = wintypes.HANDLE
close_handle = kernel.CloseHandle
close_handle.argtypes = [wintypes.HANDLE]
close_handle.restype = wintypes.BOOL

with tempfile.TemporaryDirectory(prefix="flickdock-media-release-") as temporary:
    folder = Path(temporary)
    os.environ["APPDATA"] = str(folder / "profile")
    app = QApplication([])
    card = QWidget()
    layout = QVBoxLayout(card)
    viewer = MediaPreview(card, motion=False)
    layout.addWidget(viewer)
    card.resize(400, 300)
    card.show()
    viewer.toggle_mute()  # Applies to every lazily recreated audio output.
    frames = []
    viewer.frame_ready.connect(lambda path, image: frames.append(path))

    def load(name):
        source = folder / (name + ".mp4")
        shutil.copyfile(fixture, source)
        count = len(frames)
        viewer.set_source(source)
        assert wait_until(app, lambda: len(frames) > count or viewer._failed, timeout=8)
        assert len(frames) > count, viewer.status_label.toolTip()
        assert viewer.audio_output.isMuted() and not viewer._reported_playing
        assert exclusive_error(source) == 32, "Fixture must exercise a real native open media source"
        return source

    try:
        stopped = load("stop")
        viewer.stop()
        assert viewer.player is None and viewer.path == str(stopped)
        assert not viewer.surface.image.isNull()
        assert_released(app, stopped)
        print("PASS native stop releases exclusive-open, rename and unlink")

        switched = load("switch-old")
        current = load("switch-new")
        assert viewer.path == str(current)
        assert_released(app, switched)
        viewer.release_source()
        assert_released(app, current)
        print("PASS selecting a new source releases the previous file")

        hidden = load("card-hide")
        poster = viewer.surface.image.copy()
        card.hide()  # Mirrors the shelf card's temporary morph hide.
        assert viewer.player is None and viewer.path == str(hidden)
        assert wait_until(app, lambda: exclusive_error(hidden) == 0)
        card.show()
        app.processEvents()
        assert viewer.player is None and viewer.surface.image == poster
        viewer.toggle_playback()
        assert wait_until(app, lambda: viewer.player is not None and viewer._reported_playing)
        assert viewer.audio_output.isMuted(), "Replay must preserve mute preference"
        assert exclusive_error(hidden) == 32
        viewer.stop()
        assert_released(app, hidden)
        print("PASS temporary card hide releases the file; explicit replay rebuilds the player silently")

        removed = load("removed")
        viewer.release_source()
        assert not viewer.path and viewer.player is None and viewer.surface.image.isNull()
        assert_released(app, removed)
        card.hide()
        card.show()
        viewer.toggle_playback()
        QTest.qWait(80)
        assert viewer.player is None and not viewer.path and not viewer.play_button.isEnabled()
        print("PASS removed source is deletable and cannot reopen after show or playback")
    finally:
        viewer.release_source()
        card.close()
        wait_until(app, lambda: viewer.player is None)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

print("FILE_RELEASE_V161_OK")
