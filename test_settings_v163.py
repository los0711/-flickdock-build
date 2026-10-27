"""Real settings form/store regressions with isolated files and offscreen Qt."""
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['QT_SCALE_FACTOR'] = sys.argv[1] if len(sys.argv)>1 else '1.25'

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox

import app_settings as subject


app = QApplication([])
font_path = Path(__file__).with_name('fonts')/'HarmonyOS_Sans_SC_Regular.ttf'
assert QFontDatabase.addApplicationFont(str(font_path)) >= 0
app.setFont(QFont('HarmonyOS Sans SC', 11))


def flush():
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def assert_rejected(store, **value):
    before = dict(store._data)
    saved = store.path.read_bytes() if store.path.exists() else None
    try: store.update(**value)
    except ValueError: pass
    else: raise AssertionError(f'Invalid setting accepted: {value!r}')
    assert store._data == before
    assert (store.path.read_bytes() if store.path.exists() else None) == saved


def assert_tick_visible(checkbox):
    checkbox.setChecked(True)
    image = checkbox.grab().toImage()
    ratio = image.devicePixelRatio()
    rect = checkbox.indicator_rect().adjusted(2, 2, -2, -2)
    colors = [image.pixelColor(x, y)
              for y in range(round(rect.top()*ratio), round((rect.bottom()+1)*ratio))
              for x in range(round(rect.left()*ratio), round((rect.right()+1)*ratio))]
    white = sum(min(c.red(), c.green(), c.blue()) >= 254 for c in colors)
    blue = sum(c.blue()>175 and c.red()<100 and c.green()<180 for c in colors)
    assert white >= 5 and blue >= 15, (checkbox.text(), white, blue, ratio)
    before = checkbox.isChecked()
    QTest.mouseClick(checkbox, Qt.MouseButton.LeftButton, pos=checkbox.indicator_rect().center())
    assert checkbox.isChecked() != before


with tempfile.TemporaryDirectory(prefix='flickdock-settings-v163-') as temporary, \
     patch.object(subject, '_read_startup_value') as read_registry, \
     patch.object(subject, '_write_startup_value') as write_registry, \
     patch.object(subject, '_startup_runtime_allowed', return_value=False):
    root = Path(temporary)
    store = subject.SettingsStore(root/'profile')
    assert store.capture_mode == 'reference' and store.keep_empty_handle
    assert store.trigger_view == 'compact' and store.shake_sensitivity == 50
    assert store.sync_upload_enabled and store.sync_receive_enabled
    assert store.autostart and not store.path.exists()
    assert not store.reconcile_startup()
    changes = []
    store.changed.connect(lambda: changes.append(dict(store._data)))
    assert store.update(capture_mode='copy', keep_empty_handle=True, trigger_view='expanded', shake_sensitivity=100)
    loaded = subject.SettingsStore(store.base)
    assert (loaded.capture_mode, loaded.keep_empty_handle, loaded.trigger_view, loaded.shake_sensitivity) == ('copy', True, 'expanded', 100)
    assert loaded.autostart and len(changes) == 1
    assert not store.update(capture_mode='copy', keep_empty_handle=True, trigger_view='expanded', shake_sensitivity=100)
    for value in (0, 101, True, 1.5, '50'):
        assert_rejected(store, shake_sensitivity=value)
    for value in ('move', '', False): assert_rejected(store, capture_mode=value)
    for value in ('overview', '', False): assert_rejected(store, trigger_view=value)
    for value in (1, 'false'): assert_rejected(store, keep_empty_handle=value)
    for value in (0, 'false'): assert_rejected(store, sync_upload_enabled=value)
    for value in (1, 'true'): assert_rejected(store, sync_receive_enabled=value)
    assert store.update(sync_upload_enabled=False, sync_receive_enabled=False)
    disabled = subject.SettingsStore(store.base)
    assert not disabled.sync_upload_enabled and not disabled.sync_receive_enabled
    assert store.update(sync_upload_enabled=True, sync_receive_enabled=True)
    assert store.update(shake_sensitivity=1)
    assert store.update(shake_sensitivity=50)
    print('PASS defaults, persistent new settings, strict validation and sensitivity boundaries')

    legacy = root/'legacy'
    legacy.mkdir()
    (legacy/'settings.json').write_text(json.dumps({'autostart': False, 'capture_mode': 'move', 'keep_empty_handle': 1,
        'trigger_view': 'other', 'shake_sensitivity': True}), encoding='utf8')
    old = subject.SettingsStore(legacy)
    assert not old.autostart
    assert (old.capture_mode, old.keep_empty_handle, old.trigger_view, old.shake_sensitivity) == ('reference', True, 'compact', 50)
    old.update(shake_sensitivity=22)
    assert not subject.SettingsStore(legacy).autostart
    print('PASS old profiles keep explicit autostart=False; invalid new fields fall back independently')

    store.update(capture_mode='reference', keep_empty_handle=False, trigger_view='compact', shake_sensitivity=50)
    dialog = subject.SettingsDialog(store)
    previews = []
    dialog.preview_changed.connect(previews.append)
    dialog.show(); flush()
    saved = store.path.read_bytes()
    events = len(changes)
    dialog.capture_mode_combo.setCurrentIndex(dialog.capture_mode_combo.findData('copy'))
    dialog.keep_empty_handle_checkbox.setChecked(True)
    dialog.trigger_view_combo.setCurrentIndex(dialog.trigger_view_combo.findData('expanded'))
    dialog.shake_sensitivity_slider.setValue(87)
    assert dialog.shake_sensitivity_spin.value() == 87
    dialog.shake_sensitivity_spin.setValue(23)
    assert dialog.shake_sensitivity_slider.value() == 23
    QTest.mouseClick(dialog.shake_sensitivity_reset_button, Qt.MouseButton.LeftButton)
    assert dialog.shake_sensitivity_spin.value() == dialog.shake_sensitivity_slider.value() == 50
    dialog.shake_sensitivity_spin.setValue(73)
    assert dialog.draft()['capture_mode'] == 'copy'
    assert dialog.draft()['keep_empty_handle'] and dialog.draft()['trigger_view'] == 'expanded'
    assert dialog.draft()['shake_sensitivity'] == 73
    assert store.path.read_bytes() == saved and len(changes) == events
    count = len(previews)
    dialog.dock_position_spin.setValue(33)
    assert len(previews)>count and previews[-1]['dock_position'] == 33
    assert not {'capture_mode', 'keep_empty_handle', 'trigger_view', 'shake_sensitivity'}.intersection(previews[-1])
    dialog.reject(); flush()
    assert previews[-1] is None and store.path.read_bytes() == saved
    dialog.show(); flush()
    assert (dialog.capture_mode_combo.currentData(), dialog.keep_empty_handle_checkbox.isChecked(),
            dialog.trigger_view_combo.currentData(), dialog.shake_sensitivity_spin.value()) == ('reference', False, 'compact', 50)
    print('PASS slider/spin/reset, draft-only changes, Cancel restores form and dock preview remains live')

    render_dir = os.getenv('FLICKDOCK_SETTINGS_RENDER_DIR')
    if render_dir: Path(render_dir).mkdir(parents=True, exist_ok=True)
    for theme in ('light', 'dark'):
        dialog.theme_combo.setCurrentIndex(dialog.theme_combo.findData(theme))
        dialog.resize(560, 520); flush()
        assert dialog.scroll.verticalScrollBar().maximum() > 0
        assert dialog.rect().contains(dialog.save_button.geometry())
        assert dialog.shake_sensitivity_reset_button.dark == (theme == 'dark')
        for checkbox in dialog.findChildren(QCheckBox):
            assert isinstance(checkbox, subject.SettingsCheckBox)
            assert_tick_visible(checkbox)
            checkbox.setChecked(True)
        dialog.scroll.ensureWidgetVisible(dialog.shake_sensitivity_reset_button)
        flush()
        spin = dialog.shake_sensitivity_spin
        assert spin.width() >= spin.fontMetrics().horizontalAdvance('100')+30
        assert dialog.capture_mode_combo.font().family() == 'HarmonyOS Sans SC'
        if render_dir: dialog.grab().save(str(Path(render_dir)/f'settings-{theme}-{os.environ["QT_SCALE_FACTOR"]}.png'))
    print('PASS light/dark vector checkbox ticks, Harmony font, reset SVG and scrollable form at DPI', os.environ['QT_SCALE_FACTOR'])

    dialog.capture_mode_combo.setCurrentIndex(dialog.capture_mode_combo.findData('copy'))
    dialog.keep_empty_handle_checkbox.setChecked(True)
    dialog.trigger_view_combo.setCurrentIndex(dialog.trigger_view_combo.findData('expanded'))
    dialog.shake_sensitivity_spin.setValue(81)
    dialog.startup_checkbox.setChecked(False)
    dialog.folder_edit.setText(str(root/'chosen'/'saved'))
    QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
    flush()
    assert not dialog.isVisible() and previews[-1] is None
    persisted = subject.SettingsStore(store.base)
    assert (persisted.capture_mode, persisted.keep_empty_handle, persisted.trigger_view, persisted.shake_sensitivity) == ('copy', True, 'expanded', 81)
    assert not persisted.autostart and persisted.capture_dir == root/'chosen'/'saved'
    assert persisted.capture_dir.is_dir()
    assert json.loads(store.path.read_text(encoding='utf8'))['shake_sensitivity'] == 81
    read_registry.assert_not_called(); write_registry.assert_not_called()
    dialog.deleteLater(); flush()
    print('PASS actual Save button commits the new settings and preserves explicit autostart off without registry I/O')

print('SETTINGS_V163_OK')
