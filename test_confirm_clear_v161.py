"""Child confirmation regressions; offscreen only, with selectable DPI scale."""
import os
from pathlib import Path
import sys
import tempfile

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_SCALE_FACTOR"] = sys.argv[1] if len(sys.argv) > 1 else "1.0"
state = tempfile.TemporaryDirectory(prefix="flickdock-confirm-v161-")
os.environ["APPDATA"] = state.name

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFrame, QLineEdit, QMessageBox, QWidget
from shiboken6 import isValid
from confirm_clear import ClearConfirmOverlay, MODAL_PROPERTY, confirmation_active


app = QApplication([])
font_path = Path(__file__).with_name("fonts") / "HarmonyOS_Sans_SC_Regular.ttf"
if font_path.exists():
    QFontDatabase.addApplicationFont(str(font_path))
app.setFont(QFont("HarmonyOS Sans SC", 11))
host = QWidget()
host.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                    Qt.WindowType.WindowStaysOnTopHint)
host.resize(788, 450)
card = QFrame(host)
card.setGeometry(host.rect())
field = QLineEdit("behind overlay", card)
field.setGeometry(20, 20, 150, 30)
host.show()
host.activateWindow()
app.processEvents()


def flush():
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def assert_layout(dialog, expect_compact=False):
    assert dialog.parentWidget() is host and dialog.window() is host and not dialog.isWindow()
    assert not dialog.testAttribute(Qt.WidgetAttribute.WA_NativeWindow)
    assert dialog.geometry() == QRect(card.mapTo(host,QPoint()),card.size())
    assert dialog.rect().contains(dialog.panel.geometry())
    assert not dialog.description.wordWrap()
    # The 340px reference sheet fits its shorter full explanation even on the
    # old compact host. Only use the compact variant when measured space needs it.
    expected = (dialog.description.full_text if dialog.description.fontMetrics().horizontalAdvance(dialog.description.full_text)<=dialog.description.width()
                else dialog.description.compact_text)
    assert dialog.description.text() == expected, (dialog.description.text(), expected, dialog.description.width())
    assert "\n" not in dialog.description.text() and "…" not in dialog.description.text()
    assert dialog.description.fontMetrics().horizontalAdvance(dialog.description.text()) <= dialog.description.width()
    for button in (dialog.cancel_button, dialog.delete_button):
        bounds = QRect(button.mapTo(dialog.panel, QPoint()), button.size())
        assert dialog.panel.rect().contains(bounds), (bounds, dialog.panel.rect())
        assert button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 36
        assert button.height() >= button.fontMetrics().height() + 10
        assert button.isVisible()


def execute(dialog, action):
    errors = []
    def run():
        try:
            assert confirmation_active(host) and confirmation_active(card)
            assert dialog.cancel_button.hasFocus()
            action()
        except BaseException as error:
            errors.append(error)
            dialog.cancel()
    watchdog = QTimer(dialog)
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(dialog.cancel)
    watchdog.start(1000)
    QTimer.singleShot(0, run)
    result = dialog.exec()
    if isValid(dialog):
        watchdog.stop()
    if errors:
        raise errors[0]
    if isValid(host):
        assert not confirmation_active(host) and not confirmation_active(card)
    return result


try:
    # Flags prove the old code creates another window without inheriting the
    # explicit topmost bit. This is not a claim of desktop Z-order testing.
    legacy = QMessageBox(host)
    assert host.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert legacy.isWindow() and not (legacy.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
    legacy.deleteLater()
    print("PASS old topmost-parent/separate-dialog flag mismatch is documented")

    for theme in ("light", "dark"):
        for width, height, compact in ((788, 450, False), (394, 294, True)):
            card.setGeometry(0, 0, width, height)
            dialog = ClearConfirmOverlay(card, 125, font_family="HarmonyOS Sans SC", theme=theme)
            results = []
            dialog.finished.connect(results.append)
            def cancel_visible():
                assert_layout(dialog, compact)
                assert dialog.cancel_button.isDefault() and not dialog.delete_button.isDefault()
                assert dialog.cancel_button.text() == "取消" and dialog.delete_button.text() == "清空"
                assert ("rgba(28,28,30,214)" if theme == "dark" else "rgba(255,255,255,214)") in dialog.panel.styleSheet()
                dialog.cancel_button.click()
            assert execute(dialog, cancel_visible) is False
            assert results == [False]
            dialog.deleteLater()
            flush()
    print("PASS full/compact one-line copy and complete cancel/delete buttons in light/dark")

    card.setGeometry(0, 0, 788, 450)
    for key, target_name in ((Qt.Key.Key_Return, "cancel_button"),
                             (Qt.Key.Key_Enter, "delete_button"),
                             (Qt.Key.Key_Escape, "delete_button")):
        dialog = ClearConfirmOverlay(card, 10)
        def press_default():
            target = getattr(dialog, target_name)
            target.setFocus()
            QTest.keyClick(target, key)
        assert execute(dialog, press_default) is False
        dialog.deleteLater()
        flush()
    dialog = ClearConfirmOverlay(card, 10)
    def space_never_confirms():
        dialog.delete_button.setFocus()
        QTest.keyClick(dialog.delete_button, Qt.Key.Key_Space)
        assert dialog.active and not dialog.result
        QTest.keyClicks(field, "must not edit")
        assert field.text() == "behind overlay"
        dialog.cancel_button.click()
    assert execute(dialog, space_never_confirms) is False
    dialog.deleteLater()
    flush()
    print("PASS Enter/Escape cancel and keyboard cannot confirm deletion or edit covered content")

    dialog = ClearConfirmOverlay(card, 10)
    signals = []
    dialog.finished.connect(signals.append)
    def click_delete():
        assert_layout(dialog)
        QTest.mouseClick(dialog.delete_button, Qt.MouseButton.LeftButton)
    assert execute(dialog, click_delete) is True
    assert signals == [True]
    assert execute(dialog, lambda: dialog.cancel_button.click()) is False
    assert signals == [True, False]
    dialog.deleteLater()
    flush()
    print("PASS only explicit delete activation returns True and repeat use emits once")

    dialog = ClearConfirmOverlay(card, 10)
    def resize_then_cancel():
        card.resize(850, 520)
        flush()
        assert_layout(dialog)
        card.resize(394, 294)
        flush()
        assert_layout(dialog, True)
        other = ClearConfirmOverlay(card, 10)
        assert other.exec() is False and dialog.active and confirmation_active(host)
        other.deleteLater()
        dialog.cancel_button.click()
    assert execute(dialog, resize_then_cancel) is False
    dialog.deleteLater()
    flush()
    print("PASS resize tracking and duplicate-overlay rejection preserve the active modal scope")

    dialog = ClearConfirmOverlay(card, 10)
    assert execute(dialog, lambda: host.hide()) is False
    assert not dialog.active and not confirmation_active(host)
    dialog.deleteLater()
    flush()
    host.show()
    host.activateWindow()
    flush()
    disposable = QWidget()
    disposable.resize(788, 450)
    disposable.show()
    disappearing = ClearConfirmOverlay(disposable, 10)
    QTimer.singleShot(0, disposable.deleteLater)
    assert disappearing.exec() is False
    assert not isValid(disappearing)
    print("PASS parent hide/delete unwinds the nested event loop without a native window")
    print("CONFIRM_CLEAR_V161_OK scale=" + os.environ["QT_SCALE_FACTOR"])
finally:
    if isValid(host):
        host.close()
    flush()
    state.cleanup()
