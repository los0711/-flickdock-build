"""Overview toolbar geometry and deletion through the real offscreen entry."""
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile

from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QStyle,QStyleOptionButton

import test_html_native_workspace as fixture
import ui_theme
from confirm_clear import confirmation_active
from reference_widgets import SegmentedControl, refresh_tree
from flickdock import ShelfManager


class OverviewControlsTests(unittest.TestCase):
    setUp=fixture.WorkspaceTests.setUp
    tearDown=fixture.WorkspaceTests.tearDown
    overview=fixture.WorkspaceTests.overview

    @staticmethod
    def bounds(widget,owner):
        return QRect(widget.mapTo(owner,QPoint()),widget.size())

    def test_shared_segments_active_state_and_smaller_file_details(self):
        popup=self.overview()
        self.assertIsInstance(popup.sort_segment,SegmentedControl)
        self.assertIsInstance(popup.view_segment,SegmentedControl)
        self.assertEqual(popup.sort_segment.height(),popup.view_segment.height())
        for key in ('time','size'):
            for _ in range(3):
                popup.sort_buttons[key].click();fixture.flush()
                controls=popup.sort_buttons.values()
                self.assertEqual(sum(control.isChecked() for control in controls),bool(popup.sort_direction))
        row=popup._item_widgets[self.paths[0]]
        self.assertLess(row.detail_label.font().pixelSize(),row.name_label.font().pixelSize())
        self.assertLess(popup.sort_segment.width(),180)
        self.assertEqual(set(popup.sort_buttons),{'time','size'})

    def test_sort_returns_to_unselected_background_after_third_click(self):
        popup=self.overview()
        for theme in ('dark','light'):
            ui_theme.set_theme(theme);self.settings.update(theme=theme)
            for key in ('time','size'):
                popup.reset_sort();fixture.flush()
                for _ in range(3):
                    QTest.mouseClick(popup.sort_buttons[key],Qt.LeftButton)
                    fixture.flush()
                self.assertEqual((popup.sort_key,popup.sort_direction),(None,0))
                self.assertFalse(any(button.isChecked() for button in popup.sort_buttons.values()))
                self.assertIsNone(popup.sort_segment._selection_button)
                self.assertTrue(popup.sort_segment._selection_rect.isEmpty())

    def test_mac_top_edge_first_expansion_keeps_anchor(self):
        from types import SimpleNamespace
        import shelf_window
        area=fixture.Screen().availableGeometry()
        compact=QRect(area.right()-359,area.top(),360,345)
        with patch.object(shelf_window,'sys',SimpleNamespace(platform='darwin')):
            expanded=self.shelf._expansion_rect(compact)
        self.assertEqual(expanded.top(),compact.top())
        self.assertTrue(area.contains(expanded))

    def test_list_row_press_scales_and_rebounds_without_moving_layout(self):
        from overview_ui import _RowPressEffect
        popup=self.overview();row=popup._item_widgets[self.paths[0]]
        row.motion.motion=True;geometry=QRect(row.geometry())
        point=QPoint(row.width()//2,row.height()//2)
        QTest.mousePress(row,Qt.LeftButton,pos=point);QTest.qWait(75)
        self.assertIsInstance(row.graphicsEffect(),_RowPressEffect)
        self.assertGreater(row.motion.values['down'],.1)
        self.assertEqual(row.geometry(),geometry)
        QTest.mouseRelease(row,Qt.LeftButton,pos=point);QTest.qWait(400)
        self.assertAlmostEqual(row.motion.values['down'],0,places=2)
        self.assertEqual(row.geometry(),geometry)
        self.assertIn(self.paths[0],popup.selected)

    def test_row_press_pixels_scale_about_center_without_shifting(self):
        from types import SimpleNamespace
        from PySide6.QtWidgets import QWidget
        from overview_ui import _RowPressEffect
        parent=QWidget();parent.resize(400,140)
        row=QWidget(parent);row.setGeometry(40,30,320,64)
        row.setStyleSheet('background:#e12378');row.motion=SimpleNamespace(values={'down':1.})
        row.setGraphicsEffect(_RowPressEffect(row));parent.show();fixture.flush()
        try:
            image=parent.grab().toImage();ratio=image.devicePixelRatio()
            y=round((row.y()+row.height()/2)*ratio)
            pixels=[x for x in range(image.width()) if image.pixelColor(x,y).name()=='#e12378']
            self.assertTrue(pixels)
            left,right=min(pixels)/ratio,max(pixels)/ratio
            self.assertGreater(left,row.x()+2);self.assertLess(right,row.x()+row.width()-3)
            self.assertAlmostEqual((left+right)/2,row.x()+row.width()/2,delta=1.5)
            self.assertEqual(row.geometry(),QRect(40,30,320,64))
        finally:parent.close();parent.deleteLater();fixture.flush()

    def test_sort_arrows_contrast_with_idle_in_both_themes(self):
        from reference_widgets import SORT_ARROW_GAP_PX,SORT_ARROW_SIZE_PX
        popup=self.overview()
        for theme,idle,active in (('light','#A3A3A3','#000000'),('dark','#737373','#FAFAFA')):
            ui_theme.set_theme(theme);self.settings.update(theme=theme)
            self.shelf._tree_theme=None;self.shelf.refresh();fixture.flush()
            popup.reset_sort()
            for key in ('time','size'):
                control=popup.sort_buttons[key]
                popup.reset_sort();QTest.qWait(160)
                self.assertEqual(control.arrow_colors(),(idle,idle))
                for direction in (1,2):
                    popup.cycle_sort(key);QTest.qWait(180);fixture.flush()
                    self.assertEqual(control.arrow_colors(),(active,idle) if direction==1 else (idle,active))
                    image=control.grab().toImage();ratio=image.devicePixelRatio()
                    width=control.fontMetrics().horizontalAdvance(control.text())
                    left=(control.width()-width-SORT_ARROW_GAP_PX-SORT_ARROW_SIZE_PX)/2+width+SORT_ARROW_GAP_PX
                    top=(control.height()-SORT_ARROW_SIZE_PX)/2
                    pixel=image.pixelColor(round((left+SORT_ARROW_SIZE_PX/2)*ratio),round((top+(2 if direction==1 else 8))*ratio))
                    expected=QColor(active)
                    self.assertLess(sum(abs(a-b) for a,b in zip(pixel.getRgb()[:3],expected.getRgb()[:3])),75,
                                    (theme,key,direction,pixel.name()))

    def test_rename_sheet_is_topmost_and_uses_the_existing_file_operation(self):
        popup=self.overview();ref=self.paths[0]
        self.shelf.rename_ref(ref);fixture.flush();dialog=self.shelf._rename_dialog
        self.assertTrue(dialog.windowFlags()&Qt.WindowStaysOnTopHint)
        self.assertEqual(dialog.windowModality(),Qt.WindowModal)
        self.assertTrue(self.shelf.rename_edit.hasFocus())
        dialog.reject();fixture.flush();self.assertEqual(self.shelf.files['permanent'],self.paths)

    def test_rename_field_wraps_grows_scrolls_and_preserves_the_extension(self):
        from PySide6.QtCore import QMimeData
        from PySide6.QtGui import QInputMethodEvent
        from reference_widgets import RENAME_EDIT_MAX_LINES,RENAME_EDIT_MIN_HEIGHT_PX,RENAME_EDIT_PADDING_Y_PX,RENAME_EDIT_BORDER_PX
        self.overview()
        source=self.base/('图像😀测试_'+'abcde'*15+'.png');source.write_bytes(b'original')
        self.shelf.add_paths('permanent',[str(source)])
        self.shelf.rename_ref(str(source))
        dialog=self.shelf._rename_dialog;edit=self.shelf.rename_edit
        initial_height=dialog.height();self.assertGreater(initial_height,252)
        QTest.qWait(60);fixture.flush();self.assertEqual(dialog.height(),initial_height)
        self.assertEqual(edit.textCursor().selectedText(),source.stem)
        self.assertEqual(edit.toPlainText(),source.name)
        edit.setPlainText('short.png');QTest.qWait(60);fixture.flush()
        original_height=dialog.height()
        edit.setPlainText('a'*80+'.png');QTest.qWait(60);fixture.flush()
        self.assertGreater(edit.height(),RENAME_EDIT_MIN_HEIGHT_PX)
        self.assertEqual(edit.verticalScrollBar().maximum(),0)
        self.assertGreater(dialog.height(),original_height)
        text='图'*250+'.png';edit.setPlainText(text);QTest.qWait(60);fixture.flush()
        limit=edit.fontMetrics().lineSpacing()*RENAME_EDIT_MAX_LINES+2*(RENAME_EDIT_PADDING_Y_PX+RENAME_EDIT_BORDER_PX)
        self.assertLessEqual(edit.height(),limit)
        self.assertGreater(edit.verticalScrollBar().maximum(),0)
        self.assertEqual(edit.toPlainText(),text)
        edit.setPlainText('');mime=QMimeData();mime.setText('name\r\nmore\u2028text')
        edit.insertFromMimeData(mime);self.assertEqual(edit.toPlainText(),'name more text')
        QApplication.sendEvent(edit,QInputMethodEvent('测试',[]))
        QTest.keyClick(edit,Qt.Key_Return);fixture.flush()
        self.assertTrue(dialog.isVisible());self.assertTrue(source.exists())
        QApplication.sendEvent(edit,QInputMethodEvent('',[]))
        edit.setPlainText('图像😀_renamed.webp');QTest.qWait(60);fixture.flush()
        self.assertEqual(edit.height(),RENAME_EDIT_MIN_HEIGHT_PX)
        QTest.keyClick(edit,Qt.Key_Return);fixture.flush()
        target=source.with_name('图像😀_renamed.webp')
        self.assertEqual(target.read_bytes(),b'original');self.assertFalse(source.exists())
        self.assertFalse(dialog.isVisible())

    def test_remaining_toolbar_offsets_match_compact_header(self):
        compact={key:self.shelf.card.geometry().right()-self.bounds(control,self.shelf).right()
                 for key,control in (('pin',self.shelf.pin_button),('expand',self.shelf.expand_button),('menu',self.shelf.menu_button),('close',self.shelf.close_button))}
        popup=self.overview()
        for key,control in (('pin',self.shelf.pin_button),('expand',self.shelf.expand_button),('menu',self.shelf.menu_button),('close',self.shelf.close_button)):
            self.assertEqual(self.shelf.card.geometry().right()-control.geometry().right(),compact[key])

    def test_compact_entry_prewarms_overview_without_showing_or_mutating_content(self):
        self.shelf.hide();self.settings.update(trigger_view='compact')
        before=list(self.shelf.files['permanent'])
        ShelfManager.warm_overview(self.manager);fixture.flush()
        self.assertIsNotNone(self.shelf.popup);self.assertFalse(self.shelf.popup.isVisible())
        self.assertEqual(self.shelf.files['permanent'],before)

    def test_repeated_animated_switch_keeps_widgets_and_interrupted_target(self):
        popup=self.overview();widgets=dict(popup._item_widgets)
        self.shelf.geometry_morph.motion=True;self.manager.motion=True
        for _ in range(3):
            self.shelf.collapse_contents();self.shelf.geometry_morph.animation.setCurrentTime(80)
            self.shelf.show_contents();self.shelf.geometry_morph.animation.setCurrentTime(80)
            self.shelf.collapse_contents();self.shelf.geometry_morph.complete();fixture.flush()
            self.assertFalse(self.shelf.expanded);self.assertFalse(self.shelf.content_transition_active())
            self.shelf.show_contents();self.shelf.geometry_morph.complete();fixture.flush()
            self.assertTrue(self.shelf.expanded);self.assertTrue(popup.isVisible())
            self.assertEqual(popup._item_widgets,widgets)
        self.assertFalse(self.shelf._surface_snapshot.active)

    def test_header_and_action_alignment_at_all_supported_widths_in_both_themes(self):
        popup=self.overview()
        for theme in ('dark','light'):
            self.settings.update(theme=theme);ui_theme.set_theme(theme);refresh_tree(self.shelf)
            for width in (1280,1040,980,900,800,780,760,620,600,420,330):
                popup.resize(width,600);fixture.flush();popup._apply_responsive_layout();fixture.flush()
                for owner,controls in ((popup.selection_actions,(popup.search_edit,popup.sort_segment,popup.view_segment,popup.zip_button,popup.zip_all_button,popup.clear_button,popup.bulk_more,popup.select_all_button)),):
                    shown=[control for control in controls if control.isVisible()]
                    for control in shown:self.assertTrue(owner.rect().contains(self.bounds(control,owner)),(theme,width,control.accessibleName()))
                    for index,first in enumerate(shown):
                        for second in shown[index+1:]:
                            self.assertFalse(self.bounds(first,owner).intersects(self.bounds(second,owner)),(theme,width,first.accessibleName(),second.accessibleName()))
                self.assertEqual(popup.search_edit.geometry().center().y(),popup.view_segment.geometry().center().y())
                self.assertTrue(popup.select_all_button.isVisible())
                self.assertEqual(self.bounds(popup.select_all_button,popup.selection_actions).right(),popup.selection_actions.width()-13)
                if popup.sort_segment.isVisible():self.assertEqual(popup.sort_segment.x()-popup.search_edit.geometry().right()-1,6)
                self.assertLess(self.bounds(popup.search_edit,popup).right(),self.bounds(popup.view_segment,popup).left())

    def choose_delete(self,action):
        errors=[]
        def choose():
            dialog=self.shelf._clear_confirmation
            try:
                self.assertTrue(confirmation_active(self.shelf))
                self.assertEqual([b.text() for b in (dialog.selected_button,dialog.delete_button,dialog.cancel_button)],['删除已选','删除全部','取消'])
                for button in (dialog.selected_button,dialog.delete_button,dialog.cancel_button):
                    self.assertTrue(dialog.panel.rect().contains(self.bounds(button,dialog.panel)))
                action(dialog)
            except BaseException as error:
                errors.append(error);dialog.cancel()
        QTimer.singleShot(0,choose)
        self.shelf.popup.clear_button.click();fixture.flush()
        if errors:raise errors[0]
        self.assertFalse(confirmation_active(self.shelf))

    def test_delete_selected_removes_only_selected_records_and_preserves_originals(self):
        popup=self.overview();popup.select_item(self.paths[1])
        self.choose_delete(lambda dialog:QTest.mouseClick(dialog.selected_button,Qt.LeftButton))
        self.assertEqual(self.shelf.files['permanent'],[self.paths[0],self.paths[2]])
        self.assertTrue(all(Path(path).is_file() for path in self.paths))

    def test_delete_confirmation_uses_saturated_readable_red_in_both_themes(self):
        from confirm_clear import DeleteConfirmOverlay
        for theme in ('light','dark'):
            dialog=DeleteConfirmOverlay(self.shelf.card,3,1,theme=theme)
            try:
                style=dialog.delete_button.styleSheet()
                self.assertIn('background:#DC2626;color:#FFFFFF',style)
                self.assertIn('QPushButton:hover {background:#B91C1C;',style)
                self.assertIn('QPushButton:pressed {background:#991B1B;',style)
                for background in ('#DC2626','#B91C1C','#991B1B'):
                    self.assertGreaterEqual(fixture.contrast_colors(QColor(background),QColor('#FFFFFF')),4.5)
            finally:dialog.deleteLater()

    def test_delete_all_ignores_search_filter_and_preserves_originals(self):
        popup=self.overview();popup.search_edit.setText('report');fixture.flush()
        self.choose_delete(lambda dialog:QTest.mouseClick(dialog.delete_button,Qt.LeftButton))
        self.assertEqual(self.shelf.files['permanent'],[])
        self.assertTrue(all(Path(path).is_file() for path in self.paths))

    def test_cancel_and_escape_preserve_contents_and_selection(self):
        popup=self.overview();popup.select_item(self.paths[0])
        for action in (lambda dialog:dialog.cancel_button.click(),lambda dialog:QTest.keyClick(dialog.selected_button,Qt.Key_Escape)):
            self.choose_delete(action)
            self.assertEqual(self.shelf.files['permanent'],self.paths)
            self.assertEqual(popup.selected,{self.paths[0]})

    def test_no_selection_disables_selected_deletion(self):
        self.overview()
        def choose(dialog):
            self.assertFalse(dialog.selected_button.isEnabled())
            dialog.selected_button.click();self.assertTrue(dialog.active)
            dialog.cancel_button.click()
        self.choose_delete(choose)
        self.assertEqual(self.shelf.files['permanent'],self.paths)

    def test_selection_snapshot_does_not_delete_new_or_changed_selection(self):
        popup=self.overview();popup.select_item(self.paths[0])
        new=self.base/'new-arrival.txt';new.write_text('new original',encoding='utf8')
        def choose(dialog):
            self.shelf.remove_path('permanent',self.paths[0])
            self.shelf.add_paths('permanent',[str(new)])
            popup.select_item(self.paths[1])
            dialog.selected_button.click()
        self.choose_delete(choose)
        self.assertEqual(self.shelf.files['permanent'],[*self.paths[1:],str(new)])

    def test_preview_actions_follow_reference_and_remain_reachable_when_short(self):
        popup=self.overview();preview=popup.preview
        popup.select_item(self.paths[0]);fixture.flush()
        self.assertEqual([button.accessibleName() for button in preview.action_buttons],
                         ['打开文件','重命名','复制路径','打包 ZIP','发送文件','长按移出搁架'])
        self.assertEqual(preview.open_button.text(),'打开')
        self.assertEqual(preview.send_button.text(),'发送文件')
        for control in (preview.rename_button,preview.zip_button,preview.remove_button):
            self.assertEqual(control.text(),'');self.assertEqual(control.width(),control.height())
            self.assertTrue(control.toolTip())
        self.assertEqual(preview.status_card.layout().contentsMargins().bottom(),5)
        self.assertEqual(preview.size_card.layout().contentsMargins().bottom(),5)
        fixed_sizes=None;measurements=[]
        for width,height in ((1040,600),(800,560),(600,520),(420,500)):
            popup.resize(width,height);fixture.flush();popup._apply_responsive_layout();fixture.flush()
            if popup._narrow_layout:popup.toggle_narrow_preview();fixture.flush()
            preview.content_scroll.ensureWidgetVisible(preview.copy_path_button,0,0);fixture.flush()
            self.assertTrue(preview.content_scroll.viewport().rect().contains(self.bounds(preview.copy_path_button,preview.content_scroll.viewport())))
            controls=(preview.remove_button,preview.zip_button,preview.rename_button,preview.open_button,preview.send_control)
            bounds=[self.bounds(control,preview.action_footer) for control in controls]
            for control,rect in zip(controls,bounds):
                self.assertTrue(preview.action_footer.rect().contains(rect),(width,control.accessibleName(),rect))
                self.assertEqual(rect.height(),bounds[0].height())
            self.assertTrue(all(a.right()<b.left() for a,b in zip(bounds,bounds[1:])))
            self.assertEqual(bounds[0].left(),16)
            self.assertEqual(preview.action_footer.width()-bounds[-1].right()-1,16)
            self.assertEqual(bounds[3].left()-bounds[2].right()-1,6)
            self.assertEqual(bounds[4].left()-bounds[3].right()-1,6)
            self.assertEqual(bounds[3].width(),68)
            self.assertEqual(bounds[4].height(),popup.clipboard_button.height())
            self.assertEqual(bounds[4].height(),popup.capacity_button.height())
            if width==1040:
                card=self.bounds(preview.path_card,preview)
                footer=self.bounds(preview.action_footer,preview)
                self.assertEqual(footer.left()+bounds[0].left(),card.left())
                self.assertEqual(footer.left()+bounds[-1].right(),card.right())
            sizes=tuple((rect.width(),rect.height()) for rect in bounds[:-1])
            if fixed_sizes is None:fixed_sizes=sizes
            self.assertEqual(sizes,fixed_sizes,'Only the send control may resize')
            measurements.append((preview.action_footer.width(),bounds[-1].width()))
            if popup._narrow_layout:popup.toggle_narrow_preview();fixture.flush()
        for previous,current in zip(sorted(measurements),sorted(measurements)[1:]):
            if previous[0]<current[0]:self.assertLess(previous[1],current[1])

    def test_preview_copy_path_send_and_remove_use_current_item_only(self):
        popup=self.overview();preview=popup.preview;ref=self.paths[1]
        popup.select_item(ref);preview.copy_path_button.click()
        self.assertFalse(preview.copy_path_button.icon().isNull())
        self.assertEqual(preview.copy_path_button.iconSize().width(),12)
        self.assertLess(preview.copy_path_button.font().pixelSize(),preview.open_button.font().pixelSize())
        self.assertEqual(QApplication.clipboard().text(),str(Path(ref).absolute()))
        preview.send_button.click()
        self.assertEqual(self.events[-1],('lan',[str(Path(ref).absolute())]))
        QTest.mousePress(preview.remove_button,Qt.LeftButton);QTest.qWait(800)
        QTest.mouseRelease(preview.remove_button,Qt.LeftButton);fixture.flush()
        self.assertEqual(self.shelf.files['permanent'],[self.paths[0],self.paths[2]])
        self.assertTrue(Path(ref).is_file())

    def test_removal_requires_continuous_750ms_hold(self):
        popup=self.overview();preview=popup.preview;ref=self.paths[1]
        popup.select_item(ref);button=preview.remove_button;self.assertTrue(button.isVisible())
        QTest.mousePress(button,Qt.LeftButton);self.assertTrue(button.isDown());self.assertEqual(button.text(),'')
        QTest.qWait(250);self.assertIn(ref,self.shelf.files['permanent'])
        QTest.mouseRelease(button,Qt.LeftButton);QTest.qWait(550)
        self.assertIn(ref,self.shelf.files['permanent']);self.assertFalse(button.isDown());self.assertEqual(button.text(),'')
        QTest.mousePress(button,Qt.LeftButton);QTest.qWait(700)
        self.assertIn(ref,self.shelf.files['permanent'])
        QTest.qWait(80);fixture.flush()
        self.assertNotIn(ref,self.shelf.files['permanent']);self.assertTrue(Path(ref).is_file())
        QTest.mouseRelease(button,Qt.LeftButton);fixture.flush()

    def test_hold_cancels_if_the_selected_item_changes(self):
        popup=self.overview();preview=popup.preview;popup.select_item(self.paths[0])
        button=preview.remove_button;QTest.mousePress(button,Qt.LeftButton)
        popup.select_item(self.paths[1]);QTest.qWait(800);fixture.flush()
        self.assertEqual(self.shelf.files['permanent'],self.paths)
        QTest.mouseRelease(button,Qt.LeftButton)

    def test_hold_progress_fills_full_button_height(self):
        from reference_widgets import palette
        popup=self.overview();popup.select_item(self.paths[0]);button=popup.preview.remove_button
        QTest.mousePress(button,Qt.LeftButton);QTest.qWait(330)
        try:
            image=button.grab().toImage();ratio=image.devicePixelRatio();red=QColor(palette()['red'])
            def sample(x,y):return image.pixelColor(round(button.width()*x*ratio),round(button.height()*y*ratio))
            for y in (.25,.75):self.assertEqual(sample(.15,y),red)
            self.assertNotEqual(sample(.85,.25),red)
        finally:QTest.mouseRelease(button,Qt.LeftButton)

    def test_remove_hover_and_hold_show_red_and_contrasting_icon(self):
        popup=self.overview();popup.select_item(self.paths[0]);button=popup.preview.remove_button
        for theme in ('dark','light'):
            ui_theme.set_theme(theme);self.settings.update(theme=theme)
            refresh_tree(self.shelf);fixture.flush()
            button.setAttribute(Qt.WA_UnderMouse,False);button.update();fixture.flush()
            group=button.parentWidget()
            def sample():
                image=group.grab().toImage();ratio=image.devicePixelRatio()
                return image.pixelColor(round((button.x()+5)*ratio),round((button.y()+button.height()/2)*ratio))
            normal=sample()
            button.setAttribute(Qt.WA_UnderMouse,True);button.update();fixture.flush()
            hovered=sample()
            self.assertGreater(hovered.red(),normal.red())
            self.assertGreater(hovered.red(),hovered.green())
            button._begin_hold()
            try:
                with patch('reference_widgets.time.monotonic',return_value=button._hold_started+.43):
                    image=button.grab().toImage()
                ratio=image.devicePixelRatio();mid=round(button.width()*.57*ratio)
                white=red=0
                for y in range(round(8*ratio),round(24*ratio)):
                    for x in range(round(8*ratio),round(24*ratio)):
                        color=image.pixelColor(x,y)
                        if x<mid and min(color.red(),color.green(),color.blue())>220:white+=1
                        if x>=mid and color.red()>200 and color.green()<120 and color.blue()<120:red+=1
                self.assertGreater(white,0);self.assertGreater(red,0)
            finally:
                button.cancel_hold();button.setAttribute(Qt.WA_UnderMouse,False)

    def test_keyboard_hold_supports_cancel_and_confirmation(self):
        popup=self.overview();popup.select_item(self.paths[0]);button=popup.preview.remove_button
        button.setFocus();QTest.keyPress(button,Qt.Key_Space);QTest.qWait(100);QTest.keyRelease(button,Qt.Key_Space)
        QTest.qWait(750);self.assertIn(self.paths[0],self.shelf.files['permanent'])
        QTest.keyPress(button,Qt.Key_Space);QTest.qWait(780);fixture.flush()
        self.assertNotIn(self.paths[0],self.shelf.files['permanent'])
        QTest.keyRelease(button,Qt.Key_Space)

    def test_preview_zip_contains_actual_current_file_and_preserves_bytes(self):
        popup=self.overview();preview=popup.preview;ref=self.paths[1]
        popup.select_item(ref);output=self.base/'preview-output.zip';original=Path(ref).read_bytes()
        with patch.object(self.shelf,'choose_zip_path',return_value=str(output)):
            preview.zip_button.click()
            fixture.until(lambda:self.shelf.archive_job is None and output.is_file())
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.namelist(),[Path(ref).name]);self.assertEqual(archive.read(Path(ref).name),original)
        self.assertEqual(Path(ref).read_bytes(),original)

    def test_native_text_actions_materialize_one_real_file_and_pending_is_disabled(self):
        popup=self.overview();preview=popup.preview
        ref=self.shelf.content.add_text('完整原文\n中文内容');self.shelf.add_paths('permanent',[ref]);popup.select_item(ref)
        preview.copy_path_button.click();path=QApplication.clipboard().text()
        self.assertEqual(Path(path).read_text(encoding='utf8'),'完整原文\n中文内容')
        preview.send_button.click()
        self.assertEqual(self.events[-1],('lan',[path]))
        self.assertFalse(preview.rename_button.isEnabled())
        pending='flickdock:transfer:pending-action'
        self.shelf.content.pending[pending]=dict(name='remote.txt',kind='file')
        self.shelf.refresh();popup.preview.show_path(pending)
        self.assertTrue(preview.remove_button.isEnabled())
        for button in preview.action_buttons[:-1]:self.assertFalse(button.isEnabled())


if __name__=='__main__':unittest.main(verbosity=2)
