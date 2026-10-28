"""Real offscreen ZIP button -> save/cancel dialog -> asynchronous archive."""
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtCore import QPoint,QRect,QTimer,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QDialogButtonBox,QFileDialog,QSystemTrayIcon,QLineEdit

def until(predicate,timeout=5):
    deadline=time.monotonic()+timeout
    while not predicate() and time.monotonic()<deadline:
        app.processEvents();QTest.qWait(10)
    assert predicate(),'ZIP operation did not finish'

with tempfile.TemporaryDirectory(prefix='flickdock-zip-dialog-') as tmp:
    os.environ['APPDATA']=str(Path(tmp)/'profile')
    import flickdock as subject,app_settings
    app=QApplication([]);subject.load_font(app)
    screen=SimpleNamespace(availableGeometry=lambda:QRect(0,0,1920,1080))
    with patch.object(subject.QGuiApplication,'screenAt',return_value=screen),patch.object(subject.QGuiApplication,'primaryScreen',return_value=screen),patch.object(subject,'motion_enabled',return_value=False),patch.object(QSystemTrayIcon,'show'),patch.object(QSystemTrayIcon,'hide'),patch.object(app_settings,'_read_startup_value'),patch.object(app_settings,'_write_startup_value'):
        manager=subject.ShelfManager(app);manager.timer.stop();shelf=manager.shelf
        one,two=Path(tmp)/'one.txt',Path(tmp)/'two.txt'
        one.write_text('one',encoding='utf8');two.write_text('two',encoding='utf8')
        raw=shelf.content.add_text('原生文字')
        shelf.add_paths('permanent',[str(one),str(two),raw])
        shelf.show_transient(QPoint(400,350));shelf.show_contents()
        popup=shelf.popup
        until(lambda:popup._build_cursor==len(popup.visible_refs()))
        popup.select_item(str(one));popup.select_item(raw,toggle=True)
        assert popup.zip_button.isEnabled() and popup.zip_button.isVisible()
        errors=[];destination=Path(tmp)/'selected.zip'
        def interact(save):
            try:
                dialog=QApplication.activeModalWidget()
                assert isinstance(dialog,QFileDialog) and dialog.isVisible()
                assert not dialog.testOption(QFileDialog.Option.DontUseNativeDialog)
                assert dialog.parentWidget() is shelf
                assert not dialog.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
                if save:
                    dialog.setDirectory(str(destination.parent))
                    name=dialog.findChild(QLineEdit,'fileNameEdit')
                    assert name is not None
                    name.setText(destination.name)
                    box=dialog.findChild(QDialogButtonBox)
                    button=box.button(QDialogButtonBox.StandardButton.Save)
                    assert button is not None and button.isVisible() and button.isEnabled()
                    QTest.mouseClick(button,Qt.MouseButton.LeftButton)
                else:dialog.reject()
            except Exception as error:
                errors.append(repr(error))
                for dialog in shelf.findChildren(QFileDialog):dialog.reject()
        QTimer.singleShot(100,lambda:interact(False))
        QTest.mouseClick(popup.zip_button,Qt.MouseButton.LeftButton)
        assert not errors and shelf.archive_job is None and not destination.exists(),errors
        QTimer.singleShot(100,lambda:interact(True))
        QTest.mouseClick(popup.zip_button,Qt.MouseButton.LeftButton)
        assert not errors,errors
        try:until(lambda:shelf.archive_job is None and destination.exists())
        except AssertionError:
            print('ZIP_DIAGNOSTIC',shelf.archive_job,repr(popup.preview.info.text()),shelf.files['permanent'],list(Path(tmp).rglob('*.zip')))
            raise
        with zipfile.ZipFile(destination) as archive:
            assert archive.testzip() is None
            assert len(archive.namelist())==2 and archive.read('one.txt')==b'one'
            assert any(archive.read(name).decode('utf8')=='原生文字' for name in archive.namelist())
            assert 'two.txt' not in archive.namelist()
        assert one.read_text()=='one' and two.read_text()=='two'
        assert str(destination) in shelf.files['permanent']
        assert popup.primary_selection==str(destination) and popup.category=='all'
        assert str(destination) in popup.preview.info.full_text
        # Failed asynchronous decoding must preserve the old format-card
        # fallback instead of leaving a permanently empty image panel.
        broken=Path(tmp)/'broken.png';broken.write_bytes(b'not an image')
        shelf.add_paths('permanent',[str(broken)])
        popup.select_item(str(broken))
        until(lambda:shelf.image_cache.image(str(broken),2048) is not None)
        assert popup.preview.stack.currentWidget() is popup.preview.icon
        print('ZIP_DIALOG_V162_OK')
        manager.quit();app.processEvents()
