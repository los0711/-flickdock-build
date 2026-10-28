"""Native reference workspace geometry, status, menus and real ZIP workflow.

Qt runs offscreen with disposable files, no global hooks, network or registry.
"""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'

from contextlib import ExitStack
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

import app_settings
import flickdock as subject
import ui_theme
from reference_widgets import ThumbnailLabel

APP=QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
subject.load_font(APP)


def flush():
    APP.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)


def until(predicate,seconds=5):
    deadline=time.monotonic()+seconds
    while not predicate() and time.monotonic()<deadline:
        flush();QTest.qWait(5)
    if not predicate():raise AssertionError('Native workspace fixture did not settle')


class Screen:
    def availableGeometry(self):return QRect(0,0,1920,1080)
    def devicePixelRatio(self):return 1


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-native-workspace-')
        self.base=Path(self.temp.name);self.patches=ExitStack();self.events=[]
        self.patches.enter_context(patch.object(app_settings,'_startup_runtime_allowed',return_value=False))
        self.patches.enter_context(patch.object(subject.QGuiApplication,'screenAt',return_value=Screen()))
        self.patches.enter_context(patch.object(subject.QGuiApplication,'primaryScreen',return_value=Screen()))
        self.patches.enter_context(patch.object(subject,'raise_shelf_for_reveal',return_value=False))
        self.settings=app_settings.SettingsStore(self.base/'profile')
        self.settings.update(theme='dark',capture_mode='reference',capture_dir=str(self.base/'saved'),view_mode='list')
        ui_theme.set_theme('dark');ui_theme.apply_application(APP,subject.FONT_FAMILY)
        self.manager=SimpleNamespace(motion=False,settings=self.settings,save=lambda:None,update_handles=lambda:None,
            activate_shelf=lambda _:None,_quitting=False,outgoing_drag=False,
            show_lan_dialog=lambda:self.events.append('lan'),show_settings=lambda:self.events.append('settings'))
        self.shelf=subject.ShelfWindow(self.manager,1);self.manager.shelf=self.shelf
        self.shelf.pinned=True;self.shelf._show_at(QPoint(60,60),'manual',QPoint());flush()
        self.paths=[]
        for index,name in enumerate(('report.txt','assets.dat','specification.pdf')):
            path=self.base/name;path.write_bytes(f'original {index}'.encode());self.paths.append(str(path))
        self.shelf.add_paths('permanent',self.paths);flush()

    def tearDown(self):
        if self.shelf.archive_job:self.shelf.archive_job.cancel()
        until(lambda:self.shelf.archive_job is None and not self.shelf.copy_jobs and not self.shelf._delete_jobs)
        for timer in (self.shelf._transfer_timer,self.shelf._image_timer,self.shelf._intake_timer):timer.stop()
        if self.shelf.popup:self.shelf.popup.preview.media.release_source()
        self.shelf.resolver.close();self.shelf.image_cache.close()
        self.shelf.presentation.stop();self.shelf.geometry_morph.stop()
        self.shelf.hide();self.shelf.deleteLater();flush();self.patches.close();self.temp.cleanup()

    def overview(self):
        self.shelf.show_contents();flush();popup=self.shelf.popup;popup.resume_content()
        until(lambda:popup._build_cursor==len(popup.visible_refs()) and popup.retirement.is_idle)
        return popup

    def bounds(self,widget,owner):
        return QRect(widget.mapTo(owner,QPoint()),widget.size())

    def test_reference_sizes_left_navigation_and_complete_button_bounds(self):
        self.assertEqual((self.shelf.card.width(),self.shelf.card.height()),(332,318))
        popup=self.overview()
        self.assertEqual((self.shelf.card.width(),self.shelf.card.height()),(1040,600))
        self.assertEqual((popup.width(),popup.height()),(1040,600))
        self.assertEqual(popup.sidebar.width(),210);self.assertEqual(popup.preview.width(),340)
        self.assertLess(popup.sidebar.geometry().right(),popup.middle.geometry().left())
        self.assertLess(popup.middle.geometry().right(),popup.preview.geometry().left())
        popup.lan_nav.click();popup.settings_nav.click();self.assertEqual(self.events,['lan','settings'])
        for control in (popup.zip_button,popup.zip_all_button,popup.clear_button,popup.view_list,popup.view_grid,
                        popup.lan_nav,popup.settings_nav,popup.preview.copy_button,popup.preview.open_button,
                        popup.preview.rename_button,*popup.category_buttons.values()):
            self.assertTrue(control.isVisible(),control.text())
            self.assertTrue(popup.rect().contains(self.bounds(control,popup)),control.text())
            text_width=control.fontMetrics().horizontalAdvance(control.text())
            icon_width=control.iconSize().width()+4 if not control.icon().isNull() else 0
            self.assertGreaterEqual(control.width(),text_width+icon_width+10,control.text())
            self.assertGreaterEqual(control.height(),control.fontMetrics().height()+4,control.text())
        for control in (popup.lan_nav,popup.settings_nav):
            image=control.grab().toImage();ratio=image.devicePixelRatio()
            # The text after the 16px icon must start at the left inset,
            # rather than the center of the 190px navigation row.
            painted=0
            for y in range(round(8*ratio),round((control.height()-7)*ratio)):
                for x in range(round(34*ratio),round(60*ratio)):
                    color=image.pixelColor(x,y)
                    if color.alpha()>180 and min(color.red(),color.green(),color.blue())>110:painted+=1
            self.assertGreater(painted,8,control.text()+' is not left aligned')
        self.assertTrue(popup.preview.stack.rect().isValid())
        self.assertTrue(popup.preview.rect().contains(self.bounds(popup.preview.stack,popup.preview)))
        if os.environ.get('FLICKDOCK_CAPTURE_WORKSPACE'):
            output=Path(__file__).resolve().parents[2]/'outputs'
            self.shelf.grab().save(str(output/'html-native-workspace-tested.png'))
        self.shelf.collapse_contents();flush()
        self.assertEqual((self.shelf.card.width(),self.shelf.card.height()),(332,318))

    def test_card_stroke_occupies_all_four_edges_without_clipping(self):
        popup=self.overview();row=next(row for row in popup.rows_body.findChildren(subject.FileRow) if row.path==self.paths[1])
        popup.selected.clear();row.update();flush()
        image=row.grab().toImage();ratio=image.devicePixelRatio();w,h=image.width(),image.height()
        points=((w//2,0),(w//2,h-1),(0,h//2),(w-1,h//2))
        inner=((w//2,round(2*ratio)),(w//2,h-1-round(2*ratio)),(round(2*ratio),h//2),(w-1-round(2*ratio),h//2))
        for edge,inside in zip(points,inner):
            outside=image.pixelColor(*edge);surface=image.pixelColor(*inside)
            self.assertGreater(outside.alpha(),0)
            self.assertNotEqual(outside.rgba(),surface.rgba(),(edge,inside,'card border clipped'))

    def test_cloud_status_points_follow_live_sync_provider_for_each_state(self):
        state={'value':'local'}
        self.manager.sync=SimpleNamespace(sync_status_for_ref=lambda _:state['value'],
            sync_statuses_for_refs=lambda refs:{ref:state['value'] for ref in refs},is_upload_paused=lambda _:False)
        popup=self.overview();ref=self.paths[1];popup.select_item(ref)
        row=next(row for row in popup.rows_body.findChildren(subject.FileRow) if row.path==ref)
        expected={'synced':'#30D158','uploading':'#0A84FF','pending':'#64D2FF',
                  'local':'#A1A1AA','paused':'#FF9F0A','error':'#FF453A'}
        for status,hex_color in expected.items():
            state['value']=status;popup.refresh_cloud_status();flush()
            self.assertEqual(row.icon_label.cloud_status,status)
            self.assertEqual(popup.preview.cloud_badge.status,status)
            for widget,position in ((row.icon_label,(34,34)),(popup.preview.cloud_badge,(12,12))):
                image=widget.grab().toImage();ratio=image.devicePixelRatio();actual=image.pixelColor(round(position[0]*ratio),round(position[1]*ratio))
                expected_color=QColor(hex_color)
                self.assertLessEqual(max(abs(a-b) for a,b in zip(actual.getRgb()[:3],expected_color.getRgb()[:3])),3,status)
        popup.preview.show_group((self.paths[0],self.paths[1]));self.assertFalse(popup.preview.cloud_badge.isVisible())
        for enabled,hex_color in ((True,'#30D158'),(False,'#FF453A')):
            self.settings.update(sync_upload_enabled=enabled);flush()
            image=popup.cloud_selector.grab().toImage();ratio=image.devicePixelRatio()
            actual=image.pixelColor(round(15*ratio),round(14*ratio))
            self.assertEqual(actual.name(),hex_color.lower())

    def test_multiselect_exports_actual_selected_only_zip_and_original_menus_remain_reachable(self):
        popup=self.overview();popup.select_item(self.paths[0]);popup.select_item(self.paths[1],toggle=True);flush()
        self.assertEqual(set(popup.chosen_refs()),set(self.paths[:2]))
        self.assertEqual(tuple(popup.preview._group_refs),tuple(popup.chosen_refs()))
        destination=self.base/'selected-only.zip'
        with patch.object(self.shelf,'choose_zip_path',return_value=str(destination)):
            popup.zip_button.click()
            until(lambda:self.shelf.archive_job is None and destination.is_file())
        with zipfile.ZipFile(destination) as archive:
            self.assertEqual(set(archive.namelist()),{'report.txt','assets.dat'})
            self.assertEqual(archive.read('report.txt'),b'original 0')
        self.assertTrue(all(Path(path).is_file() for path in self.paths))
        single=[]
        with patch.object(subject.AnimatedMenu,'exec',lambda menu,*_:single.extend(action.text() for action in menu.actions() if not action.isSeparator())):
            popup.selected.clear();popup.select_item(self.paths[0]);popup.show_item_menu(self.paths[0],QPoint(0,0))
        for text in ('打开文件','在文件夹中显示','复制文件路径','复制所选内容','重命名文件…','移动所选文件到…','从搁架移除此项'):
            self.assertIn(text,single)
        page=[]
        with patch.object(subject.AnimatedMenu,'exec',lambda menu,*_:page.extend(action.text() for action in menu.actions() if not action.isSeparator())):
            self.shelf.show_menu(pos=QPoint(0,0))
        self.assertTrue(any('添加文件' in text for text in page));self.assertTrue(any('打包全部' in text for text in page))
        self.assertFalse(any('重命名' in text for text in page))


if __name__=='__main__':unittest.main(verbosity=2)
