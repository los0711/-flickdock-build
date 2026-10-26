"""Hotkey while dragging -> overview child surfaces import files/content."""
import os
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch
os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtCore import QPoint,QPointF,QMimeData,QUrl,Qt,QTimer
from PySide6.QtGui import QDragEnterEvent,QDragMoveEvent,QDropEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QSystemTrayIcon

def until(predicate,seconds=5):
    deadline=time.monotonic()+seconds
    while not predicate() and time.monotonic()<deadline:app.processEvents();QTest.qWait(5)
    assert predicate(),'Overview drop/copy did not finish'

with tempfile.TemporaryDirectory(prefix='flickdock-overview-drop-') as tmp:
    os.environ['APPDATA']=str(Path(tmp)/'profile')
    import flickdock as subject,app_settings,capture_files
    from shake_detector import ShakeDetector,config_for_sensitivity
    app=QApplication([]);subject.load_font(app)
    with patch.object(subject,'motion_enabled',return_value=False),patch.object(QSystemTrayIcon,'show'),patch.object(QSystemTrayIcon,'hide'),patch.object(app_settings,'_read_startup_value'),patch.object(app_settings,'_write_startup_value'):
        manager=subject.ShelfManager(app);manager.timer.stop();shelf=manager.shelf;shelf.pinned=True
        def overview():
            with patch.object(manager,'_pointer_dragging',return_value=True),patch.object(shelf,'activateWindow') as activate,patch.object(shelf,'setFocus') as focus:
                manager.show_overview();activate.assert_not_called();focus.assert_not_called()
            assert shelf.expanded and shelf.isVisible()
        overview();popup=shelf.popup
        targets=[popup,popup.scroll.viewport(),popup.rows_body,popup.preview,popup.preview.text.viewport(),popup.preview.image,popup.preview.media.surface]
        for index,target in enumerate(targets):
            path=Path(tmp)/f'file-{index}.txt';path.write_text(str(index),encoding='utf8')
            mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(path))])
            enter=QDragEnterEvent(QPoint(3,3),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(target,enter);assert enter.isAccepted(),type(target).__name__
            move=QDragMoveEvent(QPoint(4,4),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(target,move);assert move.isAccepted()
            drop=QDropEvent(QPointF(4,4),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(target,drop);assert drop.isAccepted()
            assert str(path) in shelf.files['permanent']
        mime=QMimeData();mime.setText('拖入总览的文字')
        enter=QDragEnterEvent(QPoint(3,3),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(popup.preview.text.viewport(),enter)
        drop=QDropEvent(QPointF(4,4),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(popup.preview.text.viewport(),drop)
        assert drop.isAccepted() and shelf.content.text(shelf.files['permanent'][-1])==mime.text()

        # Keep the real 20ms manager polling alive while a worker is blocked.
        # Polling must neither cancel imports nor wait for file work on the UI.
        manager.settings.update(capture_mode='copy')
        source=Path(tmp)/'poll-copy.bin';source.write_bytes(b'x'*1024*1024)
        gate=threading.Event();entered=threading.Event();original=capture_files._copy_file
        def slow_copy(*args):entered.set();gate.wait(2);return original(*args)
        with patch.object(capture_files,'_copy_file',slow_copy):
            shelf.import_paths('permanent',[str(source)])
            assert entered.wait(2)
            epoch=shelf._copy_epoch;beats=[]
            ticker=QTimer();ticker.setInterval(10);ticker.timeout.connect(lambda:beats.append(time.monotonic()));ticker.start()
            manager.timer.start(20);QTimer.singleShot(160,gate.set)
            until(lambda:not shelf.copy_jobs)
            manager.timer.stop();ticker.stop()
        assert shelf._copy_epoch==epoch and len(beats)>=5
        assert Path(shelf.files['permanent'][-1]).read_bytes()==source.read_bytes()
        assert shelf.files['permanent'][-1]!=str(source)
        # Sensitivity varies effort; at all levels slow/window drags stay blocked.
        from test_shake_detector import triangle_samples,feed,wrist_samples
        medium=triangle_samples([.025],duration=1.2,amplitude=50,frequency=3)
        assert feed(ShakeDetector(config_for_sensitivity(100)),medium)
        assert not feed(ShakeDetector(config_for_sensitivity(1)),medium)
        for value in (1,50,100):
            assert not feed(ShakeDetector(config_for_sensitivity(value)),wrist_samples(),window_moving=True)
            assert not feed(ShakeDetector(config_for_sensitivity(value)),triangle_samples([.025],duration=3,frequency=.3))
        manager.quit();app.processEvents()
        print('OVERVIEW_DROP_V163_OK')
