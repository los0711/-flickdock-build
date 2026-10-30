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
from PySide6.QtGui import QColor, QFocusEvent, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

import app_settings
import flickdock as subject
import ui_theme
from reference_widgets import ThumbnailLabel, palette
from content_grid import IconItem

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


def contrast_colors(first,second):
    def luminance(color):
        values=[channel/255 for channel in color.getRgb()[:3]]
        values=[value/12.92 if value<=.04045 else ((value+.055)/1.055)**2.4 for value in values]
        return sum(value*weight for value,weight in zip(values,(.2126,.7152,.0722)))
    low,high=sorted((luminance(first),luminance(second)))
    return (high+.05)/(low+.05)


def brightest_pixel(image,rect):
    rect=rect.intersected(image.rect())
    return max((image.pixelColor(x,y) for x in range(rect.left(),rect.right()+1)
                for y in range(rect.top(),rect.bottom()+1)),
               key=lambda color:sum(color.getRgb()[:3]))


class Screen:
    def availableGeometry(self):return QRect(0,0,1920,1080)
    def devicePixelRatio(self):return 1


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-native-workspace-')
        self.base=Path(self.temp.name).resolve();self.patches=ExitStack();self.events=[]
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
        renderings=[]
        for status in ('synced','uploading','pending','local','paused','error'):
            state['value']=status;popup.refresh_cloud_status();flush()
            self.assertEqual(row.icon_label.cloud_status,status)
            self.assertEqual(popup.preview.cloud_badge.status,status)
            image=popup.preview.cloud_badge.grab().toImage()
            renderings.append(bytes(image.constBits()))
            self.assertEqual(popup.preview.cloud_badge.accessibleName(),subject.CLOUD_STATUS_LABELS[status])
            # Real glyph artwork must be present in the circle, not just a
            # color-only dot; every state is distinguishable without color.
            self.assertGreater(sum(image.pixelColor(x,y).alpha()>0 for x in range(8,24) for y in range(8,24)),8,status)
        self.assertEqual(len(set(renderings)),6)
        popup.preview.show_group((self.paths[0],self.paths[1]));self.assertFalse(popup.preview.cloud_badge.isVisible())
        for enabled,hex_color in ((True,palette()['green']),(False,palette()['red'])):
            self.settings.update(sync_upload_enabled=enabled);flush()
            image=popup.cloud_selector.grab().toImage();ratio=image.devicePixelRatio()
            target=QColor(hex_color)
            matching=sum(max(abs(a-b) for a,b in zip(image.pixelColor(x,y).getRgb()[:3],target.getRgb()[:3]))<12
                for x in range(round(4*ratio),round(22*ratio)) for y in range(round(7*ratio),round(25*ratio)))
            self.assertGreater(matching,5)

    def test_compact_and_expanded_trailing_controls_keep_identical_offsets_and_full_menu(self):
        controls=(self.shelf.pin_button,self.shelf.expand_button,self.shelf.menu_button,self.shelf.close_button)
        def positions():
            card=self.shelf.card.geometry()
            return [(card.right()+1-button.x(),button.y()-card.y()) for button in controls]
        compact=positions();self.overview();expanded=positions()
        self.assertEqual(compact,expanded)
        self.assertTrue(self.shelf.menu_button.isVisible())
        self.assertLess(self.bounds(self.shelf.popup.clear_button,self.shelf.popup).right()+6,
                        self.bounds(self.shelf.pin_button,self.shelf.popup).left())
        texts=[]
        with patch.object(subject.AnimatedMenu,'exec',lambda menu,*_:texts.extend(action.text() for action in menu.actions() if not action.isSeparator())):
            self.shelf.menu_button.click()
        for expected in ('添加文件','设置','收起到屏幕边缘'):
            self.assertTrue(any(expected in value for value in texts),(expected,texts))
        self.shelf.collapse_contents();self.shelf.presentation_kind='drag';self.shelf._layout_header(subject.CARD_W)
        for control in (self.shelf.account_button,self.shelf.lan_button,self.shelf.account_caption,self.shelf.lan_caption,self.shelf.footer_line):
            self.assertFalse(control.isVisible())
        self.assertTrue(self.shelf.zones['permanent'].caption.isVisible())

    def test_view_mode_only_settings_do_not_apply_global_services(self):
        popup=self.overview();self.manager._applied_settings=dict(self.settings._data)
        self.manager.reset_drag_gesture=lambda:(_ for _ in ()).throw(AssertionError('view switch reset drag'))
        self.settings.update(view_mode='icons')
        with patch.object(subject.ui_theme,'apply_application') as apply_theme:
            subject.ShelfManager.apply_settings(self.manager)
        apply_theme.assert_not_called()
        self.assertTrue(popup.view_grid.isChecked());self.assertFalse(popup.view_list.isChecked())
        self.assertEqual(self.settings.view_mode,'icons')
        until(lambda:popup._build_cursor==len(popup.visible_refs()))
        self.assertTrue(popup.rows_body.findChildren(IconItem))

    def test_reopen_resets_navigation_selection_search_scroll_and_preview_but_keeps_saved_mode(self):
        popup=self.overview();popup.change_view_mode('icons');popup.select_item(self.paths[0]);popup.select_item(self.paths[1],toggle=True)
        popup.search_edit.setText('report');popup.set_category('text');flush()
        self.shelf.collapse_contents();self.shelf.show_contents();flush()
        self.assertEqual(popup.category,'all');self.assertFalse(popup.selected)
        self.assertIsNone(popup.primary_selection);self.assertIsNone(popup.hovered_ref)
        self.assertEqual(popup.search_edit.text(),'');self.assertEqual(popup.scroll.verticalScrollBar().value(),0)
        self.assertEqual(popup.preview.current_path,popup.visible_refs()[0])
        self.assertEqual(self.settings.view_mode,'icons');self.assertTrue(popup.view_grid.isChecked())

    def test_sidebar_count_capsules_center_one_two_and_three_digits(self):
        popup=self.overview();badge=popup.nav_counts['all'];control=popup.category_buttons['all']
        for text in ('1','12','128'):
            badge.setText(text);popup._position_nav_counts();flush()
            self.assertEqual(badge.alignment(),Qt.AlignCenter)
            self.assertGreaterEqual(badge.width(),24)
            self.assertGreaterEqual(badge.width(),badge.fontMetrics().horizontalAdvance(text)+12)
            self.assertEqual(control.width()-badge.geometry().right()-1,12)
            image=badge.grab().toImage();white=[]
            for x in range(image.width()):
                for y in range(image.height()):
                    color=image.pixelColor(x,y)
                    if min(color.red(),color.green(),color.blue())>220 and color.alpha()>180:white.append(x)
            self.assertTrue(white,text)
            self.assertLessEqual(abs((min(white)+max(white))/2-(image.width()-1)/2),3,text)

    def test_actual_sidebar_badge_menu_toolbar_and_transfer_text_contrast(self):
        from reference_widgets import refresh_tree
        ratios=[];popup=self.overview();ref=self.paths[0]
        self.shelf.content.transfers[ref]=dict(phase='uploading',received=25,total=100)
        def check(name,foreground,background):
            ratio=contrast_colors(foreground,background)
            ratios.append(dict(theme=ui_theme.current_theme(),component=name,
                foreground=foreground.name(),background=background.name(),ratio=round(ratio,3)))
            self.assertGreaterEqual(ratio,4.5,(name,foreground.name(),background.name(),ratio))
        for theme in ('light','dark'):
            self.settings.update(theme=theme);ui_theme.set_theme(theme);ui_theme.apply_application(APP,subject.FONT_FAMILY)
            refresh_tree(self.shelf);popup._render_signature=None;popup.rebuild();popup.set_category('all');flush()
            selected=popup.category_buttons['all'];image=selected.grab().toImage()
            check('selected category',brightest_pixel(image,QRect(35,8,45,18)),image.pixelColor(6,selected.height()//2))
            badge=popup.nav_counts['all'];position=badge.geometry()
            check('selected count',brightest_pixel(image,position.adjusted(2,3,-2,-3)),
                  image.pixelColor(position.center().x(),position.top()+3))
            other=popup.category_buttons['uploading'];other_image=popup.grab().toImage();other_badge=popup.nav_counts['uploading'];position=other_badge.geometry()
            check('unselected transfer count',other_badge.palette().color(QPalette.WindowText),
                  other_image.pixelColor(other.mapTo(popup,QPoint(position.center().x(),position.top()+3))))
            # Exercise real QMenu selection drawing; palette() alone does not
            # resolve the QSS :selected foreground/background state.
            menu=subject.AnimatedMenu();subject.style_menu(menu);action=menu.addAction('选择菜单项目')
            menu.popup(QPoint(0,0));menu.setActiveAction(action);flush();rect=menu.actionGeometry(action);image=menu.grab().toImage()
            check('selected menu item',brightest_pixel(image,QRect(rect.x()+14,rect.y()+4,85,rect.height()-8)),
                  image.pixelColor(rect.right()-12,rect.center().y()))
            menu.close();menu.deleteLater();flush()
            unselected_mode=popup.view_grid;image=popup.grab().toImage()
            check('unselected view toggle',unselected_mode.palette().color(QPalette.ButtonText),
                  image.pixelColor(popup.view_segment.mapTo(popup,QPoint(unselected_mode.geometry().center().x(),3))))
            popup.update_transfer_status();popup.preview.show_path(ref,force=True);flush()
            for name,label in (('footer percent',popup.strip_rows[0][2]),('preview percent',popup.preview.transfer_meta)):
                self.assertTrue(label.isVisible(),name)
                image=popup.grab().toImage();position=label.mapTo(popup,QPoint())
                check(name,label.palette().color(QPalette.WindowText),image.pixelColor(position+QPoint(1,1)))
            popup.set_category('uploading');flush();label=popup.transfer_eta
            self.assertTrue(label.isVisible());image=popup.grab().toImage();position=label.mapTo(popup,QPoint())
            check('transfer summary percent',label.palette().color(QPalette.WindowText),image.pixelColor(position+QPoint(1,1)))
        if os.environ.get('FLICKDOCK_CAPTURE_WORKSPACE'):
            import json
            output=Path(__file__).resolve().parents[2]/'outputs/hig-redesign/component-contrast-QA.json'
            output.write_text(json.dumps(ratios,ensure_ascii=False,indent=2),encoding='utf8')

    def test_adaptive_layout_preserves_content_preview_and_bulk_actions(self):
        popup=self.overview()
        self.assertGreaterEqual(popup.preview.stack.height(),280)
        for width,height in ((1040,600),(800,560),(600,520),(420,500)):
            popup.resize(width,height);flush();popup._apply_responsive_layout();flush()
            self.assertTrue(popup.rect().contains(self.bounds(popup.header,popup)))
            self.assertTrue(popup.rect().contains(self.bounds(popup.scroll,popup)))
            self.assertGreaterEqual(popup.scroll.width(),200)
            for control in (popup.search_edit,popup.cloud_selector,popup.view_segment,popup.zip_button,popup.zip_all_button,popup.clear_button,popup.bulk_more):
                if control.isVisible():self.assertTrue(popup.header.rect().contains(self.bounds(control,popup.header)),(width,control.objectName(),getattr(control,'text',lambda:'')()))
            if width>=780:
                self.assertTrue(popup.sidebar.isVisible());self.assertTrue(popup.preview.isVisible())
                self.assertLess(popup.middle.geometry().right(),popup.preview.geometry().left())
            else:
                self.assertFalse(popup.sidebar.isVisible());self.assertTrue(popup.category_picker.isVisible())
                self.assertTrue(popup.bulk_more.isVisible());self.assertFalse(popup.preview.isVisible())
                popup.toggle_narrow_preview();flush()
                self.assertTrue(popup.preview.isVisible());self.assertFalse(popup.scroll.isVisible())
                self.assertTrue(popup.rect().contains(self.bounds(popup.preview.open_button,popup)))
                popup.toggle_narrow_preview();flush()

    def test_grid_cells_fit_scroll_area_and_keyboard_selection_has_visible_feedback(self):
        popup=self.overview();popup.change_view_mode('icons');flush();popup.resume_content()
        until(lambda:popup._build_cursor==len(popup.visible_refs()))
        for width in (1040,800,600):
            popup.resize(width,560);flush();popup._apply_responsive_layout();popup.resume_content();flush()
            until(lambda:popup._build_cursor==len(popup.visible_refs()))
            for item in popup.rows_body.findChildren(IconItem):
                if not item.isVisible():continue
                self.assertTrue(popup.rows_body.rect().contains(item.geometry()),(width,item.geometry()))
                self.assertTrue(item.rect().contains(item.remove_button.geometry()))
                self.assertEqual(item.remove_button.size().toTuple(),(32,32))
        item=next(item for item in popup.rows_body.findChildren(IconItem) if item.isVisible())
        popup.select_item(item.ref);QTest.keyClick(item,Qt.Key_Space);flush()
        self.assertNotIn(item.ref,popup.selected)
        QTest.keyClick(item,Qt.Key_Space);flush();self.assertIn(item.ref,popup.selected)
        APP.sendEvent(item,QFocusEvent(QEvent.FocusIn,Qt.TabFocusReason));flush()
        self.assertTrue(item._keyboard_focus);self.assertTrue(item.remove_button.isVisible())
        APP.sendEvent(item,QFocusEvent(QEvent.FocusOut,Qt.MouseFocusReason));flush()
        self.assertFalse(item._keyboard_focus)

    def test_empty_state_disabled_actions_and_search_uses_actual_titles(self):
        popup=self.overview();popup.search_edit.setText('specification');flush()
        self.assertEqual(popup.visible_refs(),[self.paths[2]])
        popup.search_edit.clear();flush();self.assertEqual(len(popup.visible_refs()),3)
        self.shelf.files['permanent'].clear();self.shelf.refresh();popup.rebuild();flush()
        self.assertTrue(popup.empty_state.isVisible())
        self.assertFalse(popup.zip_button.isEnabled());self.assertFalse(popup.zip_all_button.isEnabled());self.assertFalse(popup.clear_button.isEnabled())

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

    def test_deselect_all_retires_group_preview_and_copies_current_single_item(self):
        popup=self.overview()
        popup.toggle_select_all_visible();flush()
        self.assertEqual(set(popup.selected),set(self.paths))
        self.assertEqual(tuple(popup.preview._group_refs),tuple(popup.chosen_refs()))
        self.assertTrue(popup.preview.group_view.isVisible())
        popup.toggle_select_all_visible();flush()
        self.assertEqual(popup.selected,set())
        self.assertEqual(popup.chosen_refs(),[])
        self.assertFalse(popup.preview._group_refs)
        self.assertFalse(popup.preview.group_view.isVisible())
        self.assertIn(popup.preview.current_path,self.paths)
        self.assertFalse(popup.zip_button.isEnabled())
        self.assertEqual(popup.preview.copy_button.glyph,'copy')
        copied=[]
        with patch.object(self.shelf,'copy_items',side_effect=lambda refs:copied.append(tuple(refs))):
            popup.preview.copy_button.click();flush()
        self.assertEqual(copied,[(popup.preview.current_path,)])


if __name__=='__main__':unittest.main(verbosity=2)
