"""Real settings gestures -> saved settings -> manager detector configuration.

All profile files and IPC names are temporary. Native input, hotkey and
registry access are mocked; Qt events only target the offscreen application.
"""
import contextlib
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['QT_SCALE_FACTOR'] = '1.25'

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QMimeData, QObject, QPoint, QPointF, QRect, QTimer, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionSlider, QSystemTrayIcon


class Screen:
    def availableGeometry(self): return QRect(0, 0, 1920, 1080)
    def devicePixelRatio(self): return 1.25


class LocalResolver(QObject):
    def __init__(self, *args, parent=None, **kwargs): super().__init__(parent)
    @property
    def pending_refs(self): return frozenset()
    def cancel(self, ref): pass
    def cancel_all(self): pass
    def close(self): pass


def flush():
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def run_for(milliseconds):
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


def drag_slider(slider, to_maximum):
    option = QStyleOptionSlider()
    slider.initStyleOption(option)
    handle = slider.style().subControlRect(QStyle.ComplexControl.CC_Slider, option,
        QStyle.SubControl.SC_SliderHandle, slider)
    start = handle.center()
    finish = QPoint(slider.width()-1 if to_maximum else 0, start.y())
    QTest.mousePress(slider, Qt.MouseButton.LeftButton, pos=start)
    move = QMouseEvent(QEvent.Type.MouseMove, QPointF(finish), QPointF(slider.mapToGlobal(finish)),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(slider, move)
    QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=finish)
    return slider.value()


failures = []
def check(name, operation):
    try: operation()
    except BaseException as error:
        failures.append((name, repr(error)))
        print('FAIL', name, repr(error), flush=True)
    else: print('PASS', name, flush=True)


with tempfile.TemporaryDirectory(prefix='flickdock-settings-gesture-v165-') as temporary:
    root = Path(temporary)
    os.environ['APPDATA'] = str(root/'profile')
    import app_settings
    import flickdock as subject
    from global_hotkeys import _NoopBackend
    from shake_detector import config_for_sensitivity
    app = QApplication([])
    subject.load_font(app)
    with contextlib.ExitStack() as mocks:
        for owner, name, value in [
            (subject, 'motion_enabled', lambda: False),
            (subject, 'ContentResolver', LocalResolver),
            (subject.QCursor, 'pos', lambda *args: QPoint(500, 350)),
            (subject.QGuiApplication, 'screenAt', lambda *args: Screen()),
            (subject.QGuiApplication, 'primaryScreen', lambda *args: Screen()),
            (subject.ctypes.windll.user32, 'GetAsyncKeyState', lambda key: 0),
            (subject, 'native_window_move_active', lambda: False),
            (app_settings, '_read_startup_value', lambda: None),
            (app_settings, '_write_startup_value', lambda *args: None),
            (QSystemTrayIcon, 'show', lambda self: None),
            (QSystemTrayIcon, 'hide', lambda self: None),
        ]: mocks.enter_context(patch.object(owner, name, value))
        for name in ('RegisterHotKey', 'UnregisterHotKey'):
            mocks.enter_context(patch.object(subject.ctypes.windll.user32, name,
                side_effect=AssertionError('Native hotkey registration is forbidden')))
        manager = subject.ShelfManager(app)
        manager.timer.stop()
        assert isinstance(manager.hotkeys.backend, _NoopBackend)
        records = []
        manager.settings.changed.connect(lambda: records.append({
            'saved': manager.settings.shake_sensitivity, 'detector': asdict(manager.shake_detector.config)}))
        manager.show_settings(); flush()
        dialog = manager.settings_dialog
        moved = []
        dialog.shake_sensitivity_slider.sliderMoved.connect(moved.append)

        def save_dragged(value):
            manager.show_settings(); flush()
            assert manager.settings_dialog is dialog
            dialog.scroll.ensureWidgetVisible(dialog.shake_sensitivity_slider); flush()
            previous_value = manager.settings.shake_sensitivity
            previous_detector = manager.shake_detector
            previous_config = manager.shake_detector.config
            previous_signals = len(records)
            assert drag_slider(dialog.shake_sensitivity_slider, value==100)==value
            assert dialog.shake_sensitivity_spin.value()==value
            # Gesture detection previews live; persistence waits for Save.
            assert manager.settings.shake_sensitivity==previous_value
            assert manager.shake_detector.config==config_for_sensitivity(value)
            assert str(config_for_sensitivity(value).reversals) in dialog.shake_effort_label.text()
            assert len(records)==previous_signals
            QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
            flush()
            assert not dialog.isVisible()
            assert manager.settings.shake_sensitivity==value
            assert manager.shake_detector.config==config_for_sensitivity(value)
            if previous_value!=value:
                assert manager.shake_detector is not previous_detector and len(records)==previous_signals+1
            assert json.loads(manager.settings.path.read_text(encoding='utf8'))['shake_sensitivity']==value
            print('OBSERVED actual saved detector', value, json.dumps(asdict(manager.shake_detector.config)), flush=True)
            return manager.shake_detector.config

        configs = {}
        def endpoints():
            configs[1] = save_dragged(1)
            configs[100] = save_dragged(100)
            assert configs[1]!=configs[100] and moved
            assert configs[1].reversals>configs[100].reversals
        check('actual slider drags 1/100 immediately configure detection; Save persists the same value', endpoints)

        def keyboard_reset_reuse():
            manager.show_settings(); flush()
            assert manager.settings_dialog is dialog
            previous = manager.settings.shake_sensitivity
            previous_detector = manager.shake_detector
            editor = dialog.shake_sensitivity_spin.lineEdit()
            editor.selectAll()
            QTest.keyClicks(editor, '1')
            QTest.keyClick(editor, Qt.Key.Key_Tab)
            assert dialog.shake_sensitivity_spin.value()==dialog.shake_sensitivity_slider.value()==1
            QTest.keyClick(dialog.shake_sensitivity_spin, Qt.Key.Key_Up)
            assert dialog.shake_sensitivity_spin.value()==dialog.shake_sensitivity_slider.value()==2
            QTest.mouseClick(dialog.shake_sensitivity_reset_button, Qt.MouseButton.LeftButton)
            assert dialog.shake_sensitivity_spin.value()==dialog.shake_sensitivity_slider.value()==50
            assert manager.settings.shake_sensitivity==previous
            assert manager.shake_detector.config==config_for_sensitivity(50)
            QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton); flush()
            assert manager.settings.shake_sensitivity==50
            assert manager.shake_detector.config==config_for_sensitivity(50)
            manager.show_settings(); flush()
            assert manager.settings_dialog is dialog and dialog.shake_sensitivity_spin.value()==50
            drag_slider(dialog.shake_sensitivity_slider, False)
            manager.show_settings(); flush()
            print('OBSERVED reuse while already visible reloads form value', dialog.shake_sensitivity_spin.value(), flush=True)
            assert manager.settings.shake_sensitivity==50 and manager.shake_detector.config==config_for_sensitivity(50)
            dialog.close_button.click(); flush()
            manager.show_settings(); flush()
            assert dialog.shake_sensitivity_spin.value()==50
            dialog.cancel_button.click(); flush()
        check('spin text/arrow/reset preview live; the reused settings form restores saved state on Cancel', keyboard_reset_reuse)

        def cancel_live_preview():
            saved=manager.settings.shake_sensitivity
            manager.show_settings();flush()
            drag_slider(dialog.shake_sensitivity_slider,saved!=100)
            assert manager.shake_detector.config!=config_for_sensitivity(saved)
            dialog.cancel_button.click();flush()
            assert manager.shake_detector.config==config_for_sensitivity(saved)
            assert manager.settings.shake_sensitivity==saved
        check('Cancel rolls back the live gesture detector without writing the preview',cancel_live_preview)

        def constructor_load():
            save_dragged(100)
            loaded = app_settings.SettingsStore(manager.settings.base)
            assert loaded.shake_sensitivity==100
            second = subject.ShelfManager(app)
            second.timer.stop()
            assert second.settings.shake_sensitivity==100
            assert second.shake_detector.config==config_for_sensitivity(100)
            second.quit()
            flush()
        check('fresh store and manager constructor load the saved sensitivity without a default/cache override', constructor_load)

        def active_probe_after_save():
            save_dragged(100)
            manager.show_settings(); flush()
            drag_slider(dialog.shake_sensitivity_slider, False)
            gate = manager.drag_gate
            assert gate.arm(QPoint(500, 350))
            previous_epoch = gate._epoch
            try:
                # Use the real button signal synchronously so no event-loop
                # turn obscures the order of configuration and probe cleanup.
                dialog.save_button.click()
                assert manager.shake_detector.config==config_for_sensitivity(1)
                print('OBSERVED active probe after Save', gate.active, gate._epoch, previous_epoch, flush=True)
                assert not gate.active and not gate.validated and not gate.probe.isVisible(), 'The old-threshold probe survived a sensitivity change'
            finally:
                gate.reset_session(); manager.shelf.hide_immediately(); flush()
        check('saving a new sensitivity cancels the already armed probe from the previous threshold', active_probe_after_save)

        def queued_probe_after_save():
            save_dragged(100)
            manager.shelf.hide_immediately()
            manager.show_settings(); flush()
            drag_slider(dialog.shake_sensitivity_slider, False)
            gate = manager.drag_gate
            assert gate.arm(QPoint(500, 350))
            original = root/'drag.txt';original.write_text('Local MIME validation fixture')
            mime = QMimeData();mime.setUrls([QUrl.fromLocalFile(str(original))])
            event = QDragEnterEvent(QPoint(20, 20), Qt.DropAction.CopyAction, mime,
                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(gate.probe, event)
            assert event.isAccepted() and gate.validated
            try:
                dialog.save_button.click()
                assert manager.settings.shake_sensitivity==1
                run_for(35)
                print('OBSERVED queued old-threshold validation after Save; shelf visible', manager.shelf.isVisible(), flush=True)
                assert not manager.shelf.isVisible(), 'Queued validation from the former threshold reopened the shelf after Save'
            finally:
                gate.reset_session(); manager.shelf.hide_immediately(); flush()
        check('a queued validation from the previous sensitivity cannot reveal the shelf after Save', queued_probe_after_save)

        manager.quit(); flush()

if failures:
    raise AssertionError(f'{len(failures)} settings gesture regressions: {failures!r}')
print('SETTINGS_GESTURE_V165_OK')
