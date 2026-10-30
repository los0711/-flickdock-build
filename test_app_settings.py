"""Isolated settings tests. Registry access is mocked and Qt is offscreen."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QLineEdit, QColorDialog, QDialog
from PySide6.QtGui import QFont, QFontDatabase, QColor
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
import app_settings as subject


app = QApplication([])
font_path = Path(__file__).parent / "fonts" / "HarmonyOS_Sans_SC_Regular.ttf"
if font_path.exists():
    font_id = QFontDatabase.addApplicationFont(str(font_path))
    names = QFontDatabase.applicationFontFamilies(font_id)
    if names:
        app.setFont(QFont(names[0], 11))
with tempfile.TemporaryDirectory(prefix="flickdock-settings-") as temporary:
    base = Path(temporary) / "profile"
    writes = []
    with patch.object(subject, "_read_startup_value", return_value=None) as read, \
         patch.object(subject, "_write_startup_value", side_effect=writes.append):
        store = subject.SettingsStore(base)
        assert store.theme == "light" and store.autostart
        assert store.capture_dir == base / "saved"
        assert store.view_mode == "list"
        assert store.shelf_shortcut == "Ctrl+Alt+Space"
        assert store.overview_shortcut == "Ctrl+Alt+Shift+Space"
        assert (store.dock_edge, store.dock_position, store.dock_length, store.dock_thickness) == ("right", 8, 76, 8)
        assert store.trigger_visible and store.dock_color == "#567daf" and store.dock_opacity == 100
        read.assert_not_called()
        assert writes == [] and not store.path.exists()
        changes = []
        store.changed.connect(lambda: changes.append(store.theme))
        assert store.update(theme="dark")
        assert store.theme == "dark" and changes == ["dark"]
        read.assert_not_called()
        assert writes == []
        assert not store.update(theme="dark", autostart=True)
        assert len(changes) == 1 and not writes
        assert store.update(capture_dir=Path(temporary) / "images")
        assert store.capture_dir.is_dir()
        assert list(store.capture_dir.iterdir()) == []
        assert store.update(autostart=False)
        assert store.update(autostart=True)
        assert store.update(autostart=False)
        assert not writes
        read.assert_not_called()
        loaded = subject.SettingsStore(base)
        assert loaded.theme == "dark" and loaded.capture_dir == store.capture_dir and not loaded.autostart
        current = json.loads(store.path.read_text(encoding="utf8"))
        assert current["version"] == 1
    print("PASS settings persist; load and theme changes never write startup registry")

    with patch.object(subject, "_read_startup_value") as read, \
         patch.object(subject, "_write_startup_value") as write:
        assert store.update(view_mode="icons", shelf_shortcut="ctrl+alt+o", overview_shortcut="ctrl+alt+shift+o")
        assert store.view_mode == "icons" and store.shelf_shortcut == "Ctrl+Alt+O"
        assert store.overview_shortcut == "Ctrl+Alt+Shift+O"
        read.assert_not_called()
        write.assert_not_called()
        loaded = subject.SettingsStore(base)
        assert loaded.view_mode == "icons" and loaded.shelf_shortcut == store.shelf_shortcut
    print("PASS presentation mode and normalized global shortcuts persist")

    capture_before = store.capture_dir
    with patch.object(subject, "_read_startup_value") as read, \
         patch.object(subject, "_write_startup_value") as write:
        for edge in subject.DOCK_EDGES:
            assert store.update(dock_edge=edge, dock_position=100, dock_length=600, dock_thickness=40)
            loaded = subject.SettingsStore(base)
            assert (loaded.dock_edge, loaded.dock_position, loaded.dock_length, loaded.dock_thickness) == (edge, 100, 600, 40)
            assert loaded.capture_dir == capture_before
        assert store.update(dock_edge="right", dock_position=0, dock_length=32, dock_thickness=2)
        read.assert_not_called()
        write.assert_not_called()
    print("PASS every dock edge, position and trigger dimensions persist without changing saved content location")

    invalid = Path(temporary) / "a-file"
    invalid.write_text("not a folder", encoding="utf8")
    for values in ({"theme": "unknown"}, {"capture_dir": "relative/path"},
                   {"capture_dir": invalid}, {"autostart": "true"}, {"view_mode": "grid"},
                   {"shelf_shortcut": ""}, {"shelf_shortcut": "Ctrl"},
                   {"shelf_shortcut": "Ctrl+K, Ctrl+C"},
                   {"shelf_shortcut": store.overview_shortcut}, {"dock_edge": "center"},
                   {"dock_position": -1}, {"dock_position": 101}, {"dock_position": 0.5},
                   {"dock_position": True}, {"dock_length": 31}, {"dock_length": 601},
                   {"dock_thickness": 1}, {"dock_thickness": 41}, {"dock_length": "100"},
                   {"trigger_visible": "false"}, {"dock_color": "#abc"}, {"dock_color": "red"},
                   {"dock_color": "#12345678"}, {"dock_opacity": -1}, {"dock_opacity": 101},
                   {"dock_opacity": True}, {"dock_opacity": 30.5}):
        before = store.path.read_bytes()
        try:
            store.update(**values)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Invalid preference accepted: {values}")
        assert before == store.path.read_bytes()
    print("PASS invalid settings retain previous state and file")

    restored = []
    installed = Path(temporary) / 'installed' / 'FlickDock.exe'
    installed.parent.mkdir(); installed.write_bytes(b'MZ-fixture')
    previous = (f'"{Path(temporary) / "old" / "FlickDock.exe"}"', 1)
    registration = [previous]
    def restore_write(value):
        restored.append(value); registration[0] = value
    with patch.object(subject.sys, "frozen", True, create=True), \
         patch.object(subject.sys, "executable", str(installed)), \
         patch.object(subject, "_read_startup_value", side_effect=lambda:registration[0]), \
         patch.object(subject, "_write_startup_value", side_effect=restore_write), \
         patch.object(subject, "_startup_runtime_allowed", return_value=True), \
         patch.object(subject.os, "replace", side_effect=OSError("simulated disk failure")):
        try:
            store.update(autostart=True)
        except OSError:
            pass
        else:
            raise AssertionError("Save failure should propagate")
    assert not store.autostart and restored[-1] == previous and len(restored) == 2
    assert not list(base.glob(".settings-*.tmp"))
    print("PASS disk failure rolls startup registration back and cleans temporary file")

    with patch.object(subject.sys, "frozen", True, create=True), \
         patch.object(subject.sys, "executable", str(installed)), \
         patch.object(subject, "_read_startup_value", return_value=None), \
         patch.object(subject, "_startup_runtime_allowed", return_value=True), \
         patch.object(subject, "_write_startup_value", side_effect=PermissionError("simulated registry failure")):
        before = store.path.read_bytes()
        try:
            store.update(autostart=True)
        except OSError as error:
            assert 'simulated registry failure' in str(error)
        else:
            raise AssertionError('Startup failure was reported as success')
        assert before == store.path.read_bytes() and not store.autostart
    print("PASS startup write failure does not commit JSON preferences")

    dialog = subject.SettingsDialog(store)
    previews = []
    dialog.preview_changed.connect(previews.append)
    assert not dialog.isModal()
    dialog.show()
    app.processEvents()
    assert previews and previews[-1] == dialog._dock_values()
    body = dialog.findChild(subject.QWidget, "settingsBody")
    assert body.layout().itemAt(0).widget() is dialog._reference_top
    assert dialog._reference_top.layout().indexOf(dialog.account_slot) >= 0
    assert dialog.dock_card.isAncestorOf(dialog.trigger_visible_checkbox)
    assert dialog.surface.size().width() == min(892,dialog.chrome._available().width()-16)-32 and dialog.surface.size().height() == min(632,dialog.chrome._available().height()-16)-32
    assert set(previews[-1]) == {"dock_edge", "dock_position", "dock_length", "dock_thickness", "trigger_visible", "dock_color", "dock_opacity"}
    dialog.theme_combo.setCurrentIndex(0)
    dialog.folder_edit.setText(str(invalid))
    dialog.save()
    assert dialog.error_label.isVisible() and dialog.isVisible()
    assert store.theme == "dark"
    dialog.load_values()
    assert dialog.view_combo.currentData() == "icons"
    assert dialog.dock_edge_combo.currentData() == "right" and dialog.dock_position_spin.value() == 0
    assert dialog.dock_length_spin.value() == 32 and dialog.dock_thickness_spin.value() == 2
    app.processEvents()  # Finish the source form's theme/NoButtons layout pass.
    for field in (dialog.dock_position_spin, dialog.dock_length_spin, dialog.dock_thickness_spin, dialog.dock_opacity_spin):
        editor = field.findChild(QLineEdit)
        assert editor.isVisible() and editor.width() >= 60 and editor.height() >= 18
    assert not hasattr(dialog, "font_notice") and dialog.author_button.text() == "Author @Roger"
    assert dialog.author_dialog is None and dialog.license_dialog is None
    draft_before = store.path.read_bytes()
    dialog.dock_edge_combo.setCurrentIndex(dialog.dock_edge_combo.findData("bottom"))
    dialog.dock_position_slider.setValue(63)
    dialog.dock_length_spin.setValue(240)
    dialog.dock_thickness_spin.setValue(12)
    assert dialog.dock_position_spin.value() == 63 and "靠左" in dialog.dock_position_hint.text()
    dialog.dock_position_spin.setValue(58)
    assert dialog.dock_position_slider.value() == 58
    dialog.trigger_visible_checkbox.setChecked(False)
    dialog.dock_opacity_slider.setValue(37)
    dialog._set_dock_color("#aa44cc")
    assert previews[-1] == {"dock_edge": "bottom", "dock_position": 58, "dock_length": 240,
                            "dock_thickness": 12, "trigger_visible": False, "dock_color": "#aa44cc", "dock_opacity": 37}
    assert store.path.read_bytes() == draft_before and store.trigger_visible and store.dock_opacity == 100
    def cancel_color(picker):
        picker.setCurrentColor(QColor("#123456"))
        assert previews[-1]["dock_color"] == "#123456"
        return QDialog.DialogCode.Rejected
    with patch.object(QColorDialog, "exec", cancel_color):
        dialog.choose_dock_color()
    assert dialog._draft_color == "#aa44cc" and previews[-1]["dock_color"] == "#aa44cc"
    assert store.path.read_bytes() == draft_before
    assert subject.normalize_shortcut(dialog.shelf_shortcut_edit.keySequence()) == store.shelf_shortcut
    dialog.grab().save(str(Path(__file__).parent / "v16-settings-dark.png"))
    dialog.theme_combo.setCurrentIndex(0)
    app.processEvents()
    dialog.grab().save(str(Path(__file__).parent / "v16-settings-light.png"))
    before = store.path.read_bytes()
    received = []
    dialog.shortcut_validator = lambda values: received.append(values) or "这个快捷键已被其他程序使用。"
    dialog.shelf_shortcut_edit.setKeySequence(subject.QKeySequence('Ctrl+Alt+P'))
    dialog.save()
    assert dialog.error_label.isVisible() and "快捷键" in dialog.error_label.text()
    assert store.path.read_bytes() == before and dialog.isVisible()
    assert received[0]["shelf_shortcut"] == "Ctrl+Alt+P"
    dialog.shortcut_validator = None
    with patch.object(subject, "_read_startup_value", return_value=None) as read, \
         patch.object(subject, "_write_startup_value") as write:
        dialog.save()
        read.assert_not_called()
        write.assert_not_called()
    assert store.theme == "light" and not dialog.isVisible()
    assert previews[-1] is None
    assert not store.trigger_visible and store.dock_color == "#aa44cc" and store.dock_opacity == 37
    assert (store.dock_edge, store.dock_position, store.dock_length, store.dock_thickness) == ("bottom", 58, 240, 12)
    dialog.show()
    dialog.author_button.click()
    app.processEvents()
    author_dialog = dialog.author_dialog
    assert author_dialog.isVisible() and not author_dialog.isModal()
    assert author_dialog.author_label.text() == "Author @Roger"
    assert not hasattr(author_dialog, 'font_notice') and author_dialog.license_dialog is None
    author_dialog.licenses_button.click()
    license_dialog = author_dialog.license_dialog
    app.processEvents()
    assert license_dialog.isVisible() and not license_dialog.isModal()
    assert not hasattr(license_dialog, 'font_notice')
    for index in range(license_dialog.document_combo.count()):
        license_dialog.document_combo.setCurrentIndex(index)
        assert len(license_dialog.license_text.toPlainText()) > 150
        assert "缺少" not in license_dialog.license_text.toPlainText()
    license_dialog.document_combo.setCurrentIndex(1)
    assert "HarmonyOS Sans Fonts License Agreement" in license_dialog.license_text.toPlainText()
    dialog.apply_theme("dark")
    assert license_dialog.document_combo.dark
    author_dialog.grab().save(str(Path(__file__).parent / "v16-author-about.png"))
    license_dialog.grab().save(str(Path(__file__).parent / "v16-third-party-licenses.png"))
    license_dialog.close()
    author_dialog.close()
    dialog.close()
    for action in (lambda: dialog.reject(), lambda: dialog.close(), lambda: QTest.keyClick(dialog, Qt.Key.Key_Escape)):
        previews.clear()
        dialog.show(); app.processEvents()
        assert previews[-1] is not None
        saved_before = store.path.read_bytes()
        dialog.dock_position_spin.setValue(84)
        dialog.dock_length_spin.setValue(490)
        dialog.dock_opacity_spin.setValue(0)
        assert previews[-1]["dock_length"] == 490 and store.path.read_bytes() == saved_before
        action(); app.processEvents()
        assert previews[-1] is None and store.path.read_bytes() == saved_before
        assert store.dock_length == 240 and store.dock_opacity == 37
    print("PASS nonmodal dialog validates inline, previews themes and saves without unnecessary registry access")
    print("PASS shortcut availability validator blocks save before persistence")
    print("PASS Author @Roger opens author information first; separate attribution and offline license reader remain accessible")
    print("PASS full dock drafts preview immediately without persistence; color cancel and Save/Cancel/X/Escape end preview correctly")

with tempfile.TemporaryDirectory(prefix="flickdock-settings-legacy-") as temporary:
    base = Path(temporary)
    saved_directory = base / "existing-content"
    (base / "settings.json").write_text(json.dumps({"capture_dir": str(saved_directory), "theme": "dark",
        "dock_edge": "invalid", "dock_position": -4, "dock_length": "90", "dock_thickness": True}), encoding="utf8")
    legacy = subject.SettingsStore(base)
    assert legacy.capture_dir == saved_directory and legacy.theme == "dark"
    assert (legacy.dock_edge, legacy.dock_position, legacy.dock_length, legacy.dock_thickness) == ("right", 8, 76, 8)
    print("PASS older settings retain capture path; malformed dock values fall back independently")

with tempfile.TemporaryDirectory(prefix="flickdock-startup-") as temporary:
    base = Path(temporary) / "profile"; base.mkdir()
    (base / "settings.json").write_text(json.dumps({"autostart": True,"startup_initialized":True}), encoding="utf8")
    installed = Path(temporary) / "new-location" / "FlickDock.exe"
    installed.parent.mkdir(); installed.write_bytes(b'MZ-fixture')
    old = Path(temporary) / "old-location" / "FlickDock.exe"
    registration = [(f'"{old}"',1)]
    def write_registration(value): registration[0]=value
    with patch.object(subject.sys, "frozen", True, create=True), \
         patch.object(subject, "_startup_runtime_allowed", return_value=True), \
         patch.object(subject.sys, "executable", str(installed)), \
         patch.object(subject, "_read_startup_value", side_effect=lambda:registration[0]) as read, \
         patch.object(subject, "_write_startup_value", side_effect=write_registration) as write:
        startup_store = subject.SettingsStore(base)
        read.assert_not_called()
        assert startup_store.reconcile_startup()
        write.assert_called_once_with((f'"{installed}"',1))
        write.reset_mock()
        registration[0]=None
        assert startup_store.reconcile_startup()
        write.assert_called_once_with((f'"{installed}"',1))
        write.reset_mock()
        registration[0]=None
        assert startup_store.update(autostart=True)
        write.assert_called_once_with((f'"{installed}"',1))
        write.reset_mock()
        assert not startup_store.reconcile_startup()
        write.assert_not_called()
        for command in (('"python.exe" "flickdock.py"',1),(f'"{old}" --background',1),
                        ('FlickDock.exe',1),(str(old.with_name("other.exe")),1),(str(old),2)):
            registration[0]=command; write.reset_mock()
            try: startup_store.reconcile_startup()
            except OSError: pass
            else: raise AssertionError("Non-owned startup value was reported as enabled")
            write.assert_not_called(); assert registration[0]==command
    with patch.object(subject.sys, "frozen", False, create=True), \
         patch.object(subject, "_read_startup_value") as read, \
         patch.object(subject, "_write_startup_value") as write:
        assert not startup_store.reconcile_startup()
        read.assert_not_called(); write.assert_not_called()
    print("PASS startup repairs missing/moved own commands and unchanged saves; non-owned commands are refused")

with patch.object(subject.sys, "frozen", True, create=True):
    assert subject.startup_command() == '"' + str(Path(subject.sys.executable).resolve()) + '"'

with tempfile.TemporaryDirectory(prefix="flickdock-default-startup-") as temporary:
    base = Path(temporary) / "fresh-profile"
    installed = Path(temporary) / "installed" / "FlickDock.exe"
    installed.parent.mkdir(); installed.write_bytes(b'MZ-fixture')
    registration = [None]
    def write_default(value): registration[0]=value
    with patch.object(subject.sys, "frozen", True, create=True), \
         patch.object(subject.sys, "executable", str(installed)), \
         patch.object(app, "platformName", return_value="windows"), \
         patch.object(subject, "_read_startup_value", side_effect=lambda:registration[0]) as read, \
         patch.object(subject, "_write_startup_value", side_effect=write_default) as write:
        default = subject.SettingsStore(base)
        assert default.autostart and not default.path.exists()
        read.assert_not_called(); write.assert_not_called()
        assert default.reconcile_startup()
        write.assert_called_once_with((f'"{installed}"',1))
        assert json.loads(default.path.read_text(encoding="utf8"))["startup_initialized"] is True
        write.reset_mock()
        restored = subject.SettingsStore(base)
        assert not restored.reconcile_startup(); write.assert_not_called()
        registration[0]=None
        assert restored.reconcile_startup()
        write.assert_called_once_with((f'"{installed}"',1))
        write.reset_mock()
        assert restored.update(autostart=False)
        write.assert_called_once_with(None)
        write.reset_mock(); read.reset_mock()
        disabled = subject.SettingsStore(base)
        assert not disabled.autostart and not disabled.reconcile_startup()
        read.assert_not_called(); write.assert_not_called()
    print("PASS installed default registers and repairs; saved false remains false")

    for frozen, platform in ((False,"windows"),(True,"offscreen"),(True,"minimal")):
        with patch.object(subject.sys,"frozen",frozen,create=True), \
             patch.object(subject.sys,"executable",str(installed)), \
             patch.object(app,"platformName",return_value=platform), \
             patch.object(subject,"_read_startup_value") as read, \
             patch.object(subject,"_write_startup_value") as write:
            isolated=subject.SettingsStore(Path(temporary)/f"isolated-{frozen}-{platform}")
            assert isolated.autostart and not isolated.reconcile_startup()
            isolated.update(autostart=False); isolated.update(autostart=True)
            read.assert_not_called(); write.assert_not_called()
    print("PASS source/offscreen/minimal never access startup registry, including explicit preference updates")

    rollback_writes=[]; registration[0]=None
    def rollback_default(value): rollback_writes.append(value); registration[0]=value
    failed=subject.SettingsStore(Path(temporary)/"failed-first-registration")
    with patch.object(subject.sys,"frozen",True,create=True), \
         patch.object(subject.sys,"executable",str(installed)), \
         patch.object(app,"platformName",return_value="windows"), \
         patch.object(subject,"_read_startup_value",side_effect=lambda:registration[0]), \
         patch.object(subject,"_write_startup_value",side_effect=rollback_default), \
         patch.object(subject.os,"replace",side_effect=OSError("simulated first-start disk failure")):
        try: failed.reconcile_startup()
        except OSError as error: assert "simulated first-start disk failure" in str(error)
        else: raise AssertionError("First startup persistence failure was hidden")
    assert rollback_writes==[(f'"{installed}"',1),None] and registration[0] is None
    assert not failed.path.exists() and not failed._data["startup_initialized"]
    print("PASS failed first startup rolls registration back before marking the default initialized")
print("APP_SETTINGS_OK")
