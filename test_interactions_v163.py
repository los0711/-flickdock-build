"""Public shelf entry checks, with isolated data and offscreen Qt only."""
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import patch
os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtCore import QPoint,Qt,QEvent,QPointF
from PySide6.QtGui import QContextMenuEvent,QMouseEvent,QEnterEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QSystemTrayIcon

def until(predicate):
    deadline=time.monotonic()+5
    while not predicate() and time.monotonic()<deadline:app.processEvents();QTest.qWait(10)
    assert predicate()

with tempfile.TemporaryDirectory(prefix='flickdock-interactions-v163-') as tmp:
    os.environ['APPDATA']=str(Path(tmp)/'profile')
    import flickdock as subject,app_settings
    from content_grid import IconItem
    app=QApplication([]);subject.load_font(app)
    with patch.object(subject,'motion_enabled',return_value=False),patch.object(QSystemTrayIcon,'show'),patch.object(QSystemTrayIcon,'hide'),patch.object(app_settings,'_read_startup_value') as reg_read,patch.object(app_settings,'_write_startup_value') as reg_write:
        manager=subject.ShelfManager(app);manager.timer.stop();shelf=manager.shelf
        refs=[]
        for i in range(3):
            path=Path(tmp)/f'original-{i}.txt';path.write_text(str(i),encoding='utf8');refs.append(str(path))
        shelf.add_paths('permanent',refs)
        shelf.show_transient(QPoint(400,350));shelf.show_contents();popup=shelf.popup
        for mode,kind in (('list',subject.FileRow),('icons',IconItem)):
            manager.settings.update(view_mode=mode)
            until(lambda:popup._build_cursor==len(popup.visible_refs()) and popup.retirement.is_idle)
            app.processEvents()
            items={item.path:item for item in popup.rows_body.findChildren(kind) if item.isVisible()}
            popup.selected.clear();popup.primary_selection=None
            item=items[refs[0]]
            assert item.remove_button.isHidden()
            before=item.size()
            QApplication.sendEvent(item,QEnterEvent(QPointF(10,10),QPointF(10,10),QPointF(10,10)))
            assert item.remove_button.isVisible() and item.size()==before
            QApplication.sendEvent(item,QEvent(QEvent.Type.Leave))
            assert item.remove_button.isHidden() and item.size()==before
            QTest.mouseClick(item,Qt.MouseButton.LeftButton)
            assert popup.selected=={refs[0]} and popup.zip_button.isEnabled()
            QTest.mouseClick(item,Qt.MouseButton.LeftButton)
            assert not popup.selected and not popup.zip_button.isEnabled()
            assert popup.zip_button.isVisible() and popup.zip_all_button.isEnabled()
            QTest.mouseClick(item,Qt.MouseButton.LeftButton)
            QTest.mouseClick(items[refs[1]],Qt.MouseButton.LeftButton,Qt.KeyboardModifier.ControlModifier)
            chosen=set(popup.selected);exports=[]
            def drag(drag,*args):
                exports.append({str(Path(url.toLocalFile()).resolve()) for url in drag.mimeData().urls()});return Qt.DropAction.CopyAction
            QTest.mousePress(item,Qt.MouseButton.LeftButton,pos=QPoint(15,25))
            move=QMouseEvent(QEvent.Type.MouseMove,QPointF(40,25),QPointF(item.mapToGlobal(QPoint(40,25))),Qt.MouseButton.NoButton,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
            with patch.object(subject.QDrag,'exec',drag):QApplication.sendEvent(item,move)
            QTest.mouseRelease(item,Qt.MouseButton.LeftButton,pos=QPoint(40,25))
            assert exports==[chosen] and popup.selected==chosen,(mode,exports,chosen,popup.selected)
        packed=[]
        with patch.object(shelf,'compress_refs',side_effect=lambda paths:packed.append(list(paths))):
            popup.zip_button.click();popup.set_category('images');popup.zip_all_button.click()
        assert set(packed[0])==chosen and packed[1]==refs
        popup.set_category('all')
        menus=[]
        def read_menu(menu,*args):menus.append([action.text() for action in menu.actions()])
        with patch.object(subject.AnimatedMenu,'exec',read_menu):
            shelf.show_menu(pos=QPoint(200,100))
            popup.selected.clear();popup.show_item_menu(refs[0],QPoint(200,100))
        assert not any('重命名' in label or label=='打开文件' or '移动' in label for label in menus[0])
        assert any('重命名' in label for label in menus[1]) and '打开文件' in menus[1]
        shelf.rename_ref(refs[0]);shelf.rename_edit.setText('renamed')
        QTest.keyClick(shelf.rename_edit,Qt.Key.Key_Return)
        renamed=str(Path(tmp)/'renamed.txt')
        assert Path(renamed).read_text()=='0' and not Path(refs[0]).exists()
        assert shelf.files['permanent']==[renamed,*refs[1:]]
        assert popup.primary_selection==renamed
        moved=Path(tmp)/'moved';moved.mkdir()
        with patch.object(subject.QFileDialog,'getExistingDirectory',return_value=str(moved)):
            shelf.move_files('permanent',[renamed])
        assert (moved/'renamed.txt').read_text()=='0' and not Path(renamed).exists()
        assert all(Path(ref).is_file() for ref in refs[1:])
        assert popup.primary_selection==str(moved/'renamed.txt')
        shelf.hide_immediately();manager.settings.update(trigger_view='expanded')
        manager.edge_suppressed=False;manager.show_docked()
        assert shelf.expanded and shelf.presentation_kind=='dock'
        shelf.hide_immediately();manager.auto_shown=None
        manager._content_drag_validated(QPoint(300,300))
        assert shelf.expanded and shelf.presentation_kind=='drag'
        assert all(button.isHidden() for button in (shelf.pin_button,shelf.expand_button,shelf.menu_button,shelf.close_button))
        shelf.hide_immediately();manager.auto_shown=None;manager.settings.update(trigger_view='compact')
        manager._content_drag_validated(QPoint(300,300));assert not shelf.expanded
        shelf.remove_all('permanent');shelf.hide_immediately()
        manager.update_handles()
        assert manager.settings.keep_empty_handle and manager.edge.isVisible()
        manager.settings.update(keep_empty_handle=False);assert not manager.edge.isVisible()
        manager.settings.update(keep_empty_handle=True)
        assert manager.edge.isVisible()
        manager.settings.update(keep_empty_handle=False);assert not manager.edge.isVisible()
        manager.settings.update(shake_sensitivity=100)
        assert manager.shake_detector.config.minimum_peak_speed<750
        manager.settings.update(shake_sensitivity=1)
        assert manager.shake_detector.config.minimum_peak_speed>750
        reg_read.assert_not_called();reg_write.assert_not_called()
        manager.quit();app.processEvents()
        print('INTERACTIONS_V163_OK')
