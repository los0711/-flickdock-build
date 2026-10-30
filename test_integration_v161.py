"""v1.6.1 UI-entry integration with offscreen Qt and isolated local files.

Real child confirmation/color event loops and real asynchronous ZIP jobs run.
Only native input, registry, tray and the external drag transport are mocked.
"""
import json
import os
from pathlib import Path
import tempfile
import time
import traceback
from unittest.mock import patch
from types import SimpleNamespace
import zipfile

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QObject, QPoint, QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QEnterEvent, QImage, QKeyEvent, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QColorDialog, QPushButton, QSystemTrayIcon


def flush(app):
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def until(app, condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        flush(app)
        if condition():
            return
        QTest.qWait(15)
    assert condition(), "Offscreen UI operation did not complete"


class LocalResolver(QObject):
    def __init__(self, *args, parent=None, **kwargs):
        super().__init__(parent)
        self.jobs = []

    @property
    def pending_refs(self): return frozenset(ref for ref, _ in self.jobs)

    def candidates(self, mime):
        from content_resolver import candidates
        return candidates(mime)

    def resolve(self, ref, url): self.jobs.append((ref, url))
    def cancel(self, ref): self.jobs = [(key, url) for key, url in self.jobs if key != ref]
    def cancel_all(self): self.jobs.clear()
    def close(self): self.cancel_all()


failures = []


def check(name, operation):
    try:
        operation()
        print("PASS " + name)
    except Exception as error:
        traceback.print_exc()
        failures.append((name, str(error)))
        print("FAIL " + name)


with tempfile.TemporaryDirectory(prefix="flickdock-v161-integration-") as temporary:
    os.environ["APPDATA"] = str(Path(temporary) / "profile")
    import flickdock as subject
    import app_settings
    from confirm_clear import ClearConfirmOverlay, confirmation_active
    from content_grid import IconItem
    from content_stack import StackTile
    from dock_geometry import trigger_rect
    from global_hotkeys import _NoopBackend

    app = QApplication([])
    subject.load_font(app)
    wide_screen=SimpleNamespace(availableGeometry=lambda:QRect(0,0,1920,1080))
    with patch.object(subject.QGuiApplication,'screenAt',return_value=wide_screen), \
         patch.object(subject.QGuiApplication,'primaryScreen',return_value=wide_screen), \
         patch.object(subject, "motion_enabled", return_value=False), \
         patch.object(subject, "ContentResolver", LocalResolver), \
         patch.object(QSystemTrayIcon, "show"), patch.object(QSystemTrayIcon, "hide"), \
         patch.object(subject.ctypes.windll.user32, "GetAsyncKeyState", return_value=0), \
         patch.object(subject, "native_window_move_active", return_value=False), \
         patch.object(app_settings, "_read_startup_value") as read_registry, \
         patch.object(app_settings, "_write_startup_value") as write_registry:
        manager = subject.ShelfManager(app)
        manager.timer.stop()
        shelf = manager.shelf
        assert isinstance(manager.hotkeys.backend, _NoopBackend)
        originals = []
        for index in range(1, 8):
            path = Path(temporary) / f"file-{index}.txt"
            path.write_text(f"Original payload {index}", encoding="utf8")
            originals.append(str(path))
        shelf.add_paths("permanent", originals)

        def show(compact=False):
            with patch.object(subject.QCursor, "pos", return_value=QPoint(420, 350)):
                (manager.show_shortcut if compact else manager.show_overview)()
            flush(app)
            return shelf.popup

        def settings_preview():
            show(compact=True)
            manager.show_settings()
            dialog = manager.settings_dialog
            flush(app)
            assert manager.dock_preview.isVisible() and not shelf.isVisible() and not manager.edge.isVisible()
            assert manager.dock_preview.target_rect == manager.edge.geometry()
            before = manager.settings.path.read_bytes() if manager.settings.path.exists() else None
            original_values = dict(manager.dock_values())
            for widget, value in ((dialog.dock_position_slider, 69), (dialog.dock_length_spin, 220),
                                  (dialog.dock_thickness_spin, 16), (dialog.dock_opacity_slider, 43)):
                widget.setValue(value)
                assert manager.dock_preview.target_rect == manager.edge.geometry()
            dialog.dock_edge_combo.setCurrentIndex(dialog.dock_edge_combo.findData("left"))
            dialog.trigger_visible_checkbox.setChecked(False)
            picker_errors = []
            def pick_color():
                picker = next((item for item in dialog.findChildren(QColorDialog) if item.isVisible()), None)
                try:
                    assert picker is not None
                    picker.setCurrentColor(QColor("#2a9b6f"))
                    assert manager.dock_preview.values["dock_color"] == "#2a9b6f"
                except Exception as error:
                    picker_errors.append(error)
                finally:
                    if picker is not None: picker.accept()
            QTimer.singleShot(0, pick_color)
            dialog.dock_color_button.click()
            assert not picker_errors, str(picker_errors)
            draft = manager.dock_values()
            assert draft == {"dock_edge": "left", "dock_position": 69, "dock_length": 220,
                             "dock_thickness": 16, "dock_opacity": 43, "trigger_visible": False, "dock_color": "#2a9b6f"}
            assert manager.edge.geometry() == trigger_rect(manager.edge.screen_area(), "left", 69, 220, 16)
            assert manager.dock_preview.values == draft
            assert not manager.settings.path.exists() if before is None else manager.settings.path.read_bytes() == before
            dialog.cancel_button.click()
            assert manager._dock_preview_values is None and not manager.dock_preview.isVisible()
            assert manager.dock_values() == original_values
            manager.show_settings(); dialog.dock_length_spin.setValue(350); dialog.close()
            assert manager._dock_preview_values is None and manager.dock_values() == original_values
            manager.show_settings()
            dialog.dock_edge_combo.setCurrentIndex(dialog.dock_edge_combo.findData("bottom"))
            dialog.dock_position_spin.setValue(35); dialog.dock_length_spin.setValue(160); dialog.dock_thickness_spin.setValue(12)
            dialog.trigger_visible_checkbox.setChecked(False); dialog.dock_opacity_spin.setValue(56)
            dialog._set_dock_color("#c24f71")
            dialog.save_button.click()
            assert not dialog.isVisible() and manager._dock_preview_values is None and not manager.dock_preview.isVisible()
            assert manager.settings.dock_edge == "bottom" and manager.settings.dock_color == "#c24f71"
            assert manager.settings.dock_opacity == 56 and not manager.settings.trigger_visible
            assert json.loads(manager.settings.path.read_text(encoding="utf8"))["dock_length"] == 160
        check("settings entry previews real edge/desktop guide live; color picker, Cancel/X and Save restore or persist correctly", settings_preview)

        def hidden_trigger():
            if manager.settings_dialog and manager.settings_dialog.isVisible(): manager.settings_dialog.close()
            shelf.hide_immediately(); manager.edge_suppressed = False; manager.update_handles()
            assert manager.edge.isVisible() and not manager.settings.trigger_visible
            marker = manager.edge.grab().toImage()
            assert marker.pixelColor(marker.width() // 2, marker.height() // 2).alpha() <= 2
            with patch.object(subject.QCursor, "pos", return_value=manager.edge.geometry().center()): manager.poll()
            assert shelf.isVisible() and shelf.presentation_kind == "dock"
        check("hidden marker keeps a transparent hittable region and opens the shelf on hover", hidden_trigger)

        def compact_and_order():
            shelf.hide_immediately(); manager.auto_shown = None
            manager._content_drag_validated(QPoint(160, 300))
            assert shelf.presentation_kind == "drag"
            assert all(button.isHidden() for button in (shelf.pin_button, shelf.expand_button, shelf.menu_button, shelf.close_button))
            zone = shelf.zones["permanent"]
            assert isinstance(zone.icon, StackTile) and zone.icon.refs == tuple(originals[-5:])
            assert zone.icon.refs[-1] == originals[-1] and zone.caption.text() == "7 项"
            assert zone.description.isHidden() and shelf.status_label.text() == ""
            manager.auto_shown = None
            popup = show(); popup.set_category("images"); shelf.collapse_contents()
            payload = QMimeData(); payload.setText("Newest native content")
            enter = QDragEnterEvent(QPoint(160, 130), Qt.DropAction.CopyAction, payload,
                                   Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(shelf, enter)
            drop = QDropEvent(QPointF(160, 130), Qt.DropAction.CopyAction, payload,
                              Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(shelf, drop)
            assert enter.isAccepted() and drop.isAccepted()
            newest = shelf.files["permanent"][-1]
            assert shelf.content.text(newest) == payload.text() and zone.icon.refs[-1] == newest
            assert zone.caption.text() == "8 项"
            until(app, lambda: not shelf.isVisible())
            popup = show()
            assert popup.category == "all" and popup.visible_refs()[0] == newest
            assert all(button.isVisible() for button in (shelf.pin_button, shelf.expand_button, shelf.close_button))
            # Expanded reference toolbar keeps the essential controls; page
            # actions remain available through the real context/sidebar menus.
            assert shelf.popup.lan_nav.isVisible() and shelf.popup.settings_nav.isVisible()
        check("compact uses latest five with newest front/count only; drag header is clear and expanding resets all/newest-first", compact_and_order)

        def rows_for(popup):
            until(app,lambda: popup._build_cursor==len(popup.visible_refs()))
            flush(app)
            return {row.path: row for row in popup.rows_body.findChildren(subject.FileRow) if row.isVisible()}

        def multi_drag_zip():
            manager.settings.update(view_mode="list")
            popup = show(); refs = popup.visible_refs(); rows = rows_for(popup)
            QTest.mouseClick(rows[refs[0]], Qt.MouseButton.LeftButton)
            QTest.mouseClick(rows[refs[2]], Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier)
            assert popup.selected == {refs[0], refs[2]}
            assert tuple(popup.preview._group_refs) == tuple(popup.chosen_refs())
            QTest.mouseClick(rows[refs[1]], Qt.MouseButton.LeftButton)
            QTest.mouseClick(rows[refs[4]], Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
            assert popup.selected == set(refs[1:5])
            QTest.keyClick(shelf, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
            assert popup.selected == set(refs) and popup.zip_button.isEnabled()
            assert tuple(popup.preview._group_refs) == tuple(popup.chosen_refs())
            row = rows[refs[3]]
            QApplication.sendEvent(row, QEnterEvent(QPointF(2, 2), QPointF(2, 2), QPointF(2, 2)))
            assert popup.preview.current_path == row.path and not popup.preview._group_refs
            QApplication.sendEvent(row, QEvent(QEvent.Type.Leave)); flush(app)
            assert tuple(popup.preview._group_refs) == tuple(popup.chosen_refs())
            exported = []
            def drag_transport(drag, *_):
                mime = drag.mimeData()
                exported.append((set(str(Path(url.toLocalFile()).resolve()) for url in mime.urls()), mime.text()))
                return Qt.DropAction.CopyAction
            chosen_before = list(shelf.files["permanent"])
            QTest.mousePress(row, Qt.MouseButton.LeftButton, pos=QPoint(12, 20))
            move = QMouseEvent(QEvent.Type.MouseMove, QPointF(38, 20), QPointF(row.mapToGlobal(QPoint(38, 20))),
                               Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            with patch.object(subject.QDrag, "exec", drag_transport): QApplication.sendEvent(row, move)
            QTest.mouseRelease(row, Qt.MouseButton.LeftButton, pos=QPoint(38, 20))
            assert exported and exported[-1][0] == set(originals) and exported[-1][1] == "Newest native content", repr(exported)
            assert shelf.files["permanent"] == chosen_before and not shelf.dragging and not manager.outgoing_drag
            flush(app); rows = rows_for(popup)
            keep = {originals[1], originals[5]}
            for ref in list(popup.selected - keep):
                QTest.mouseClick(rows[ref], Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier)
            assert popup.selected == keep
            assert tuple(popup.preview._group_refs) == tuple(popup.chosen_refs())
            destination = Path(temporary) / "selected-export.zip"
            with patch.object(shelf, "choose_zip_path", return_value=str(destination)):
                popup.zip_button.click()
            assert shelf.archive_job is not None
            until(app, lambda: shelf.archive_job is None and destination.is_file())
            with zipfile.ZipFile(destination) as archive:
                assert set(archive.namelist()) == {"file-2.txt", "file-6.txt"}
                assert archive.read("file-2.txt").decode() == "Original payload 2"
            assert all(Path(path).is_file() for path in originals)
            assert all(path in shelf.files["permanent"] for path in originals)
            assert popup.visible_refs()[0] == str(destination)
            manager.settings.update(view_mode="icons")
            until(app,lambda: popup._build_cursor==len(popup.visible_refs()))
            flush(app)
            icon_refs = popup.visible_refs()
            icons = {item.path: item for item in popup.rows_body.findChildren(IconItem) if item.isVisible()}
            popup.selected.clear();popup.primary_selection=None
            QTest.mouseClick(icons[icon_refs[0]], Qt.MouseButton.LeftButton)
            QTest.mouseClick(icons[icon_refs[2]], Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier)
            assert popup.selected == {icon_refs[0], icon_refs[2]}
            QTest.mouseClick(icons[icon_refs[3]], Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
            assert popup.selected == {icon_refs[0], icon_refs[2], icon_refs[3]}
            QTest.keyClick(icons[icon_refs[3]], Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
            assert popup.selected == set(icon_refs)
            assert tuple(popup.preview._group_refs) == tuple(popup.chosen_refs())
        check("Ctrl/Shift/Ctrl+A, grouped hover ownership, real selected MIME and asynchronous selected-only ZIP retain originals", multi_drag_zip)

        def clear_entry():
            popup = show()
            before = list(shelf.files["permanent"])
            def invoke(delete):
                callback_errors = []
                timer = QTimer(); timer.setSingleShot(True)
                def interact():
                    overlay = next((item for item in shelf.findChildren(ClearConfirmOverlay) if item.active), None)
                    try:
                        assert overlay is not None and not overlay.isWindow() and overlay.parentWidget() is shelf
                        assert confirmation_active(shelf) and overlay.cancel_button.hasFocus()
                        snapshot = (shelf.visual_geometry(), shelf.expanded, shelf.presentation_kind)
                        for identifier in tuple(manager.hotkeys._registrations): manager.hotkeys._dispatch(identifier)
                        assert snapshot == (shelf.visual_geometry(), shelf.expanded, shelf.presentation_kind)
                        assert overlay.active and not overlay.description.wordWrap()
                        (overlay.delete_button if delete else overlay.cancel_button).click()
                    except Exception as error:
                        callback_errors.append(error)
                        if overlay is not None: overlay.cancel()
                def timeout():
                    callback_errors.append(AssertionError("Confirmation child loop did not finish"))
                    for item in shelf.findChildren(ClearConfirmOverlay): item.cancel()
                timer.timeout.connect(timeout); timer.start(2000)
                QTimer.singleShot(0, interact)
                popup.clear_button.click()
                timer.stop()
                assert not callback_errors, str(callback_errors)
                assert not confirmation_active(shelf)
            invoke(False)
            assert shelf.files["permanent"] == before
            flush(app)
            invoke(True)
            assert not shelf.files["permanent"] and all(Path(path).is_file() for path in originals)
        check("clear button enters real child overlay, blocks both hotkeys and requires actual Cancel/Delete choices", clear_entry)

        read_registry.assert_not_called(); write_registry.assert_not_called()
        manager.quit(); flush(app)

if failures:
    for name, error in failures: print("INTEGRATION_FAILURE", name, error)
    raise AssertionError(f"{len(failures)} v1.6.1 integration checks failed")
print("INTEGRATION_V161_OK")
