"""v1.6 integration audit: offscreen, isolated profile, no network/native input.

The player contract is mocked here; test_media_native.py separately exercises
the actual Windows decoder without sound or a desktop window.
"""
import json
import os
from pathlib import Path
import tempfile
import time
import traceback
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QObject, QPoint, QPointF, QRect, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QSystemTrayIcon


def flush(app):
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def wait_until(app, predicate):
    for _ in range(55):
        flush(app)
        if predicate():
            return
        QTest.qWait(20)
    assert predicate(), "Offscreen state did not settle"


class FakeResolver(QObject):
    def __init__(self, repository, directory, retained, on_ready=None, parent=None, **kwargs):
        super().__init__(parent)
        self.jobs = []
        self.cancelled = []

    @property
    def pending_refs(self):
        return frozenset(ref for ref, _ in self.jobs)

    def candidates(self, mime):
        from content_resolver import candidates
        return candidates(mime)

    def resolve(self, ref, url):
        self.jobs.append((ref, url))

    def cancel(self, ref):
        self.cancelled.append(ref)
        self.jobs = [(key, url) for key, url in self.jobs if key != ref]

    def cancel_all(self):
        self.jobs.clear()

    def close(self):
        self.cancel_all()


def fake_media_source(self, path):
    self.path = str(path)
    self._integration_source = str(path)


def fake_media_stop(self):
    self._integration_playing = False


failures = []


def check(name, callback):
    try:
        callback()
        print("PASS " + name)
    except Exception as error:
        failures.append((name, str(error)))
        traceback.print_exc()
        print("FAIL " + name)


with tempfile.TemporaryDirectory(prefix="flickdock-v16-") as temporary:
    os.environ["APPDATA"] = str(Path(temporary) / "appdata")
    import flickdock as subject
    import app_settings
    from content_grid import IconItem
    from global_hotkeys import _NoopBackend
    from media_preview import AUDIO_SUFFIXES, VIDEO_SUFFIXES

    subject.BASE.mkdir(parents=True)
    original = Path(temporary) / "original.txt"
    original.write_text("Original file remains on disk", encoding="utf8")
    old_id = "flickdock:text:legacy"
    legacy = {"version": 4, "shelves": [{"temporary": [str(original), old_id],
        "permanent": [str(original)], "texts": {old_id: "Retained native text"}}]}
    subject.STATE_FILE.write_text(json.dumps(legacy), encoding="utf8")
    app = QApplication([])
    subject.load_font(app)
    with patch.object(subject, "motion_enabled", return_value=False), \
         patch.object(subject, "ContentResolver", FakeResolver), \
         patch.object(QSystemTrayIcon, "show"), patch.object(QSystemTrayIcon, "hide"), \
         patch.object(subject.MediaPreview, "set_source", autospec=True, side_effect=fake_media_source) as media_source, \
         patch.object(subject.MediaPreview, "stop", autospec=True, side_effect=fake_media_stop) as media_stop, \
         patch.object(subject, "native_window_move_active", return_value=False), \
         patch.object(app_settings, "_read_startup_value") as registry_read, \
         patch.object(app_settings, "_write_startup_value") as registry_write:
        manager = subject.ShelfManager(app)
        manager.timer.stop()
        shelf = manager.shelf
        manager.save()

        def migration():
            assert isinstance(manager.hotkeys.backend, _NoopBackend) and not manager.hotkeys._filter_installed
            assert manager.shelves == [shelf] and set(shelf.files) == {"permanent"}
            assert len(shelf.files["permanent"]) == 2
            assert any(shelf.content.text(ref) == "Retained native text" for ref in shelf.files["permanent"])
            saved = json.loads(subject.STATE_FILE.read_text(encoding="utf8"))
            assert saved["version"] == 6 and "temporary" not in saved["shelf"]
            assert json.loads((subject.BASE / "shelves-before-v6.json").read_text(encoding="utf8")) == legacy
        check("v4 mixed zones migrate into one permanent v6 shelf with backup and native text", migration)

        image = Path(temporary) / "raster.png"
        picture = QImage(180, 100, QImage.Format.Format_ARGB32)
        picture.fill(0xff2584ed)
        assert picture.save(str(image))
        vector = Path(temporary) / "vector.svg"
        vector.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="90" height="60"><rect width="90" height="60" fill="#2584ed"/></svg>', encoding="utf8")
        video = Path(temporary) / "movie.mp4"
        audio = Path(temporary) / "sound.wav"
        document = Path(temporary) / ("very-long-document-title-" * 6 + ".pdf")
        for path in (video, audio, document):
            path.write_bytes(b"Metadata-only test fixture")
        native = shelf.content.add_text("Native shelf text\n" + "long complete text " * 400)
        link = shelf.content.add_text("https://example.invalid/article")
        shelf.content.metadata[link] = {"title": "Example webpage", "source_url": "https://example.invalid/article"}
        shelf.add_paths("permanent", [str(image), str(vector), str(video), str(audio), str(document), native, link])

        def open_overview():
            with patch.object(subject.QCursor, "pos", return_value=QPoint(400, 350)):
                manager.show_overview()
            flush(app)
            return shelf.popup

        def category_and_selection():
            popup = open_overview()
            expected = {"images": {str(image), str(vector)}, "video": {str(video)}, "audio": {str(audio)},
                        "links": {link}, "files": {str(document)}}
            for category, refs in expected.items():
                popup.category_buttons[category].click()
                assert set(popup.visible_refs()) == refs, category
            popup.set_category("text")
            assert native in popup.visible_refs() and str(original) in popup.visible_refs()
            popup.set_category("all")
            popup.select_item(native)
            popup.hover_item(str(document))
            assert popup.preview.current_path == str(document)
            popup.end_hover(str(document)); flush(app)
            assert popup.preview.current_path == native and popup.primary_selection == native
            assert popup.preview.text.toPlainText() == shelf.content.text(native)
            popup.hover_item(str(image)); popup.hover_item(str(vector)); popup.end_hover(str(image)); flush(app)
            assert popup.preview.current_path == str(vector)
            popup.end_hover(str(vector)); flush(app)
            assert popup.preview.current_path == native
            manager.settings.update(view_mode="icons")
            wait_until(app,lambda: popup._build_cursor==len(popup.visible_refs()) and popup.retirement.is_idle)
            flush(app)
            assert len(popup.rows_body.findChildren(IconItem)) == len(shelf.files["permanent"])
            assert popup.primary_selection == native and popup.preview.current_path == native
            assert all(item.toolTip() == "" for item in popup.rows_body.findChildren(IconItem))
        check("categories, hover preview restoration and list/icons preserve primary selection", category_and_selection)

        def media_categories():
            mismatches = [(suffix, shelf.content.category("test" + suffix), expected)
                for expected, suffixes in (("video", VIDEO_SUFFIXES), ("audio", AUDIO_SUFFIXES))
                for suffix in suffixes if suffix != ".ts" and shelf.content.category("test" + suffix) != expected]
            assert not mismatches, str(mismatches)
            typescript = Path(temporary) / "source-code.ts"
            typescript.write_text("const greeting: string = 'TypeScript remains readable';", encoding="utf8")
            transport = Path(temporary) / "transport-video.ts"
            transport.write_bytes((b"\x47" + bytes(187)) * 2)
            assert shelf.content.category(str(typescript)) == "files"
            assert shelf.content.category(str(transport)) == "video"
            assert not subject.MediaPreview.can_preview(typescript), "TypeScript source must not enter the video player"
            assert subject.MediaPreview.can_preview(transport)
            popup = open_overview()
            popup.preview.show_path(str(typescript))
            assert popup.preview.stack.currentWidget() is popup.preview.text
            assert "TypeScript remains readable" in popup.preview.text.toPlainText()
        check("all preview-supported media are classified consistently", media_categories)

        def elision():
            popup = open_overview()
            popup.set_category("all")
            popup.select_item(str(document))
            flush(app)
            label = popup.preview.name
            assert label.full_text == document.name and not label.wordWrap() and label.toolTip() == ""
            assert label.fontMetrics().horizontalAdvance(label.text()) <= label.width() + 1
            assert label.text().endswith("…")
            assert popup.preview.info.toolTip() == ""
        check("long preview title uses one-line elision with no native filename tooltip", elision)

        def poll_at(point, buttons=0):
            def state(key):
                bit = {0x01: 1, 0x02: 2, 0x04: 4}.get(key, 0)
                return 0x8000 if buttons & bit else 0
            with patch.object(app, "platformName", return_value="windows"), \
                 patch.object(subject.ctypes.windll.user32, "GetAsyncKeyState", side_effect=state), \
                 patch.object(subject.QCursor, "pos", return_value=point):
                manager.poll()

        def pin_and_outside():
            shelf.hide_immediately()
            with patch.object(subject.QCursor, "pos", return_value=QPoint(250, 350)):
                manager.show_shortcut()
            if not shelf.pinned:
                shelf.pin_button.click()
            assert shelf.pinned and json.loads(subject.STATE_FILE.read_text(encoding="utf8"))["shelf"]["pinned"]
            shelf.last_activity = time.monotonic() - 20
            outside = QPoint(0, 740)
            for buttons in (0, 1, 0, 2, 0, 4, 0):
                poll_at(outside, buttons)
                assert shelf.isVisible() and shelf.visible_state != "hiding"
            mime = QMimeData(); mime.setText("Pinned drop remains visible")
            assert shelf.receive_mime("permanent", mime)
            shelf._finish_intake()
            assert shelf.isVisible() and shelf.visible_state != "hiding" and not shelf.incoming_drop
            shelf.pin_button.click()
            assert not shelf.pinned
            poll_at(outside, 1)
            assert shelf.visible_state == "hiding"
            wait_until(app, lambda: not shelf.isVisible())
            poll_at(outside, 0)
            with patch.object(subject.QCursor, "pos", return_value=QPoint(250, 350)):
                manager.show_shortcut()
            shelf.last_activity = time.monotonic() - 20
            poll_at(outside)
            assert shelf.visible_state == "hiding"
            wait_until(app, lambda: not shelf.isVisible())
        check("pin prevents outside-click, idle and completed-drop hiding; unpinned interactions dismiss", pin_and_outside)

        def popup_press_protection():
            class Popup:
                def frameGeometry(self):
                    return QRect(20, 650, 160, 60)

            shelf.hide_immediately()
            with patch.object(subject.QCursor, "pos", return_value=QPoint(250, 350)):
                manager.show_shortcut()
            if shelf.pinned:
                shelf.pin_button.click()
            popup = Popup()
            in_popup = popup.frameGeometry().center()
            outside = QPoint(0, 740)
            assert not shelf.hover_region_contains(in_popup)
            assert not popup.frameGeometry().contains(outside)
            shelf._menu_open = True
            try:
                with patch.object(QApplication, "activePopupWidget", return_value=popup):
                    poll_at(in_popup, 0)
                    poll_at(in_popup, 1)
                    assert shelf.isVisible() and shelf.visible_state != "hiding", "menu item press dismissed shelf"
                    poll_at(in_popup, 0)
                    shelf.pin_button.click()
                    poll_at(outside, 1)
                    assert shelf.pinned and shelf.isVisible() and shelf.visible_state != "hiding"
                    poll_at(outside, 0)
                    shelf.pin_button.click()
                    poll_at(outside, 1)
                    assert shelf.visible_state == "hiding", "menu outside press was swallowed by popup blocking"
            finally:
                shelf._menu_open = False
                if shelf.pinned:
                    shelf.pin_button.click()
            wait_until(app, lambda: not shelf.isVisible())
            poll_at(outside, 0)

            # The menu's exec() flag can still be set while the popup has
            # already closed. That state must not consume a new outside press.
            with patch.object(subject.QCursor, "pos", return_value=QPoint(250, 350)):
                manager.show_shortcut()
            shelf._menu_open = True
            try:
                with patch.object(QApplication, "activePopupWidget", return_value=None):
                    poll_at(outside, 0)
                    poll_at(outside, 1)
                    assert shelf.visible_state == "hiding"
            finally:
                shelf._menu_open = False
            wait_until(app, lambda: not shelf.isVisible())
            poll_at(outside, 0)
        check("popup item clicks stay open, popup outside clicks dismiss, and pin still protects", popup_press_protection)

        def region_and_capture():
            area = manager.edge.screen_area()
            for edge in ("right", "left", "top", "bottom"):
                shelf.hide_immediately()
                manager.edge_suppressed = False
                manager.settings.update(dock_edge=edge, dock_position=55, dock_length=120, dock_thickness=10)
                manager.update_handles()
                trigger = manager.edge.geometry()
                assert area.contains(trigger)
                if edge in ("right", "left"):
                    assert (trigger.width(), trigger.height()) == (10, 120)
                    assert trigger.left() == area.left() if edge == "left" else trigger.right() == area.right()
                else:
                    assert (trigger.width(), trigger.height()) == (120, 10)
                    assert trigger.top() == area.top() if edge == "top" else trigger.bottom() == area.bottom()
                poll_at(trigger.center())
                assert shelf.isVisible() and shelf.presentation_kind == "dock" and shelf.hover_region_contains(trigger.center())
                outside = QPoint(area.right(), area.bottom()) if edge in ("left", "top") else QPoint(area.left(), area.top())
                assert not shelf.hover_region_contains(outside)
                poll_at(outside)
                assert shelf.visible_state == "hiding"
                wait_until(app, lambda: not shelf.isVisible())
            chosen = Path(temporary) / "new-content-folder"
            manager.settings.update(capture_dir=chosen)
            payload = QMimeData(); payload.setImageData(picture)
            old = set(shelf.files["permanent"])
            assert shelf.receive_mime("permanent", payload)
            capture = next(iter(set(shelf.files["permanent"]) - old))
            assert Path(capture).parent == chosen and Path(capture).is_file() and original.is_file()
            shelf.incoming_drop = False
        check("all configured edge regions open on hover and chosen capture path remains effective", region_and_capture)

        def media_lifecycle():
            popup = open_overview(); popup.set_category("all")
            popup.select_item(str(video))
            media = popup.preview.media
            assert popup.preview.stack.currentWidget() is media and media._integration_source == str(video)
            media._integration_playing = True
            before = media_stop.call_count
            popup.select_item(native)
            assert media_stop.call_count > before and not media._integration_playing
            popup.select_item(str(audio))
            assert popup.preview.stack.currentWidget() is media and media._integration_source == str(audio)
            shelf.preview_cache.clear()
            media.frame_ready.emit(str(video), picture)
            assert str(video) in shelf.preview_cache
            pm = shelf.item_pixmap(str(video), 64, dpr=2)
            assert pm.width() == 128 and pm.devicePixelRatioF() == 2
            media._integration_playing = True
            shelf.collapse_contents(); flush(app)
            assert not media._integration_playing
            shelf.show_contents(); media._integration_playing = True
            shelf.hide_animated(explicit=True)
            assert not media._integration_playing
            wait_until(app, lambda: not shelf.isVisible())
        check("media stack uses native player contract, caches DPR-aware posters and stops on ref/collapse/hide", media_lifecycle)

        def baseline_drops():
            open_overview()
            mime = QMimeData(); mime.setText("v1.6 expanded native text drop")
            enter = QDragEnterEvent(QPoint(500, 200), Qt.DropAction.CopyAction, mime,
                                   Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(shelf, enter)
            drop = QDropEvent(QPointF(500, 200), Qt.DropAction.CopyAction, mime,
                              Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(shelf, drop)
            assert enter.isAccepted() and drop.isAccepted()
            ref = next(ref for ref in shelf.files["permanent"] if shelf.content.text(ref) == mime.text())
            native_mime = shelf.content.mime_for([ref])
            assert native_mime.text() == mime.text() and not native_mime.hasUrls()
            before = list(shelf.files["permanent"])
            with patch.object(subject.QDrag, "exec", lambda *args: Qt.DropAction.CopyAction):
                shelf.drag_files("permanent", [ref])
            assert shelf.files["permanent"] == before
            shelf.complete_drag("permanent", [ref], Qt.DropAction.IgnoreAction)
            assert shelf.files["permanent"] == before
            manager.outgoing_drag = True
            shelf.dropEvent(drop)
            assert not drop.isAccepted()
            manager.outgoing_drag = False
            shelf.incoming_drop = False
        check("expanded synthetic drop, native text export and permanent drag retention preserve baseline", baseline_drops)

        def resolved_urls_and_capacity():
            # Inspect resolution while pinned; an unpinned accepted drop now
            # folds visibly before landing/tucking instead of hiding a pulse.
            shelf._cancel_intake();shelf.pinned=True
            popup = open_overview(); popup.set_category("all")
            payload = QMimeData(); payload.setText("https://example.invalid/new-article")
            before = set(shelf.files["permanent"])
            assert shelf.receive_mime("permanent", payload)
            url_ref = next(iter(set(shelf.files["permanent"]) - before))
            assert shelf.resolver.jobs[-1] == (url_ref, payload.text())
            assert shelf.content.category(url_ref) == "links"
            popup.select_item(url_ref)
            shelf._resolved_content(url_ref, {"title": "Resolved article", "preview_image": str(image), "source_url": payload.text()})
            assert popup.preview.current_path == url_ref and popup.preview.stack.currentWidget() is popup.preview.image
            assert popup.preview.name.full_text == "Resolved article"
            exported = shelf.content.mime_for([url_ref])
            assert exported.text() == payload.text() and not exported.hasUrls()
            shelf._resolved_content(url_ref, {"path": str(vector), "source_url": payload.text(), "kind": "image"})
            assert url_ref not in shelf.files["permanent"] and str(vector) in shelf.files["permanent"]
            assert popup.primary_selection == str(vector)
            shelf.remove_path("permanent", str(vector))
            shelf._resolved_content(str(vector), {"title": "Late callback must not revive item"})
            assert str(vector) not in shelf.files["permanent"]
            count = len(shelf.files["permanent"])
            new_refs = [shelf.content.add_text(f"Capacity item {index}") for index in range(125)]
            shelf.add_paths("permanent", new_refs)
            flush(app)
            assert len(shelf.files["permanent"]) == count + 125
            assert len(popup.visible_refs()) == count + 125
            popup.select_item(new_refs[-1])
            assert popup.preview.text.toPlainText() == "Capacity item 124"
            shelf._cancel_intake();shelf.pinned=False
        check("resolved URL metadata keeps native link export, removed callbacks stay removed and 125 extra items remain accessible", resolved_urls_and_capacity)

        def geometry():
            class Screen:
                def availableGeometry(self): return QRect(0, 0, 1920, 1080)
                def devicePixelRatio(self): return 1.0
            shelf.hide_immediately()
            shelf.geometry_morph.motion = True
            screen = Screen()
            with patch.object(subject.QGuiApplication, "screenAt", return_value=screen), \
                 patch.object(subject.QGuiApplication, "primaryScreen", return_value=screen):
                shelf._show_at(QPoint(1200, 200), "manual", QPoint())
                origin = shelf.visual_geometry()
                shelf.show_contents()
                host = shelf.geometry()
                source_geometry=QRect(shelf.card.geometry())
                assert host.contains(origin)
                right = origin.x() + origin.width()
                for moment in (20, 65, 130, 200):
                    shelf.geometry_morph.animation.setCurrentTime(moment)
                    visual = shelf.visual_geometry()
                    assert shelf.geometry() == host, "Native HWND geometry changed during morph"
                    assert visual.x() + visual.width() == right, "Painted right edge moved"
                    assert shelf.card.geometry()==source_geometry,"Live source tree moved during compositing"
                shelf.geometry_morph.animation.setCurrentTime(230)
                assert shelf.geometry() == host and host.contains(shelf.visual_geometry())
                shelf.collapse_contents()
                host = shelf.geometry()
                for moment in (30, 90, 180):
                    shelf.geometry_morph.animation.setCurrentTime(moment)
                    assert shelf.geometry() == host
                    assert shelf.visual_geometry().x() + shelf.visual_geometry().width() == right
                shelf.geometry_morph.animation.setCurrentTime(230)
                assert shelf.geometry() == host and host.contains(shelf.visual_geometry())
            shelf.geometry_morph.motion = False
        check("expansion/collapse hold painted right edge and native union bounds until settling", geometry)

        def clear_confirmation():
            refs = list(shelf.files["permanent"])
            def answer(dialog, result):
                assert dialog.cancel_button.isDefault()
                assert dialog.cancel_button.text()=='取消' and dialog.delete_button.text()=='删除'
                assert not dialog.isWindow() and not dialog.description.wordWrap()
                return result
            with patch.object(subject.ClearConfirmOverlay, "exec", lambda dialog: answer(dialog, False)):
                assert not shelf.confirm_clear_all()
            assert shelf.files["permanent"] == refs
            with patch.object(subject.ClearConfirmOverlay, "exec", lambda dialog: answer(dialog, True)):
                assert shelf.confirm_clear_all()
            assert shelf.files["permanent"] == [] and original.is_file() and image.is_file() and document.is_file()
        check("clear all defaults to cancel and requires Yes while retaining original disk files", clear_confirmation)

        def empty_shortcuts():
            shelf.hide_immediately()
            with patch.object(subject.QCursor, "pos", return_value=QPoint(400, 350)):
                for role in ("shelf", "overview", "shelf"):
                    identifier = next(key for key, (_, name) in manager.hotkeys._registrations.items() if name == role)
                    assert manager.hotkeys._dispatch(identifier)
                    assert shelf.expanded == (role == "overview") and manager.shelves == [shelf]
                    assert shelf.presentation_kind == ("shortcut" if role == "shelf" else "overview")
                    manager._content_drag_validated(QPoint(10, 10))
                    assert shelf.presentation_kind != "drag"
            assert "0 项" in shelf.popup.title.text()
        check("empty shelf supports both global actions on the same shelf and visible shortcuts cannot be retriggered by shake", empty_shortcuts)

        def pending_restart():
            profile = Path(temporary) / "restart-profile"
            profile.mkdir()
            http_ref, data_ref, old_ref = ["flickdock:text:restart-" + key for key in ("http", "data", "old")]
            http_url = "https://example.invalid/interrupted"
            data_uri = "data:image/png;base64,aW50ZXJydXB0ZWQ="
            state = {"version": 6, "shelf": {"permanent": [http_ref, data_ref, old_ref],
                "texts": {http_ref: http_url, data_ref: "正在解析图片 · saved", old_ref: "Legacy interrupted item"},
                "metadata": {http_ref: {"loading": True, "source_url": http_url},
                             data_ref: {"loading": True, "source_url": data_uri},
                             old_ref: {"loading": True, "source_url": ""}}}}
            state_path = profile / "shelves.json"
            state_path.write_text(json.dumps(state), encoding="utf8")
            with patch.object(subject, "BASE", profile), patch.object(subject, "STATE_FILE", state_path), \
                 patch.object(subject, "OLD_STATE_FILE", profile / "absent-legacy.json"):
                restarted = subject.ShelfManager(app)
                restarted.timer.stop()
                restored = restarted.shelf
                assert restored.resolver.pending_refs == frozenset()
                flush(app)
                assert set(restored.resolver.jobs) == {(http_ref, http_url), (data_ref, data_uri)}
                restored._resume_pending_content()
                assert len(restored.resolver.jobs) == 2, "Startup must not duplicate in-flight jobs"
                assert restored.content.metadata[old_ref]["loading"] is False
                assert restored.content.metadata[old_ref].get("error")
                restored._resolved_content(http_ref, {"title": "Recovered webpage", "source_url": http_url})
                assert restored.content.metadata[http_ref]["loading"] is False
                restored._resolved_content(data_ref, {"path": str(image), "source_url": data_uri,
                                                     "final_url": data_uri, "kind": "image"})
                assert data_ref not in restored.files["permanent"] and str(image) in restored.files["permanent"]
                assert "source_url" not in restored.content.metadata[str(image)]
                assert "final_url" not in restored.content.metadata[str(image)]
                saved = json.loads(state_path.read_text(encoding="utf8"))["shelf"]
                assert data_ref not in saved["texts"] and data_uri not in state_path.read_text(encoding="utf8")
                assert saved["metadata"][old_ref]["loading"] is False
                restarted.quit()
                restored.deleteLater(); restarted.edge.deleteLater(); restarted.tray.deleteLater()
                flush(app)
        check("startup resumes HTTP/data jobs once, completed data URI is pruned and old missing sources stop loading", pending_restart)

        registry_read.assert_not_called()
        registry_write.assert_not_called()
        manager.quit()
        flush(app)

if failures:
    for name, error in failures:
        print("AUDIT_FAILURE:", name, error)
    raise AssertionError(f"{len(failures)} v1.6 integration checks failed")
print("FLICKDOCK_V16_OK")
