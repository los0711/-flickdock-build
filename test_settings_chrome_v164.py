"""Frameless settings title controls, rollback and offscreen DPI regressions."""
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['QT_SCALE_FACTOR'] = sys.argv[1] if len(sys.argv)>1 else '1.25'

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QFont, QFontDatabase, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import app_settings as subject

app = QApplication([])
assert QFontDatabase.addApplicationFont(str(Path(__file__).with_name('fonts')/'HarmonyOS_Sans_SC_Regular.ttf')) >= 0
app.setFont(QFont('HarmonyOS Sans SC', 11))


def flush():
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def assert_centered_cross(button, dark):
    assert button.size().width() == button.size().height() == 40
    assert button.glyph_rect.size().width() == button.glyph_rect.size().height() == 14
    assert button.glyph_rect.center() == QRectF(button.rect()).center()
    image = button.grab().toImage()
    points = []
    for y in range(image.height()):
        for x in range(image.width()):
            color = image.pixelColor(x, y)
            ink = (min(color.red(), color.green(), color.blue())>150 if dark
                   else 60<color.red()<145 and 65<color.green()<150 and 70<color.blue()<170)
            if ink: points.append((x, y))
    assert points, 'Close cross is not visible'
    left, right = min(p[0] for p in points), max(p[0] for p in points)
    top, bottom = min(p[1] for p in points), max(p[1] for p in points)
    assert abs((left+right+1)/2-image.width()/2)<=1, (left, right, image.width())
    assert abs((top+bottom+1)/2-image.height()/2)<=1, (top, bottom, image.height())
    ratio = image.devicePixelRatio()
    assert 9*ratio <= right-left+1 <= 14*ratio+1
    assert 9*ratio <= bottom-top+1 <= 14*ratio+1


with tempfile.TemporaryDirectory(prefix='flickdock-settings-chrome-v164-') as temporary, \
     patch.object(subject, '_read_startup_value') as read_registry, \
     patch.object(subject, '_write_startup_value') as write_registry, \
     patch.object(subject, '_startup_runtime_allowed', return_value=False):
    store = subject.SettingsStore(Path(temporary)/'profile')
    store.update(dock_length=90)
    dialog = subject.SettingsDialog(store)
    previews = []
    dialog.preview_changed.connect(previews.append)
    flags = dialog.windowFlags()
    assert flags & Qt.WindowType.FramelessWindowHint
    assert not flags & Qt.WindowType.WindowCloseButtonHint
    assert not flags & Qt.WindowType.WindowTitleHint
    assert flags & Qt.WindowType.Tool
    assert dialog.windowTitle() == 'FlickDock 设置' and not dialog.isModal()
    assert dialog.close_button.accessibleName() == '关闭设置'
    assert not dialog.close_button.autoDefault() and not dialog.close_button.isDefault()

    saved = store.path.read_bytes()
    render_dir = os.getenv('FLICKDOCK_SETTINGS_CHROME_RENDER_DIR')
    if render_dir: Path(render_dir).mkdir(parents=True, exist_ok=True)
    for theme in ('light', 'dark'):
        for height in (520, 680):
            dialog.show(); dialog.resize(560, height); flush()
            dialog.theme_combo.setCurrentIndex(dialog.theme_combo.findData(theme))
            dialog.save_button.setFocus(); flush()
            assert dialog.title_bar.height() == 40
            assert dialog.title_bar.rect().contains(dialog.close_button.geometry())
            assert dialog.rect().contains(dialog.save_button.geometry())
            assert dialog.rect().contains(dialog.cancel_button.geometry())
            assert dialog.scroll.verticalScrollBar().maximum()>0
            assert dialog.scroll.y() >= dialog.title_bar.geometry().bottom()
            assert_centered_cross(dialog.close_button, theme=='dark')
            if render_dir and height==680:
                dialog.grab().save(str(Path(render_dir)/f'settings-chrome-{theme}-{os.environ["QT_SCALE_FACTOR"]}.png'))
            # The target's corner is clickable, independently of the cross ink.
            dialog.dock_position_spin.setValue(61)
            dialog.capture_mode_combo.setCurrentIndex(dialog.capture_mode_combo.findData('copy'))
            dialog.keep_empty_handle_checkbox.setChecked(True)
            dialog.shake_sensitivity_spin.setValue(91)
            assert previews[-1]['dock_position']==61
            QTest.mouseClick(dialog.close_button, Qt.MouseButton.LeftButton, pos=QPoint(2, 2))
            flush()
            assert not dialog.isVisible() and dialog.result()==dialog.DialogCode.Rejected
            assert previews[-1] is None and store.path.read_bytes()==saved
            dialog.show(); flush()
            assert dialog.dock_position_spin.value()==store.dock_position
            assert dialog.capture_mode_combo.currentData()=='reference'
            assert not dialog.keep_empty_handle_checkbox.isChecked()
            assert dialog.shake_sensitivity_spin.value()==50
            dialog.reject(); flush()
    print('PASS light/dark centered 14px SVG in 40px target, frameless flags, scrolling and close rollback at DPI', os.environ['QT_SCALE_FACTOR'])

    dialog.show(); flush()
    before = QPoint(dialog.pos())
    local = QPoint(150, 20)
    global_start = dialog.title_bar.mapToGlobal(local)
    delta = QPoint(43, 27)
    QTest.mousePress(dialog.title_bar, Qt.MouseButton.LeftButton, pos=local)
    moved = QMouseEvent(QEvent.Type.MouseMove, QPointF(local+delta), QPointF(global_start+delta),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(dialog.title_bar, moved)
    QTest.mouseRelease(dialog.title_bar, Qt.MouseButton.LeftButton, pos=local+delta)
    assert dialog.pos()==before+delta and dialog.title_bar._drag_offset is None
    assert store.path.read_bytes()==saved
    dialog.reject(); flush()
    print('PASS title drag moves only the offscreen settings window and does not save draft')

    for focus in (dialog.folder_edit, dialog.theme_combo, dialog.shelf_shortcut_edit):
        dialog.show(); flush()
        dialog.dock_position_spin.setValue(72)
        focus.setFocus(); flush()
        QTest.keyClick(focus, Qt.Key.Key_Escape)
        flush()
        assert not dialog.isVisible(), f'Esc did not reject from {focus.accessibleName()}'
        assert previews[-1] is None and store.path.read_bytes()==saved
    dialog.show(); flush()
    dialog.dock_position_spin.setValue(48)
    QTest.mouseClick(dialog.cancel_button, Qt.MouseButton.LeftButton)
    flush()
    assert not dialog.isVisible() and previews[-1] is None and store.path.read_bytes()==saved
    print('PASS Esc from text, combo and shortcut controls and footer Cancel all reject and roll back preview')

    dialog.show(); flush()
    dialog.capture_mode_combo.setCurrentIndex(dialog.capture_mode_combo.findData('copy'))
    dialog.trigger_view_combo.setCurrentIndex(dialog.trigger_view_combo.findData('expanded'))
    dialog.keep_empty_handle_checkbox.setChecked(True)
    dialog.shake_sensitivity_spin.setValue(82)
    dialog.dock_position_spin.setValue(49)
    QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
    flush()
    assert not dialog.isVisible() and dialog.result()==dialog.DialogCode.Accepted
    loaded = subject.SettingsStore(store.base)
    assert (loaded.capture_mode, loaded.trigger_view, loaded.keep_empty_handle, loaded.shake_sensitivity) == ('copy', 'expanded', True, 82)
    assert loaded.dock_position==49 and previews[-1] is None
    assert json.loads(store.path.read_text(encoding='utf8'))['dock_position']==49

    for other in (subject.AuthorDialog(dialog), subject.ThirdPartyDialog(dialog)):
        assert other.windowFlags() & Qt.WindowType.WindowCloseButtonHint
        assert not other.windowFlags() & Qt.WindowType.FramelessWindowHint
        other.deleteLater()
    read_registry.assert_not_called(); write_registry.assert_not_called()
    dialog.deleteLater(); flush()
    print('PASS Save retains all v1.6.3 fields; Author/licenses retain native window chrome; no registry I/O')

print('SETTINGS_CHROME_V164_OK')
