"""Validate a real OLE content drag before revealing a shelf.

Holding the mouse and shaking is insufficient evidence: window moves, resizing,
text selection and arbitrary pointer gestures all do that.  This short-lived
drop target inspects the actual drag payload.  It is only armed after the shake
gesture, never displayed as visible UI, and never activates or captures input.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys
import time
from typing import Callable

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QPainter
from PySide6.QtWidgets import QApplication, QWidget
from platform_native import native_input_available, window_move_active, keep_tool_visible_when_inactive, raise_edge_target


class _GuiThreadInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


def _native_windows_enabled() -> bool:
    app = QApplication.instance()
    return bool(sys.platform == "win32" and app is not None
                and app.platformName().lower() == "windows")


def native_window_move_active() -> bool:
    """True during a native move/size loop, without treating menus as moves."""
    if sys.platform == 'darwin':
        return window_move_active()
    if not _native_windows_enabled():
        return False
    info = _GuiThreadInfo()
    info.cbSize = ctypes.sizeof(info)
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GuiThreadInfo)]
        user32.GetGUIThreadInfo.restype = wintypes.BOOL
        if user32.GetGUIThreadInfo(0, ctypes.byref(info)):
            return bool(info.flags & 0x0002 or info.hwndMoveSize)
    except OSError:
        pass
    return False


def is_content_mime(mime) -> bool:
    """Only formats which the shelf can actually store qualify."""
    try:
        direct = bool(
            (mime.hasUrls() and mime.urls())
            or mime.hasImage()
            or (mime.hasText() and mime.text().strip())
        )
        if direct:
            return True
        if mime.hasHtml():
            # Browser image/link drags can expose only text/html. Reuse the
            # resolver's scheme-checked parser; formatting-only HTML does not
            # become a content drag merely because it contains markup.
            from content_resolver import candidates
            return bool(candidates(mime))
        return False
    except (RuntimeError, ValueError):
        return False


class _DropProbe(QWidget):
    def __init__(self, gate: "ContentDragGate"):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint |
                         Qt.WindowType.WindowDoesNotAcceptFocus)
        self.gate = gate
        self.setObjectName("FlickDockOleProbe")
        self.setWindowTitle("FlickDock drag sensor")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        keep_tool_visible_when_inactive(self)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAcceptDrops(True)
        # A 96px target can be crossed in a single frame of a fast wrist flick,
        # particularly when video capture adds event-loop timing jitter.
        self.setFixedSize(224, 176)

    def paintEvent(self, event):
        # Alpha 0 is transparent to Windows layered-window hit testing, so it
        # would never receive OLE DragEnter. Alpha 1 is visually imperceptible
        # but remains a real target. Do not set WS_EX_TRANSPARENT here.
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 1))

    def dragEnterEvent(self, event):
        if self.gate._accepts(event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self.gate._content_entered(self.mapToGlobal(event.position().toPoint()))
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self.gate._accepts(event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        # Normally the probe is gone before the user can release. If release
        # arrives first, consume the payload only if a real storage callback
        # handles it synchronously; the event-owned MIME object is never kept.
        position = self.mapToGlobal(event.position().toPoint())
        handled = False
        if self.gate._accepts(event.mimeData()) and self.gate.on_drop is not None:
            self.gate._validated = True
            self.gate._deliver_validation(position, self.gate._epoch)
            handled = bool(self.gate.on_drop(event.mimeData(), position))
        if handled:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()
        self.gate._finish_probe()


class ContentDragGate:
    """Short-lived native drop target used after mouse shake detection.

    on_validated(global_position) is called once per held-mouse session and
    only after an actual file/image/text drag reaches the target. arm() returns
    whether a probe attempt was started. Call reset_session() on mouse release
    and at the beginning of a new press. close() must be called on application
    shutdown. on_drop(mime, position) optionally stores a rare release arriving
    during the probe; it must not retain the QMimeData object.
    """
    def __init__(self, on_validated: Callable[[QPoint], None],
                 on_drop: Callable | None = None,
                 is_internal_drag: Callable[[], bool] | None = None,
                 *, probe_timeout_ms: int = 1800):
        if QApplication.instance() is None:
            raise RuntimeError("ContentDragGate requires a QApplication")
        self.on_validated = on_validated
        self.on_drop = on_drop
        self.is_internal_drag = is_internal_drag or (lambda: False)
        self.probe = _DropProbe(self)
        self._epoch = 0
        self._active = False
        self._validated = False
        self._notified = False
        self._last_arm = -1.0
        self.probe_timeout_ms = max(80, min(2000, int(probe_timeout_ms)))
        self._timeout = QTimer(self.probe)
        self._timeout.setTimerType(Qt.TimerType.PreciseTimer)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._finish_probe)
        self._follow = QTimer(self.probe)
        self._follow.setTimerType(Qt.TimerType.PreciseTimer)
        self._follow.setInterval(16)
        self._follow.timeout.connect(self._follow_pointer)

    @property
    def active(self) -> bool:
        return self._active

    @property
    def validated(self) -> bool:
        return self._validated

    def arm(self, position: QPoint) -> bool:
        now = time.monotonic()
        if (self._active or self._validated or self.is_internal_drag()
                or native_window_move_active() or now - self._last_arm < 0.15):
            return False
        self._last_arm = now
        self._epoch += 1
        self._active = True
        self.probe.move(position - QPoint(self.probe.width() // 2, self.probe.height() // 2))
        self.probe.show()
        if sys.platform!='darwin':self.probe.raise_()
        # Commit alpha=1 over the complete native rectangle before the next
        # OLE pointer message. Deferred paint could leave a transparent target
        # for the first few events on a busy/capturing compositor.
        self.probe.repaint()
        # Enforce nonactivation even on platforms/styles which ignore the Qt
        # attribute on an already-created native HWND.
        self._raise_native_target()
        self._timeout.start(self.probe_timeout_ms)
        if native_input_available():
            self._follow.start()
        return True

    def _raise_native_target(self):
        if sys.platform=='darwin':
            raise_edge_target(self.probe)
            return
        if _native_windows_enabled():
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, wintypes.UINT]
            user32.SetWindowPos.restype = wintypes.BOOL
            user32.SetWindowPos(int(self.probe.winId()), -1, 0, 0, 0, 0,
                               0x0001 | 0x0002 | 0x0010 | 0x0040)

    def track_position(self, position: QPoint) -> bool:
        """Keep an armed target reachable; never extend its bounded lifetime.

        The main polling loop may call this with its already sampled cursor.
        The native backend also samples between main polling ticks when the
        event loop is available. The wider target and longer bounded lifetime
        tolerate delayed delivery; they cannot make a blocked GUI thread run.
        Offscreen tests may supply positions without reading/moving OS input.
        """
        if not self._active or self._validated:
            return False
        safe_rect = self.probe.geometry().adjusted(38, 30, -38, -30)
        if safe_rect.contains(position):
            return False
        self.probe.move(position - QPoint(self.probe.width() // 2, self.probe.height() // 2))
        self._raise_native_target()
        return True

    def sustain_for_shake(self, position: QPoint) -> bool:
        """Keep MIME inspection alive only while strong shake turns continue.

        A low-sensitivity gesture can legitimately outlast the initial probe.
        The full gesture and a delayed OLE DragEnter get a short grace period,
        while an ordinary drag with one turn still expires at its original time.
        """
        if not self._active:
            return self.arm(position)
        if self._validated:
            return False
        self.track_position(position)
        if self._timeout.remainingTime() < 1400:
            self._timeout.start(1400)
        return True

    def _follow_pointer(self):
        if not native_input_available() or not self._active:
            return
        if self.is_internal_drag() or native_window_move_active():
            self._finish_probe()
            return
        if not self.track_position(QCursor.pos()):
            # Capture software may repeatedly raise a non-interactive cursor
            # overlay above this OLE target without moving the pointer out of
            # its safe area. Restore ordering without activating the shelf.
            self._raise_native_target()

    def _accepts(self, mime) -> bool:
        return self._active and not self.is_internal_drag() and is_content_mime(mime)

    def _content_entered(self, position: QPoint):
        if not self._active or self._validated:
            return
        self._validated = True
        epoch = self._epoch
        # Finish the native DragEnter call before hiding its target. This
        # avoids reentrant OLE state changes and stale Windows drag visuals.
        def notify():
            self._deliver_validation(position, epoch)
        QTimer.singleShot(0, notify)

    def _deliver_validation(self, position: QPoint, epoch: int):
        if epoch != self._epoch or not self._validated or self._notified:
            return
        self._notified = True
        self._finish_probe()
        self.on_validated(QPoint(position))

    def _finish_probe(self):
        self._timeout.stop()
        self._follow.stop()
        self._active = False
        self.probe.hide()

    def reset_session(self):
        self._epoch += 1
        self._validated = False
        self._notified = False
        self._last_arm = -1.0
        self._finish_probe()

    def close(self):
        self.reset_session()
        self.probe.close()
        self.probe.deleteLater()
