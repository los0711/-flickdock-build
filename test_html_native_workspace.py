"""Native reference workspace geometry, status, menus and real ZIP workflow.

Qt runs offscreen with disposable files, no global hooks, network or registry.
"""
import file_preview
import overview_ui
import shelf_ui_common
import shelf_window
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

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QRect, Qt
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
shelf_ui_common.load_font(APP)


def flush():
    APP.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)


def until(predicate,seconds=5):
    deadline=time.monotonic()+seconds
    while not predicate() and time.monotonic()<deadline:
        flush();QTest.qWait(5)
        # QTest's C++ wait can retain the Python GIL. Give disk/import workers
        # an explicit turn, as QApplication.exec does in the running app.
        time.sleep(.001)
    if not predicate():raise AssertionError('Native workspace fixture did not settle')


def contrast_colors(first,second):
    def luminance(color):
        values=[channel/255 for channel in color.getRgb()[:3]]
        values=[value/12.92 if value<=.04045 else ((value+.055)/1.055)**2.4 for value in values]
        return sum(value*weight for value,weight in zip(values,(.2126,.7152,.0722)))
    low,high=sorted((luminance(first),luminance(second)))
    return (high+.05)/(low+.05)


def contrast_pixel(image,rect,background):
    rect=rect.intersected(image.rect())
    return max((image.pixelColor(x,y) for x in range(rect.left(),rect.right()+1)
                for y in range(rect.top(),rect.bottom()+1)),
               key=lambda color:contrast_colors(color,background))


class Screen:
    def availableGeometry(self):return QRect(0,0,1920,1080)
    def devicePixelRatio(self):return 1


from PySide6.QtCore import QObject, Signal

class SyncView(QObject):
    storage_changed=Signal(object)
    def __init__(self,**values):
        super().__init__()
        self.storage=None;self.state={};self.cloud_copy_operations=set()
        self.service=SimpleNamespace(signed_in=False)
        self.__dict__.update(values)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-native-workspace-')
        self.base=Path(self.temp.name).resolve();self.patches=ExitStack();self.events=[]
        self.patches.enter_context(patch.object(app_settings,'_startup_runtime_allowed',return_value=False))
        self.patches.enter_context(patch.object(subject.QGuiApplication,'screenAt',return_value=Screen()))
        self.patches.enter_context(patch.object(subject.QGuiApplication,'primaryScreen',return_value=Screen()))
        self.patches.enter_context(patch.object(shelf_window,'raise_shelf_for_reveal',return_value=False))
        self.settings=app_settings.SettingsStore(self.base/'profile')
        self.settings.update(theme='dark',capture_mode='reference',capture_dir=str(self.base/'saved'),view_mode='list')
        ui_theme.set_theme('dark');ui_theme.apply_application(APP,shelf_ui_common.FONT_FAMILY)
        self.manager=SimpleNamespace(motion=False,settings=self.settings,save=lambda:None,update_handles=lambda:None,
            activate_shelf=lambda _:None,_quitting=False,outgoing_drag=False,
            show_lan_dialog=lambda:self.events.append('lan'),show_transfer_dialog=lambda:self.events.append('lan'),
            queue_transfer_paths=lambda mode,paths:self.events.append((mode,paths)),show_settings=lambda:self.events.append('settings'))
        self.shelf=shelf_window.ShelfWindow(self.manager,1);self.manager.shelf=self.shelf
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
        self.assertTrue(popup.preview.empty_preview.isVisible())
        self.assertTrue(popup.preview.action_footer.isHidden())
        popup.select_item(self.paths[0]);flush()
        self.assertEqual(int(popup.preview.zip_button.font().weight()),400)
        self.assertEqual(int(popup.preview.send_button.font().weight()),400)
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
        popup=self.overview();row=next(row for row in popup.rows_body.findChildren(overview_ui.FileRow) if row.path==self.paths[1])
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
        self.manager.sync=SyncView(upload_time_for_ref=lambda _:None,sync_status_for_ref=lambda _:state['value'],
            sync_statuses_for_refs=lambda refs:{ref:state['value'] for ref in refs},is_upload_paused=lambda _:False)
        popup=self.overview();ref=self.paths[1];popup.select_item(ref)
        row=next(row for row in popup.rows_body.findChildren(overview_ui.FileRow) if row.path==ref)
        renderings=[]
        for status in ('synced','uploading','pending','local','paused','error'):
            state['value']=status;popup.refresh_cloud_status();flush()
            self.assertEqual(row.icon_label.cloud_status,status)
            self.assertEqual(popup.preview.status_value.text(),file_preview.file_presence_label(status))
            image=row.icon_label.grab().toImage()
            renderings.append(bytes(image.constBits()))
            # Tooltip and preview retain the exact live state even when local
            # and uploading items intentionally share the same blue dot.
            self.assertGreater(sum(image.pixelColor(x,y).alpha()>0 for x in range(8,24) for y in range(8,24)),8,status)
        self.assertEqual(renderings[1],renderings[2]);self.assertEqual(renderings[2],renderings[3])
        self.assertEqual(len(set(renderings)),2)
        popup.preview.show_group((self.paths[0],self.paths[1]));self.assertEqual(popup.preview.status_value.text(),'')
    def test_compact_and_expanded_trailing_controls_keep_identical_offsets_and_full_menu(self):
        controls=(self.shelf.pin_button,self.shelf.expand_button,self.shelf.menu_button,self.shelf.close_button)
        def positions():
            card=self.shelf.card.geometry()
            return [(card.right()+1-button.x(),button.y()-card.y()) for button in controls]
        compact=positions()
        logo_center=self.shelf.brand_logo.mapTo(self.shelf,self.shelf.brand_logo.rect().center()).y()
        self.assertTrue(all(abs(control.geometry().center().y()-logo_center)<=1 for control in controls))
        popup=self.overview();expanded=positions()
        self.assertEqual([item[0] for item in compact],[item[0] for item in expanded])
        logo_center=popup.brand_logo.mapTo(self.shelf,popup.brand_logo.rect().center()).y()
        self.assertTrue(all(abs(control.geometry().center().y()-logo_center)<=1 for control in controls))
        self.assertTrue(self.shelf.menu_button.isVisible())
        self.assertLess(self.bounds(self.shelf.popup.clear_button,self.shelf.popup).right()+6,
                        self.bounds(self.shelf.pin_button,self.shelf.popup).left())
        self.shelf.menu_button.click();flush()
        menu=self.shelf._more_menu
        texts=[action.text() for action in menu.actions() if not action.isSeparator()]
        menu.close();flush()
        for expected in ('添加文件','设置','收起到屏幕边缘'):
            self.assertTrue(any(expected in value for value in texts),(expected,texts))
        self.shelf.collapse_contents();self.shelf.presentation_kind='drag';self.shelf._layout_header(shelf_ui_common.CARD_W)
        for control in (self.shelf.account_button,self.shelf.account_caption,self.shelf.lan_caption):
            self.assertFalse(control.isVisible())
        for control in (self.shelf.lan_button,):
            self.assertTrue(control.isVisible())
        for control in controls:self.assertFalse(control.isVisible())
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
        self.assertIsNone(popup.primary_selection)
        self.assertEqual(popup.search_edit.text(),'');self.assertEqual(popup.scroll.verticalScrollBar().value(),0)
        self.assertIsNone(popup.preview.current_path)
        self.assertIs(popup.preview.stack.currentWidget(),popup.preview.empty_preview)
        self.assertEqual(self.settings.view_mode,'icons');self.assertTrue(popup.view_grid.isChecked())

    def test_sidebar_count_capsules_center_one_two_and_three_digits(self):
        popup=self.overview();badge=popup.nav_counts['all'];control=popup.category_buttons['all']
        for text in ('1','12','128'):
            badge.setText(text);popup._position_nav_counts();flush()
            self.assertEqual(badge.alignment(),Qt.AlignCenter)
            self.assertGreaterEqual(badge.width(),badge.height())
            self.assertGreaterEqual(badge.width(),badge.fontMetrics().horizontalAdvance(text)+12)
            self.assertEqual(control.width()-badge.geometry().right()-1,12)
            image=badge.grab().toImage();foreground=[]
            for x in range(3,image.width()-3):
                for y in range(3,image.height()-3):
                    color=image.pixelColor(x,y)
                    if color.alpha()>180:foreground.append(x)
            self.assertTrue(foreground,text)
            self.assertLessEqual(abs((min(foreground)+max(foreground))/2-(image.width()-1)/2),3,text)

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
            self.settings.update(theme=theme);ui_theme.set_theme(theme);ui_theme.apply_application(APP,shelf_ui_common.FONT_FAMILY)
            refresh_tree(self.shelf);popup._render_signature=None;popup.rebuild();popup.set_category('all');flush()
            selected=popup.category_buttons['all'];image=selected.parentWidget().grab(selected.geometry()).toImage()
            background=image.pixelColor(6,selected.height()//2)
            check('selected category',contrast_pixel(image,QRect(35,8,45,18),background),background)
            badge=popup.nav_counts['all'];position=badge.geometry()
            background=image.pixelColor(position.center().x(),position.top()+3)
            check('selected count',contrast_pixel(image,position.adjusted(2,3,-2,-3),background),background)
            other=popup.category_buttons['uploading'];other_image=popup.grab().toImage();other_badge=popup.nav_counts['uploading'];position=other_badge.geometry()
            check('unselected transfer count',other_badge.palette().color(QPalette.WindowText),
                  other_image.pixelColor(other.mapTo(popup,QPoint(position.center().x(),position.top()+3))))
            # Exercise real QMenu selection drawing; palette() alone does not
            # resolve the QSS :selected foreground/background state.
            menu=shelf_ui_common.AnimatedMenu();shelf_ui_common.style_menu(menu);action=menu.addAction('选择菜单项目')
            menu.popup(QPoint(0,0));menu.setActiveAction(action);flush();rect=menu.actionGeometry(action);image=menu.grab().toImage()
            background=image.pixelColor(rect.right()-12,rect.center().y())
            check('selected menu item',contrast_pixel(image,QRect(rect.x()+14,rect.y()+4,85,rect.height()-8),background),background)
            menu.close();menu.deleteLater();flush()
            unselected_mode=popup.view_grid;image=popup.grab().toImage()
            check('unselected view toggle',unselected_mode.palette().color(QPalette.ButtonText),
                  image.pixelColor(popup.view_segment.mapTo(popup,QPoint(unselected_mode.geometry().center().x(),3))))
            popup.update_transfer_status();popup.preview.show_path(ref,force=True);flush()
            self.assertEqual(popup.preview.cancel_transfer.height(),26)
            self.assertLess(popup.preview.cancel_transfer.font().pixelSize(),popup.preview.open_button.font().pixelSize())
            with patch.object(self.shelf,'cancel_transfer') as cancel:
                popup.preview.cancel_transfer.click();cancel.assert_called_once_with(ref)
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

    def test_transfer_status_stays_in_the_clipboard_footer_without_resizing_content(self):
        popup=self.overview();ref=self.paths[0]
        for theme in ('light','dark'):
            self.settings.update(theme=theme);ui_theme.set_theme(theme);ui_theme.apply_application(APP,shelf_ui_common.FONT_FAMILY)
            from reference_widgets import refresh_tree
            refresh_tree(self.shelf)
            for width in (1040,800,600,420):
                popup.resize(width,560);popup._apply_responsive_layout();flush()
                self.shelf.content.transfers.clear();popup.update_transfer_status();flush()
                footer=QRect(popup.clipboard_footer.geometry());scroll=QRect(popup.scroll.geometry())
                self.shelf.content.transfers[ref]=dict(phase='uploading',received=25,total=100)
                popup.update_transfer_status();flush()
                self.assertEqual(popup.clipboard_footer.geometry(),footer,(theme,width))
                self.assertEqual(popup.scroll.geometry(),scroll,(theme,width))
                self.assertIs(popup.transfer_strip.parentWidget(),popup.clipboard_footer)
                self.assertTrue(popup.transfer_strip.isVisible())
                self.assertLess(popup.clipboard_button.geometry().right(),popup.transfer_strip.geometry().left())
                self.assertTrue(popup.clipboard_footer.rect().contains(popup.transfer_strip.geometry()),(theme,width))
                self.assertTrue(popup.strip_rows[0][2].isVisible())
                self.assertEqual(popup.strip_rows[0][2].text(),'25%')
                self.assertFalse(hasattr(popup,'strip_signal'))
                self.assertLess(popup.strip_rows[0][1].geometry().right(),popup.strip_rows[0][2].geometry().left())
                self.assertAlmostEqual(popup.transfer_strip.progress_fraction,.25)
                self.assertGreaterEqual(popup.strip_rows[0][2].width(),popup.strip_rows[0][2].fontMetrics().horizontalAdvance('100%'))
                centers=[widget.geometry().center().y() for widget in
                         (popup.strip_rows[0][2],popup.strip_rows[0][1])]
                self.assertLessEqual(max(centers)-min(centers),1,(theme,width,centers))
                self.shelf.content.transfers[ref]=dict(phase='uploading',received=0,total=0)
                popup.update_transfer_status();flush()
                self.assertFalse(popup.strip_rows[0][2].isVisible())
                self.shelf.content.transfers.clear();popup.update_transfer_status();flush()
                self.assertFalse(popup.transfer_strip.isVisible());self.assertEqual(popup.scroll.geometry(),scroll)
        self.shelf.content.transfers[ref]=dict(phase='uploading',received=25,total=100)
        popup.set_category('all');popup.update_transfer_status();flush()
        popup.strip_rows[0][1].click();flush()
        self.assertEqual(popup.category,'uploading')
        self.assertTrue(popup.transfer_strip.isVisible())

    def test_brand_title_is_smaller_bolder_and_optically_centered_on_logo(self):
        import sys
        from PySide6.QtGui import QFontMetricsF
        from reference_widgets import optical_text_baseline
        popup=self.overview();title=popup.title;logo=popup.brand_logo
        self.assertEqual(int(title.font().weight()),600)
        self.assertEqual(title.font().pixelSize(),15 if sys.platform=='win32' else 14)
        self.assertEqual(title.font().letterSpacing(),0)
        if sys.platform=='win32':self.assertEqual(title.font().family(),'Microsoft YaHei')
        ink=QFontMetricsF(title.font()).tightBoundingRect(title.text())
        text_y=title.y()+optical_text_baseline(title.font(),title.text(),title.height())+ink.center().y()
        self.assertLessEqual(abs(text_y-(logo.y()+logo.height()/2)),.5)

    def test_overview_has_the_same_visible_outer_outline_in_both_themes(self):
        from reference_widgets import refresh_tree
        popup=self.overview();outline=popup.window_outline
        self.assertTrue(outline.testAttribute(Qt.WA_TransparentForMouseEvents))
        for theme in ('light','dark'):
            self.settings.update(theme=theme);ui_theme.set_theme(theme);ui_theme.apply_application(APP,shelf_ui_common.FONT_FAMILY)
            refresh_tree(self.shelf);outline.show();flush()
            shown=popup.grab().toImage();outline.hide();flush();hidden=popup.grab().toImage()
            for x,y in ((0,150),(popup.width()-1,150),(popup.width()//2,0),(popup.width()//2,popup.height()-1)):
                self.assertNotEqual(shown.pixelColor(x,y).rgba(),hidden.pixelColor(x,y).rgba(),(theme,x,y))
            outline.show();flush()

    def test_adaptive_layout_preserves_content_preview_and_bulk_actions(self):
        popup=self.overview()
        self.assertGreaterEqual(popup.preview.stack.height(),176)
        for width,height in ((1040,600),(800,560),(600,520),(420,500)):
            popup.resize(width,height);flush();popup._apply_responsive_layout();flush()
            self.assertTrue(popup.rect().contains(self.bounds(popup.header,popup)))
            self.assertTrue(popup.rect().contains(self.bounds(popup.scroll,popup)))
            self.assertGreaterEqual(popup.scroll.width(),200)
            for control in (popup.search_edit,popup.view_segment):
                if control.isVisible():self.assertTrue(popup.selection_actions.rect().contains(self.bounds(control,popup.selection_actions)),(width,control.objectName(),getattr(control,'text',lambda:'')()))
            for control in (popup.sort_segment,popup.zip_button,popup.zip_all_button,popup.clear_button,popup.bulk_more,popup.select_all_button):
                if control.isVisible():self.assertTrue(popup.selection_bar.rect().contains(self.bounds(control,popup.selection_bar)),(width,control.accessibleName()))
            if width>=780:
                self.assertTrue(popup.sidebar.isVisible());self.assertTrue(popup.preview.isVisible())
                self.assertLess(popup.middle.geometry().right(),popup.preview.geometry().left())
            else:
                self.assertFalse(popup.sidebar.isVisible());self.assertTrue(popup.category_picker.isVisible())
                self.assertTrue(popup.select_all_button.isVisible());self.assertFalse(popup.preview.isVisible())
                popup.toggle_narrow_preview();flush()
                self.assertTrue(popup.preview.isVisible());self.assertFalse(popup.scroll.isVisible())
                self.assertTrue(popup.preview.rect().contains(self.bounds(popup.preview.open_button,popup.preview)))
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

    def test_card_view_keeps_width_and_column_positions_during_add_remove(self):
        from content_grid import GRID_COLUMN_WIDTH_PX
        extra=[]
        for index in range(12):
            path=self.base/f'grid-card-{index}.txt';path.write_text('fixture',encoding='utf8');extra.append(str(path))
        refs=[*self.paths,*extra]
        popup=self.overview();popup.change_view_mode('icons');popup.resume_content();flush()
        self.assertEqual(popup.view_grid.toolTip(),'卡片视图')
        self.assertEqual(popup.view_grid.accessibleName(),'卡片视图')
        anchor=self.paths[0];widths=[]
        class WidthObserver(QObject):
            def eventFilter(self,watched,event):
                if event.type()==QEvent.Resize:widths.append(event.size().width())
                return False
        observer=WidthObserver(popup);watched=None
        for window_width in (1040,1028,900,800,600):
            popup.resize(window_width,560);flush();popup._apply_responsive_layout();flush()
            columns=[];cell_widths=[]
            for count in (1,2,3,4,8,15,7,3,1):
                self.shelf.files['permanent'][:]=refs[:count]
                popup.rebuild();until(lambda:popup._build_cursor==len(popup.visible_refs()) and popup.retirement.is_idle)
                QTest.qWait(30);flush()
                card=popup._item_widgets[anchor]
                if watched is None:watched=card;card.installEventFilter(observer)
                self.assertIs(card,watched)
                columns.append(popup._grid_columns)
                cell_widths.append(card.width())
                self.assertGreaterEqual(card.width(),GRID_COLUMN_WIDTH_PX)
                self.assertTrue(all(item.width()==card.width() for item in popup._item_widgets.values()))
                column=popup.visible_refs().index(anchor)%popup._grid_columns
                left=card.x()-column*(card.width()+8)
                right=left+popup._grid_columns*(card.width()+8)-8
                self.assertLessEqual(abs(left-(popup.rows_body.width()-right)),2,
                    (window_width,count,card.geometry().getRect(),popup.rows_body.width(),popup._grid_columns))
            self.assertEqual(len(set(columns)),1,(window_width,columns))
            self.assertEqual(len(set(cell_widths)),1,(window_width,cell_widths))
        self.assertTrue(all(width>=GRID_COLUMN_WIDTH_PX for width in widths),widths)

    def test_category_and_view_changes_animate_visible_files_without_changing_geometry(self):
        popup=self.overview();self.shelf.manager.motion=True
        with patch('ui_transitions.motion_allowed',return_value=True),patch('native_ui_common.reduced_motion',return_value=False):
            popup.change_view_mode('icons');until(lambda:popup._build_cursor==len(popup.visible_refs()))
            flush();transition=popup.item_transitions
            effect=transition.entry_effect;self.assertTrue(effect.isEnabled());self.assertIsNotNone(transition.entry_started)
            widgets=list(popup._item_widgets.values());geometry={w:QRect(w.geometry()) for w in widgets}
            self.assertIs(effect,popup.scroll.viewport().graphicsEffect())
            self.assertTrue(all(widget.graphicsEffect() is None for widget in widgets))
            self.assertLess(effect.progress,1.)
            transition.entry_started=time.perf_counter()-.1;transition._tick()
            self.assertGreater(effect.progress,0.)
            self.assertEqual({w:QRect(w.geometry()) for w in widgets},geometry)
            popup.set_category('text');until(lambda:popup._build_cursor==len(popup.visible_refs()))
            flush()
            self.assertIs(transition.entry_effect,effect)
            self.assertTrue(effect.isEnabled());self.assertIsNotNone(transition.entry_started)
            # A direct scroll or wheel must immediately settle the whole group.
            transition._scrolled();self.assertFalse(effect.isEnabled());self.assertIsNone(transition.entry_started)
            transition.finish()

    def test_file_entrance_moves_all_painted_items_by_the_same_small_offset(self):
        from PySide6.QtWidgets import QWidget
        from item_transitions import _EntryEffect
        host=QWidget();host.resize(120,120);host.setStyleSheet('background:#000000')
        surface=QWidget(host);surface.setGeometry(10,10,100,100)
        squares=[]
        for y in (10,55):
            square=QWidget(surface);square.setGeometry(10,y,25,16);square.setStyleSheet('background:#ff0000');square.show();squares.append(square)
        effect=_EntryEffect(surface);surface.setGraphicsEffect(effect);host.show();flush()
        try:
            geometry=[QRect(square.geometry()) for square in squares];positions=[]
            for progress in (0,.5,1):
                effect.set_progress(progress);flush();image=host.grab().toImage()
                red=[y for y in range(120) if image.pixelColor(25,y).red()>60 and image.pixelColor(25,y).green()<10]
                positions.append((min(red),max(red)))
                self.assertEqual([square.geometry() for square in squares],geometry)
            self.assertEqual(positions,[(24,84),(22,82),(20,80)])
            squares[0].setStyleSheet('background:#00ff00');flush();image=host.grab().toImage()
            self.assertGreater(image.pixelColor(25,25).green(),200)
        finally:host.close();host.deleteLater();flush()

    def test_thumbnail_cache_is_bounded_and_invalidates_when_content_changes(self):
        from reference_widgets import thumbnail,invalidate_thumbnail
        ref=self.paths[0];first=thumbnail(self.shelf,ref,40);second=thumbnail(self.shelf,ref,40)
        self.assertEqual(first.cacheKey(),second.cacheKey())
        invalidate_thumbnail(self.shelf,ref);fresh=thumbnail(self.shelf,ref,40)
        self.assertNotEqual(first.cacheKey(),fresh.cacheKey())
        for index in range(260):thumbnail(self.shelf,str(self.base/f'cache-{index}.dat'),12)
        self.assertLessEqual(len(self.shelf._reference_thumbnail_cache),256)

    def test_search_width_eases_between_empty_and_populated_categories_and_reverses(self):
        popup=self.overview();self.shelf.manager.motion=True
        with patch('ui_transitions.motion_allowed',return_value=True):
            APP.processEvents();original=popup.search_edit.width()
            popup.set_category('audio');APP.processEvents();motion=popup.preview_width_motion
            self.assertTrue(motion.active);motion.animation.setCurrentTime(100);APP.processEvents()
            middle=popup.search_edit.width();self.assertGreater(middle,original)
            popup.set_category('all')
            self.assertTrue(motion.active)
            self.assertLessEqual(abs(motion.animation.startValue()-popup.preview.width()),1)
            APP.processEvents()
            motion.animation.setCurrentTime(motion.animation.duration());APP.processEvents()
            self.assertFalse(motion.active)
            self.assertLessEqual(abs(popup.search_edit.width()-original),3)

    def test_category_motion_defers_offscreen_rows_and_resumes_after_animation(self):
        extra=[]
        for index in range(80):
            path=self.base/f'motion-{index}.dat';path.write_bytes(b'x');extra.append(str(path))
        self.shelf.add_paths('permanent',extra);flush();popup=self.overview();self.shelf.manager.motion=True
        with patch('ui_transitions.motion_allowed',return_value=True),patch('native_ui_common.reduced_motion',return_value=False):
            popup.set_category('audio');flush()
            motion=popup.preview_width_motion
            if motion.active:motion.animation.setCurrentTime(motion.animation.duration())
            popup.sidebar._selection_animation.setCurrentTime(popup.sidebar._selection_animation.duration());flush()
            popup.set_category('all');flush()
            until(lambda:not popup._file_entry_pending)
            self.assertTrue(popup._interactive_motion_active())
            self.assertLess(popup._build_cursor,len(popup._build_refs))
            self.assertFalse(popup._build_timer.isActive())
            self.assertTrue(popup.retirement._paused)
            until(lambda:popup._build_cursor==len(popup._build_refs) and popup.retirement.is_idle,seconds=5)
            self.assertFalse(popup.retirement._paused)

    def test_card_view_label_is_consistent_in_toolbar_and_settings(self):
        popup=self.overview()
        self.assertEqual(popup.view_grid.toolTip(),'卡片视图')
        from app_settings import SettingsDialog
        dialog=SettingsDialog(self.settings)
        try:
            self.assertEqual(dialog.view_combo.itemText(dialog.view_combo.findData('icons')),'卡片视图')
            self.assertTrue(any(button.text()=='卡片视图' for button in dialog.findChildren(QPushButton)))
        finally:dialog.deleteLater();flush()

    def test_list_row_omits_inline_copy_but_keeps_remove_and_keyboard_copy(self):
        popup=self.overview();row=popup._item_widgets[self.paths[0]]
        self.assertIsInstance(row,overview_ui.FileRow)
        self.assertFalse(hasattr(row,'copy_button'))
        APP.sendEvent(row,QEvent(QEvent.Enter));self.assertTrue(row.remove_button.isVisible())
        popup.select_item(self.paths[0])
        with patch.object(self.shelf,'copy_items') as copy:
            QTest.keyClick(row,Qt.Key_C,Qt.ControlModifier)
            copy.assert_called_once_with([self.paths[0]])
        self.assertEqual(popup.preview.copy_button.glyph,'copy')

    def test_navigation_text_remains_visible_while_selection_background_moves(self):
        popup=self.overview();self.shelf.manager.motion=True
        with patch('ui_transitions.motion_allowed',return_value=True):
            popup.set_category('uploading');animation=popup.sidebar._selection_animation
            animation.pause();animation.setCurrentTime(animation.duration()//2);flush()
            button=popup.category_buttons['uploading'];image=popup.sidebar.grab().toImage();ratio=image.devicePixelRatio()
            x=button.x()+32;y=button.y()+5
            pixels=[image.pixelColor(round(px*ratio),round(py*ratio))
                    for px in range(x,min(x+80,button.x()+button.width())) for py in range(y,y+24)]
            self.assertTrue(any(min(c.red(),c.green(),c.blue())>90 for c in pixels),
                            'selected text must stay legible while the marker is away')

    def test_empty_drop_stays_centered_while_preview_width_animates(self):
        popup=self.overview();self.shelf.manager.motion=True
        with patch('ui_transitions.motion_allowed',return_value=True):
            for theme in ('light','dark'):
                self.settings.update(theme=theme);ui_theme.set_theme(theme);ui_theme.apply_application(APP,shelf_ui_common.FONT_FAMILY)
                for width in (1040,800,600,420):
                    popup.set_category('all');popup.resize(width,560);popup._apply_responsive_layout();flush()
                    popup.set_category('uploading');flush()
                    motion=popup.preview_width_motion;entrance=popup.empty_entrance
                    for step in (0,40,100,180,200):
                        if motion.active:motion.animation.setCurrentTime(step)
                        if entrance.active:entrance.animation.setCurrentTime(min(step,entrance.animation.duration()))
                        flush()
                        area=popup.empty_state.rect();drop=popup.empty_drop.geometry()
                        self.assertLessEqual(abs(drop.center().x()-area.center().x()),1,(theme,width,step,area,drop))
                        self.assertLessEqual(abs(drop.center().y()-area.center().y()),1,(theme,width,step,area,drop))
                    self.assertFalse(popup.preview.isVisible())

    def test_card_columns_stay_stable_when_preview_reappears(self):
        popup=self.overview();self.shelf.manager.motion=True
        with patch('ui_transitions.motion_allowed',return_value=True):
            popup.change_view_mode('icons');until(lambda:popup._build_cursor==len(popup.visible_refs()))
            popup.set_category('uploading');flush();motion=popup.preview_width_motion
            if motion.active:motion.animation.setCurrentTime(motion.animation.duration());flush()
            popup.set_category('all');flush()
            self.assertTrue(motion.active)
            columns=[]
            for step in (0,40,80,120,160,200):
                if motion.active:motion.animation.setCurrentTime(step)
                flush();columns.append(popup._grid_columns)
            self.assertEqual(len(set(columns)),1,columns)
            self.assertEqual(columns[-1],popup._grid_column_count())

    def test_selected_grid_card_and_checkbox_remain_legible_in_both_themes(self):
        popup=self.overview();popup.change_view_mode('icons');popup.resume_content()
        until(lambda:popup._build_cursor==len(popup.visible_refs()))
        item=next(item for item in popup.rows_body.findChildren(IconItem) if item.path==self.paths[0])
        for theme in ('light','dark'):
            self.settings.update(theme=theme);ui_theme.set_theme(theme);ui_theme.apply_application(APP,shelf_ui_common.FONT_FAMILY)
            popup.select_item(item.ref);flush();item.update();flush()
            image=item.grab().toImage();ratio=image.devicePixelRatio()
            sample=lambda x,y:image.pixelColor(round(x*ratio),round(y*ratio))
            background=sample(5,55)
            from native_ui_common import colors
            self.assertEqual(background.name(),QColor(colors(theme)['file_card_selected']).name())
            self.assertEqual(sample(14,15).name(),QColor(colors(theme)['action']).name())
            title=contrast_pixel(image,__import__('PySide6.QtCore',fromlist=['QRect']).QRect(round(12*ratio),round(68*ratio),round((item.width()-24)*ratio),round(18*ratio)),background)
            self.assertGreaterEqual(contrast_colors(title,background),4.5)
            self.assertIsNone(item.remove_button._glyph_tint)
            self.assertFalse(item.remove_button.property('inverseOnSelection'))
        zone=self.shelf.zones['permanent']
        self.assertTrue(zone.count_arrow.isHidden())

    def test_empty_state_disabled_actions_and_search_uses_actual_titles(self):
        popup=self.overview();popup.search_edit.setText('specification');flush()
        self.assertEqual(popup.visible_refs(),[self.paths[2]])
        popup.search_edit.clear();flush();self.assertEqual(len(popup.visible_refs()),3)
        self.shelf.files['permanent'].clear();self.shelf.refresh();popup.rebuild();flush()
        self.assertTrue(popup.empty_state.isVisible())
        self.assertFalse(popup.zip_button.isEnabled());self.assertFalse(popup.zip_all_button.isEnabled());self.assertFalse(popup.clear_button.isEnabled())

    def test_multiselect_preview_keeps_divider_white_surface_and_compact_actions(self):
        popup=self.overview()
        for theme in ('light','dark'):
            self.settings.update(theme=theme);ui_theme.set_theme(theme);ui_theme.apply_application(APP,shelf_ui_common.FONT_FAMILY)
            self.shelf._tree_theme=None;self.shelf.refresh();flush()
            popup.selected.clear();popup.select_item(self.paths[0]);popup.select_item(self.paths[1],toggle=True);flush()
            preview=popup.preview;group=preview.group_view
            self.assertTrue(group.isVisible());self.assertEqual(group.geometry().left(),1)
            self.assertEqual(group.width(),preview.width()-1)
            self.assertEqual(group.palette().window().color().name(),QColor(ui_theme.color('background')).name())
            controls=(preview.group_copy,preview.group_zip,preview.group_cloud_row,preview.group_send_control,preview.group_remove)
            gaps=[b.geometry().top()-a.geometry().bottom()-1 for a,b in zip(controls,controls[1:])]
            self.assertEqual(gaps,[8,8,8,8])

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
        with patch.object(shelf_ui_common.AnimatedMenu,'exec',lambda menu,*_:single.extend(action.text() for action in menu.actions() if not action.isSeparator())):
            popup.selected.clear();popup.select_item(self.paths[0]);popup.show_item_menu(self.paths[0],QPoint(0,0))
        for text in ('打开文件','在文件夹中显示','复制文件路径','复制所选内容','重命名文件…','移动所选文件到…','从搁架移除此项'):
            self.assertIn(text,single)
        self.shelf.show_menu(pos=QPoint(0,0));flush()
        menu=self.shelf._more_menu
        page=[action.text() for action in menu.actions() if not action.isSeparator()]
        menu.close();flush()
        self.assertTrue(any('添加文件' in text for text in page));self.assertTrue(any('打包全部' in text for text in page))
        self.assertFalse(any('重命名' in text for text in page))

    def test_deselect_all_clears_group_preview_and_only_selected_item_can_be_copied(self):
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
        self.assertIsNone(popup.preview.current_path)
        self.assertIs(popup.preview.stack.currentWidget(),popup.preview.empty_preview)
        self.assertFalse(popup.zip_button.isEnabled())
        self.assertEqual(popup.preview.copy_button.glyph,'copy')
        copied=[]
        with patch.object(self.shelf,'copy_items',side_effect=lambda refs:copied.append(tuple(refs))):
            popup.preview.copy_button.click();flush()
            self.assertEqual(copied,[])
            popup.select_item(self.paths[2]);popup.preview.copy_button.click();flush()
        self.assertEqual(copied,[(popup.preview.current_path,)])


if __name__=='__main__':unittest.main(verbosity=2)
