"""Regression checks for content validation and probe lifetime."""
import os
import tempfile
os.environ["QT_QPA_PLATFORM"] = "offscreen"
isolated_state = tempfile.TemporaryDirectory(prefix="flickdock-probe-test-")
os.environ["APPDATA"] = isolated_state.name

from PySide6.QtCore import QEventLoop, QMimeData, QPoint, QPointF, QTimer, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QImage
from PySide6.QtWidgets import QApplication
import drag_probe as mod


app = QApplication([])
original_windll = mod.ctypes.WinDLL
def forbidden_native_call(*args, **kwargs):
    raise AssertionError("offscreen probe tests must not call native Windows APIs")
mod.ctypes.WinDLL = forbidden_native_call
calls = []
drops = []
gate = mod.ContentDragGate(lambda p: calls.append(p),
                           lambda mime, pos: drops.append(mime.text()) or True,
                           probe_timeout_ms=80)
assert not mod._native_windows_enabled()
assert not mod.native_window_move_active()


def enter(mime, target=None):
    event = QDragEnterEvent(QPoint(48, 48), Qt.DropAction.CopyAction, mime,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent((target or gate).probe, event)
    return event


# Holding and shaking alone cannot validate anything. An ordinary window drag
# does not generate an OLE DragEnter and the probe quietly expires.
assert gate.arm(QPoint(300, 300))
wait = QEventLoop()
QTimer.singleShot(120, wait.quit)
wait.exec()
assert not calls and not gate.active and not gate.probe.isVisible()
assert not gate._follow.isActive(), "offscreen tests never sample real OS pointer input"

# The complete target has alpha=1. Source composition prevents repeated paints
# from slowly darkening the sensor or making transparent holes at its edges.
gate.reset_session()
assert gate.arm(QPoint(300, 300))
pixels = gate.probe.grab().toImage()
assert pixels.pixelColor(0, 0).alpha() == 1
assert pixels.pixelColor(pixels.width() - 1, pixels.height() - 1).alpha() == 1
gate.probe.repaint()
assert gate.probe.grab().toImage().pixelColor(20, 20).alpha() == 1
remaining = gate._timeout.remainingTime()
for position in (QPoint(500, 300), QPoint(100, 500), QPoint(1500, 550)):
    assert gate.track_position(position)
    assert gate.probe.geometry().contains(position)
assert gate._timeout.remainingTime() <= remaining + 5, "tracking cannot extend bounded probe lifetime"
gate.reset_session()

# An unsupported custom drag never opens the shelf.
gate.reset_session()
assert gate.arm(QPoint(300, 300))
unsupported = QMimeData()
unsupported.setData("application/x-test-window", b"window")
assert not enter(unsupported).isAccepted()
assert not calls
gate.reset_session()

# Files, raw images and text all validate. No MIME object survives the event.
text = QMimeData()
text.setText("a real text drag")
files = QMimeData()
files.setUrls([QUrl.fromLocalFile(r"C:\Windows\win.ini")])
image = QMimeData()
image.setImageData(QImage(2, 2, QImage.Format.Format_ARGB32))
for mime in (text, files, image):
    gate.reset_session()
    assert gate.arm(QPoint(300, 300))
    assert enter(mime).isAccepted()
    app.processEvents()
    assert gate.validated and not gate.active and not gate.probe.isVisible()
    assert not gate.arm(QPoint(310, 300)), "one callback per held-mouse session"
assert len(calls) == 3

# Releasing the mouse cancels any validation queued in the same event turn.
gate.reset_session()
assert gate.arm(QPoint(300, 300))
enter(text)
gate.reset_session()
app.processEvents()
assert len(calls) == 3

# Native moving/resizing and outgoing shelf drags are rejected before probing.
original = mod.native_window_move_active
mod.native_window_move_active = lambda: True
assert not gate.arm(QPoint(300, 300))
mod.native_window_move_active = original
gate.is_internal_drag = lambda: True
assert not gate.arm(QPoint(300, 300))
gate.is_internal_drag = lambda: False

# Rare release on the sensor stores content synchronously, without holding an
# event-owned QMimeData pointer after the OLE call returns.
gate.reset_session()
assert gate.arm(QPoint(300, 300))
drop = QDropEvent(QPointF(48, 48), Qt.DropAction.CopyAction, text,
                  Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
gate.probe.dropEvent(drop)
assert drops == ["a real text drag"] and drop.isAccepted()
assert not gate.probe.isVisible()
app.processEvents()
assert len(calls) == 4, "release during validation must reveal once, never twice"

gate.close()
app.processEvents()

# A browser may supply only HTML with no text/plain, imageData or uri-list.
# Such a drag is accepted only when its parsed image/link URL is supported.
html_calls = []
html_gate = mod.ContentDragGate(lambda pos: html_calls.append(pos), probe_timeout_ms=80)
for markup in ('<img src="https://example.invalid/image">',
               '<a href="https://example.invalid/page">Page</a>',
               '<img src="data:image/png;base64,aGVsbG8=">'):
    payload = QMimeData()
    payload.setHtml(markup)
    assert not payload.hasText() and not payload.hasUrls() and not payload.hasImage()
    assert mod.is_content_mime(payload)
    html_gate.reset_session()
    assert html_gate.arm(QPoint(300, 300))
    assert enter(payload, html_gate).isAccepted()
    app.processEvents()
assert len(html_calls) == 3
for markup in ('<p><strong>Formatting only</strong></p>',
               '<img src="file:///private/image.png">',
               '<a href="javascript:alert(1)">Unsupported</a>'):
    payload = QMimeData()
    payload.setHtml(markup)
    assert not mod.is_content_mime(payload)
    html_gate.reset_session()
    assert html_gate.arm(QPoint(300, 300))
    assert not enter(payload, html_gate).isAccepted()
    app.processEvents()
assert len(html_calls) == 3
html_gate.close()
app.processEvents()

# Simulate a GUI busy/capture period: a real payload arriving 620ms after the
# recognized shake still validates; the old 240ms lifetime would miss it.
late_calls = []
late = mod.ContentDragGate(lambda pos: late_calls.append(pos))
assert late.probe_timeout_ms == 1200
assert late.arm(QPoint(300, 300))
late_wait = QEventLoop()
def delayed_ole():
    event = QDragEnterEvent(QPoint(112, 88), Qt.DropAction.CopyAction, text,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(late.probe, event)
    assert event.isAccepted()
QTimer.singleShot(620, delayed_ole)
QTimer.singleShot(720, late_wait.quit)
late_wait.exec()
assert len(late_calls) == 1 and not late.active
late.close()
app.processEvents()
mod.ctypes.WinDLL = original_windll
isolated_state.cleanup()
print("CONTENT_DRAG_GATE_OK")
