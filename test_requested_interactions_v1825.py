"""Requested UI behavior through real Qt widgets in disposable offscreen profiles."""
import tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox,QPushButton
from test_html_native_workspace import APP,WorkspaceTests,flush,until
from test_html_native_lan import FakeLan
from app_settings import SettingsStore,SettingsDialog
from lan_panel import LanDialog


class ShelfInteractions(unittest.TestCase):
    setUp=WorkspaceTests.setUp
    tearDown=WorkspaceTests.tearDown
    overview=WorkspaceTests.overview

    def test_copy_path_is_plain_actual_path_and_preserves_original(self):
        popup=self.overview();popup.select_item(self.paths[0]);flush()
        button=popup.preview.copy_selection_button
        self.assertEqual(button.text(),'复制路径')
        QTest.mouseClick(button,Qt.LeftButton);flush()
        self.assertEqual(APP.clipboard().text(),str(Path(self.paths[0]).absolute()))
        self.assertFalse(APP.clipboard().mimeData().hasUrls())
        self.assertEqual(Path(self.paths[0]).read_bytes(),b'original 0')

    def test_delete_removes_all_selected_shelf_refs_in_both_views_not_originals(self):
        from content_grid import IconItem
        from flickdock import FileRow
        popup=self.overview()
        for mode,cls in (('icons',IconItem),('list',FileRow)):
            with self.subTest(mode=mode):
                self.shelf.add_paths('permanent',self.paths);flush();popup.change_view_mode(mode)
                until(lambda:popup._build_cursor==len(popup.visible_refs()) and popup.retirement.is_idle)
                popup.selected.clear();popup.select_item(self.paths[0]);popup.select_item(self.paths[1],toggle=True)
                self.assertEqual(set(popup.chosen_refs()),set(self.paths[:2]))
                rows=[w for w in popup.rows_body.findChildren(cls) if getattr(w,'ref',getattr(w,'path',None))==self.paths[0]]
                self.assertEqual(len(rows),1)
                QTest.keyClick(rows[0],Qt.Key_Delete);flush()
                self.assertNotIn(self.paths[0],self.shelf.files['permanent']);self.assertNotIn(self.paths[1],self.shelf.files['permanent'])
                self.assertIn(self.paths[2],self.shelf.files['permanent'])
                self.assertEqual([Path(p).read_bytes() for p in self.paths],[b'original 0',b'original 1',b'original 2'])

    def test_extension_change_cancel_keeps_editor_then_confirm_renames(self):
        self.overview();source=Path(self.paths[0]);self.shelf.rename_ref(str(source));flush()
        dialog=self.shelf._rename_dialog;edit=self.shelf.rename_edit
        self.assertEqual(edit.text(),source.name)
        edit.setText('renamed.bin')
        with patch('flickdock.QMessageBox.question',return_value=QMessageBox.StandardButton.Cancel) as question:
            QTest.mouseClick(dialog.confirm,Qt.LeftButton);flush()
        self.assertIn('修改文件扩展名可能导致文件无法正常打开',question.call_args.args[2])
        self.assertTrue(source.is_file());self.assertTrue(dialog.isVisible());self.assertTrue(edit.isVisible())
        with patch('flickdock.QMessageBox.question',return_value=QMessageBox.StandardButton.Yes):
            QTest.mouseClick(dialog.confirm,Qt.LeftButton);flush()
        until(lambda:not dialog.isVisible())
        self.assertFalse(source.exists());self.assertEqual((source.parent/'renamed.bin').read_bytes(),b'original 0')


class DialogInteractions(unittest.TestCase):
    def test_queue_type_icons_use_background_metadata_for_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);tree=root/'folder.with.suffix';tree.mkdir()
            image=root/'photo.png';image.write_bytes(b'original')
            dialog=LanDialog(FakeLan(),SettingsStore(root/'profile'));dialog.show();flush()
            try:
                panel=dialog.panel;panel.queue_paths([str(tree),str(image)])
                until(lambda:panel._queue_row_map[str(tree)].file_preview.property('fileType')=='folder')
                self.assertEqual(panel._queue_row_map[str(image)].file_preview.property('fileType'),'png')
                self.assertEqual(panel._queue_row_map[str(tree)].file_preview.size(),panel._queue_row_map[str(image)].file_preview.size())
                self.assertTrue(tree.is_dir());self.assertEqual(image.read_bytes(),b'original')
            finally:dialog.hide();dialog.deleteLater();flush()

    def test_close_keeps_active_transport_and_history_but_discards_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);service=FakeLan();service._transfers={'active':SimpleNamespace(active=True)}
            path=root/'original.bin';path.write_bytes(b'original')
            dialog=LanDialog(service,SettingsStore(root/'profile'));dialog.show();flush()
            try:
                panel=dialog.panel;panel.queue_paths([str(path)]);before=list(panel._history)
                dialog.close();until(lambda:not dialog.isVisible())
                self.assertEqual(service.calls,[]);self.assertTrue(service._transfers['active'].active)
                dialog.show();flush()
                self.assertEqual(panel._queued_paths,[]);self.assertEqual(panel._history,before)
                self.assertEqual(path.read_bytes(),b'original')
            finally:dialog.hide();dialog.deleteLater();flush()

    def test_copy_mode_requires_confirmation_without_changing_saved_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            store=SettingsStore(Path(directory)/'profile');dialog=SettingsDialog(store);dialog.show();flush()
            try:
                combo=dialog.capture_mode_combo;index=combo.findData('copy');original=store.capture_mode
                QTest.mouseClick(dialog.settings_category_buttons['sync'],Qt.LeftButton);flush()
                buttons=[w for w in dialog.findChildren(QPushButton) if w.text()=='复制到保存位置' and w.isVisible()]
                self.assertEqual(len(buttons),1)
                with patch('app_settings.QMessageBox.question',return_value=QMessageBox.StandardButton.Cancel) as warning:
                    QTest.mouseClick(buttons[0],Qt.LeftButton);flush()
                warning.assert_called_once()
                self.assertEqual(combo.currentData(),original);self.assertEqual(store.capture_mode,original)
                self.assertFalse(buttons[0].isChecked())
                with patch('app_settings.QMessageBox.question',return_value=QMessageBox.StandardButton.Yes) as warning:
                    QTest.mouseClick(buttons[0],Qt.LeftButton);flush()
                warning.assert_called_once()
                self.assertEqual(combo.currentData(),'copy');self.assertEqual(store.capture_mode,original)
            finally:dialog.hide();dialog.deleteLater();flush()


if __name__=='__main__':unittest.main()
