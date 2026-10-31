"""LAN list reachability, native picker entry and minimum-window geometry.

All windows are Qt offscreen, picker calls are intercepted, and the fake LAN
service never starts discovery, sockets or access to the user's files.
"""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QMenu, QPushButton

from app_settings import SettingsStore
from lan_panel import LanDialog
from native_ui_common import configure_reference_font
from test_html_native_lan import APP, FakeLan


class LanHigLayoutTests(unittest.TestCase):
    def setUp(self):
        self.screen=patch('window_chrome.WindowChrome._available',return_value=__import__('PySide6.QtCore',fromlist=['QRect']).QRect(0,0,1920,1080));self.screen.start()
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-lan-hig-')
        self.base=Path(self.temp.name)
        configure_reference_font(APP)
        self.settings=SettingsStore(self.base/'profile')
        self.service=FakeLan()
        self.dialog=LanDialog(self.service,self.settings)
        self.panel=self.dialog.panel
        self.dialog.show();self.settle()

    def tearDown(self):
        self.dialog.close();self.dialog.deleteLater();self.settle();self.screen.stop();self.temp.cleanup()

    @staticmethod
    def settle():
        for _ in range(5):
            APP.processEvents()
            QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)

    def files(self,count=10):
        paths=[]
        for index in range(count):
            path=self.base/f'FlickDock_Design_Export_{index+1:02d}.png'
            path.write_bytes(b'original file bytes');paths.append(str(path))
        return paths

    def history(self,count=12):
        names=('FlickDock_Design_System.fig','Booth_M31_Print_Ready.pdf',
               'UI_Icons_Bundle.zip','Promo_Reel_4K_Master.mp4')
        return [dict(when=f'10-28 14:{28-index:02d}',name=names[index%4],
                     peer="Roger's MacBook Pro" if index%2 else 'Studio Workstation',
                     state=('完成','已取消','失败')[index%3],
                     direction='send' if index%2 else 'receive')
                for index in range(count)]

    def load_busy_state(self):
        self.panel.queue_paths(self.files())
        self.panel._history=self.history()
        self.panel._save_history();self.panel._render_history()
        self.panel._progress(dict(id='active',direction='send',peer='Studio Workstation',
            state='transferring',name='Promo_Reel_4K_Master.mp4',
            received=106*1024**2,total=148*1024**2))
        self.panel.show_offer(dict(id='incoming',peer="Roger's MacBook Pro",count=3,
            names=['Design_System_v2.fig','Campaign_Hero_4K.png','Motion_Tokens.json'],
            total=86*1024**2,code='849249'))
        self.settle()

    def test_click_and_keyboard_use_native_file_picker_folder_stays_in_context_menu(self):
        source=self.files(1)[0]
        self.assertFalse(any(button.text() in ('添加文件','添加文件夹')
                             for button in self.panel.findChildren(QPushButton)))
        with patch('lan_panel.QFileDialog.getOpenFileNames',return_value=([source],'')) as picker:
            QTest.mouseClick(self.panel.drop_zone.title,Qt.MouseButton.LeftButton)
            self.settle()
            self.assertEqual(self.panel._queued_paths,[source])
            self.assertEqual(picker.call_count,1)
            self.panel.queue_paths([],replace=True)
            QTest.keyClick(self.panel.drop_zone,Qt.Key.Key_Space)
            self.assertEqual(picker.call_count,2)
            self.assertEqual(self.panel._queued_paths,[source])
        folder=self.base/'Campaign Sources';folder.mkdir()
        class FolderMenu(QMenu):
            def exec(self,_point):self.actions()[1].trigger()
        with patch('lan_panel.QFileDialog.getExistingDirectory',return_value=str(folder)) as picker, \
             patch('lan_panel.QMenu',FolderMenu):
            point=self.panel.drop_zone.rect().center()
            event=QContextMenuEvent(QContextMenuEvent.Reason.Mouse,point,
                self.panel.drop_zone.mapToGlobal(point))
            APP.sendEvent(self.panel.drop_zone,event)
            self.assertEqual(picker.call_count,1)
            self.assertIn(str(folder),self.panel._queued_paths)

    def test_every_queued_file_and_all_twelve_records_are_scrollable_and_delete_persists(self):
        self.load_busy_state()
        panel=self.panel
        self.assertEqual(len(panel.queue_rows),10)
        self.assertTrue(all(row.height()==36 for row in panel.queue_rows))
        self.assertGreater(panel.queue_scroll.verticalScrollBar().maximum(),0)
        panel.queue_scroll.ensureWidgetVisible(panel.queue_rows[-1]);self.settle()
        self.assertTrue(panel.queue_scroll.viewport().rect().intersects(
            panel.queue_rows[-1].rect().translated(panel.queue_rows[-1].mapTo(panel.queue_scroll.viewport(),QPoint(0,0)))))
        self.assertEqual(len(panel.history_rows),12)
        self.assertTrue(all(row.height()==52 for row in panel.history_rows))
        self.assertGreater(panel.history_scroll.verticalScrollBar().maximum(),0)
        panel.history_scroll.ensureWidgetVisible(panel.history_rows[-1]);self.settle()
        last=panel.history_rows[-1]
        self.assertTrue(panel.history_scroll.viewport().rect().intersects(
            last.rect().translated(last.mapTo(panel.history_scroll.viewport(),QPoint(0,0)))))
        name=last.detail.accessibleName()
        # Both nested viewports must expose the actual target, rather than
        # merely intersecting the last row inside a clipped history section.
        panel.activity_scroll.ensureWidgetVisible(panel.history_scroll,0,0);self.settle()
        panel.history_scroll.ensureWidgetVisible(last.remove_button,0,0);self.settle()
        for area in (panel.activity_scroll,panel.history_scroll):
            target=last.remove_button
            self.assertTrue(area.viewport().rect().contains(target.mapTo(area.viewport(),target.rect().topLeft())))
            self.assertTrue(area.viewport().rect().contains(target.mapTo(area.viewport(),target.rect().bottomRight())))
        clicked=[];last.remove_button.clicked.connect(lambda:clicked.append(True))
        QTest.mouseClick(last.remove_button,Qt.MouseButton.LeftButton,pos=last.remove_button.rect().center());self.settle()
        self.assertEqual(len(panel.history_rows),11,dict(clicked=clicked,status=panel.status_label.text(),modal=repr(APP.activeModalWidget()),popup=repr(APP.activePopupWidget())))
        saved=json.loads(panel._history_path.read_text(encoding='utf-8'))
        self.assertEqual(len(saved),11)
        self.assertEqual([record['name'] for record in saved],[record['name'] for record in panel._history])
        self.assertIn('active',panel._rows)
        self.assertEqual(len(panel._queued_paths),10)
        self.assertTrue(all(Path(path).is_file() for path in panel._queued_paths))

    def test_device_chevron_is_a_centered_fourteen_pixel_vector(self):
        calls=[]
        class Chevron:
            def paint(self,_painter,x,y,width,height):calls.append((x,y,width,height))
        with patch('lan_panel.ui_icon',return_value=Chevron()) as factory:
            self.panel.devices.grab()
        self.assertEqual(factory.call_args.args[0],'chevron')
        self.assertEqual(len(calls),1)
        _x,y,width,height=calls[0]
        self.assertEqual((width,height),(14,14))
        self.assertLessEqual(abs(y+height/2-self.panel.devices.height()/2),.5)

    def test_busy_default_and_minimum_windows_keep_actions_complete_and_surface_opaque(self):
        self.load_busy_state()
        panel=self.panel
        self.assertEqual(self.dialog.size().toTuple(),(980,744))
        self.assertEqual(self.dialog.minimumSize().toTuple(),(824,660))
        for size in ((980,704),(824,660)):
            for theme in ('dark','light'):
                with self.subTest(size=size,theme=theme):
                    self.dialog.resize(*size);self.dialog.apply_theme(theme);self.settle()
                    for name in ('drop_zone','queue_scroll','devices','send_code',
                            'send_button','history_scroll','receive_path','choose_destination'):
                        widget=getattr(panel,name)
                        self.assertTrue(widget.isVisible())
                        for scroll in (panel.send_scroll,panel.activity_scroll):
                            if scroll.widget().isAncestorOf(widget):
                                # ensureWidgetVisible uses a style-dependent
                                # focus rect. Exercise actual scroll-bar reachability
                                # and still require the whole outer frame to fit.
                                viewport=scroll.viewport();bar=scroll.verticalScrollBar()
                                top=widget.mapTo(viewport,widget.rect().topLeft()).y()
                                bottom=widget.mapTo(viewport,widget.rect().bottomRight()).y()
                                if top<0:bar.setValue(bar.value()+top-8)
                                elif bottom>=viewport.height():bar.setValue(bar.value()+bottom-viewport.height()+9)
                                self.settle()
                                bounds=(name,widget.size().toTuple(),viewport.size().toTuple(),
                                        widget.mapTo(viewport,widget.rect().topLeft()).toTuple(),
                                        widget.mapTo(viewport,widget.rect().bottomRight()).toTuple(),
                                        bar.value(),bar.maximum())
                                self.assertTrue(viewport.rect().contains(widget.mapTo(viewport,widget.rect().topLeft())),bounds)
                                self.assertTrue(viewport.rect().contains(widget.mapTo(viewport,widget.rect().bottomRight())),bounds)
                        self.assertTrue(panel.rect().contains(widget.mapTo(panel,widget.rect().topLeft())),widget.objectName())
                        self.assertTrue(panel.rect().contains(widget.mapTo(panel,widget.rect().bottomRight())),widget.objectName())
                    for row in panel._rows.values():
                        if not row['frame'].isVisible():continue
                        for button in row['frame'].findChildren(QPushButton):
                            if button.isVisible():
                                self.assertTrue(row['frame'].rect().contains(button.mapTo(row['frame'],button.rect().bottomRight())))
                    illustration=panel.drop_zone.illustration
                    center=illustration.mapTo(panel.drop_zone,illustration.rect().center())
                    self.assertLessEqual(abs(center.x()-panel.drop_zone.rect().center().x()),1)
                    image=self.dialog.grab().toImage()
                    self.assertEqual(image.pixelColor(25,image.height()//2).alpha(),255)
                    if os.environ.get('FLICKDOCK_CAPTURE_HIG_LAN'):
                        destination=Path(__file__).resolve().parents[2]/'outputs'/'hig-redesign'
                        destination.mkdir(parents=True,exist_ok=True)
                        suffix='minimum' if size==(824,660) else 'default'
                        image.save(str(destination/f'lan-{theme}-{suffix}.png'))


if __name__=='__main__':unittest.main(verbosity=2)
