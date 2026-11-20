"""Protect the solid page color and scroll edges using isolated Qt widgets."""
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTextEdit, QWidget

from app_settings import SettingsComboBox, SettingsSurface
from native_ui_common import colors, qss_color
from settings_navigation import _blurred_edge, install_smooth_scrolling


class GlassUiTests(unittest.TestCase):
    def test_light_window_background_is_solid_reference_gray(self):
        app=QApplication.instance() or QApplication([])
        surface=SettingsSurface()
        surface.resize(150,100)
        self.assertEqual(surface.grab().toImage().pixelColor(75,50).getRgb(),(227,228,232,255))

    def test_translator_language_list_does_not_paint_blur_strips_over_items(self):
        from translator_store import TranslatorStore
        from translator_widgets import LanguagePopup
        app=QApplication.instance() or QApplication([])
        install_smooth_scrolling(app)
        owner=QWidget()
        owner.show()
        with tempfile.TemporaryDirectory() as directory:
            popup=LanguagePopup(TranslatorStore(directory,autoload=False),owner)
            try:
                popup.configure('en')
                popup.show()
                QTest.qWait(80)
                bar=popup.list.verticalScrollBar()
                self.assertGreater(bar.maximum(),0)
                bar.setValue(bar.maximum()//2)
                QTest.qWait(50)
                self.assertFalse(hasattr(popup.list,'_flickdock_edge_fade'))
            finally:
                popup.close()
                owner.close()

    def test_popup_edges_follow_popup_color_in_both_themes(self):
        app = QApplication.instance() or QApplication([])
        install_smooth_scrolling(app)
        owner = QWidget()
        menu = SettingsComboBox(owner)
        for _ in range(40):
            menu.addItem('Choice')
        owner.show()
        try:
            for theme in ('light', 'dark'):
                with self.subTest(theme=theme):
                    menu.dark = theme == 'dark'
                    menu.showPopup()
                    QTest.qWait(60)
                    p = menu.view().window()
                    self.assertTrue(p.mask().isEmpty())
                    surface=p.grab().toImage()
                    self.assertEqual(surface.pixelColor(0,0).alpha(),0)
                    self.assertGreater(surface.pixelColor(p.width()//2,1).alpha(),0)
                    fade = menu.view()._flickdock_edge_fade
                    self.assertTrue(fade.isVisible())
                    image = fade.grab().toImage()
                    actual = image.pixelColor(image.width() // 2, image.height() - 1)
                    expected = qss_color(colors(theme)['popup_surface'])
                    # The last pixel samples halfway inside the gradient.
                    for actual_channel, expected_channel in zip(actual.getRgb()[:3],expected.getRgb()[:3]):
                        self.assertLessEqual(abs(actual_channel - expected_channel), 10)
                    menu.hidePopup()
        finally:
            owner.close()

    def test_direction_combo_shows_all_four_rows_at_the_bottom_of_the_screen(self):
        from native_ui_common import screen_for_widget
        host=QWidget();host.resize(330,180);host.show()
        combo=SettingsComboBox(host);combo.setGeometry(20,130,136,30)
        combo.addItems(['左侧','右侧','顶部','底部'])
        screen=screen_for_widget(host)
        host.move(screen.availableGeometry().left()+40,screen.availableGeometry().bottom()-163)
        combo.show()
        try:
            for selected in (0,1,3):
                combo.setCurrentIndex(selected)
                combo.showPopup()
                QTest.qWait(45)
                view=combo.view()
                self.assertTrue(view.isVisible())
                for row in range(4):
                    self.assertTrue(view.viewport().rect().contains(view.visualRect(combo.model().index(row,0))))
                combo.hidePopup()
        finally:host.close()

    def test_edge_blurs_detail_and_preserves_the_clear_boundary(self):
        source=QImage(220,62,QImage.Format_ARGB32_Premultiplied)
        source.fill(Qt.white)
        p=QPainter(source)
        for x in range(4,220,8):
            p.fillRect(x,0,3,62,Qt.black)
        p.end()
        for top in (True,False):
            with self.subTest(top=top):
                rendered=_blurred_edge(source,50,top=top,tone=QColor('white'))
                middle=rendered.pixelColor(12,16 if top else 33).red()
                self.assertGreater(middle,100)
                self.assertLess(middle,245)
                self.assertLess(rendered.pixelColor(12,49 if top else 0).red(),12)

    def test_editor_blur_stays_in_place_and_leaves_the_center_unpainted(self):
        app=QApplication.instance() or QApplication([])
        install_smooth_scrolling(app)
        editor=QTextEdit()
        editor.setPlainText('\n'.join(f'Text row {row:02} to verify fixed strips' for row in range(90)))
        editor.resize(420,280)
        editor.show()
        try:
            bar=editor.verticalScrollBar()
            bar.setValue(bar.maximum()//2)
            QTest.qWait(80)
            fade=editor._flickdock_edge_fade
            self.assertEqual(fade.geometry(),editor.viewport().geometry())
            self.assertEqual(fade._edge_height(),48)
            softened=editor.grab().toImage()
            fade.hide()
            clear=editor.grab().toImage()
            self.assertNotEqual(softened.copy(0,0,360,50),clear.copy(0,0,360,50))
            self.assertEqual(softened.copy(0,105,360,50),clear.copy(0,105,360,50))
        finally:
            editor.close()

    def test_blur_strength_ramps_up_without_lagging_the_current_scroll_position(self):
        app=QApplication.instance() or QApplication([])
        install_smooth_scrolling(app)
        editor=QTextEdit()
        editor.setPlainText('\n'.join(f'{row:02}  Render row {row}' for row in range(50)))
        editor.resize(380,240);editor.show()
        try:
            bar=editor.verticalScrollBar();fade=editor._flickdock_edge_fade
            strengths=[]
            for value in (0,4,12,24,32):
                bar.setValue(value)
                strengths.append(fade._strengths[0])
            self.assertEqual(strengths[0],0.)
            self.assertEqual(strengths[-1],1.)
            self.assertEqual(strengths,sorted(set(strengths)))
            first=editor.grab().toImage().copy(0,0,360,70)
            bar.setValue(53)
            second=editor.grab().toImage().copy(0,0,360,70)
            self.assertNotEqual(first,second)
        finally:
            editor.close()

    def test_outline_follows_scrolled_cards_and_clears_on_window_deactivation(self):
        from PySide6.QtCore import QEvent, QRectF
        from PySide6.QtWidgets import QScrollArea
        from border_reveal import install_border_reveal
        from native_ui_common import paint_rounded_surface

        class Card(QWidget):
            def paintEvent(self,event):
                painter=QPainter(self)
                paint_rounded_surface(painter,self.rect(),12,QColor('#F7F7F9'),QColor('#C2C2C6'))
                painter.end()

        app=QApplication.instance() or QApplication([])
        reveal=install_border_reveal(app)
        scroll=QScrollArea();scroll.setProperty('scrollEdgeBlur',False);scroll.resize(280,170)
        body=QWidget();body.resize(250,520);scroll.setWidget(body)
        first=Card(body);first.setGeometry(10,20,216,70)
        second=Card(body);second.setGeometry(10,280,216,70)
        scroll.setProperty('uiTheme','light')
        scroll.show()
        try:
            QTest.qWait(25)
            light=first.grab().toImage()
            point=first.mapToGlobal(QPoint(100,0))
            reveal.set_position(point,scroll.window())
            QTest.qWait(25)
            self.assertEqual(light,first.grab().toImage())
            self.assertIsNone(reveal.point)
            scroll.setProperty('uiTheme','dark')
            baseline=first.grab().toImage()
            second.grab()
            point=first.mapToGlobal(QPoint(100,0))
            reveal.set_position(point,scroll.window())
            QTest.qWait(25)
            highlighted=first.grab().toImage()
            self.assertNotEqual(baseline.pixelColor(100,0),highlighted.pixelColor(100,0))
            scroll.verticalScrollBar().setValue(260)
            QTest.qWait(25)
            self.assertNotIn(first,reveal._dirty_regions)
            self.assertIn(second,reveal._dirty_regions)
            above=second.grab().toImage()
            app.sendEvent(scroll,QEvent(QEvent.WindowDeactivate))
            QTest.qWait(25)
            after=second.grab().toImage()
            self.assertNotEqual(above.pixelColor(100,0),after.pixelColor(100,0))
            self.assertIsNone(reveal.point)
            self.assertFalse(reveal.pending.isActive())
        finally:
            reveal.set_position(None,None)
            scroll.close()


class OverviewMotionTests(unittest.TestCase):
    def setUp(self):
        from test_html_native_workspace import WorkspaceTests

        fixture=WorkspaceTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.view=fixture.overview()
        self.view.resize(1040,610)
        QTest.qWait(35)
        fixture.manager.motion=True
        animation=patch('ui_transitions.motion_allowed',return_value=True)
        animation.start()
        self.addCleanup(animation.stop)

    def test_sidebar_reverses_and_keeps_icon_buttons_usable(self):
        from test_html_native_workspace import until

        view=self.view;motion=view._sidebar_collapse
        wide=view.sidebar.width()
        view.sidebar_toggle.click()
        QTest.qWait(85)
        self.assertLess(view.sidebar.width(),wide)
        self.assertGreater(view.sidebar.width(),motion.RAIL_WIDTH)
        view.sidebar_toggle.click()
        until(lambda:view.sidebar.width()==wide and not motion.width._timer.isActive())
        view.sidebar_toggle.click()
        until(lambda:view.sidebar.width()==motion.RAIL_WIDTH and not motion.width._timer.isActive())
        self.assertTrue(view.account_nav.isVisible())
        self.assertTrue(view.translator_nav.isVisible())
        view.update_transfer_status()
        self.assertTrue(all(badge.isHidden() for badge in view.nav_counts.values()))
        view.scope_buttons['pinned'].click()
        self.assertEqual(view.scope,'pinned')

    def test_expand_card_keeps_file_identity_during_rapid_mode_switch(self):
        from content_grid import IconItem
        from overview_ui import FileRow
        from test_html_native_workspace import flush, until

        view=self.view;motion=view.item_transitions
        ref=view.visible_refs()[0]
        view.select_item(ref)
        view.view_grid.click();flush()
        until(lambda:ref in motion.mode_to)
        self.assertIsInstance(view._item_widgets[ref],IconItem)
        self.assertIn(ref,motion.mode_from)
        QTest.qWait(100)
        frames=[pose for key,pose,*_ in motion._mode_frames() if key==ref]
        self.assertEqual(len(frames),1)
        intermediate=frames[0]
        before,after=motion.mode_from[ref],motion.mode_to[ref]
        self.assertLess(intermediate.bounds.width(),before.bounds.width())
        self.assertGreater(intermediate.bounds.width(),after.bounds.width())
        self.assertGreater(intermediate.thumbnail.width(),before.thumbnail.width())
        self.assertLess(intermediate.thumbnail.width(),after.thumbnail.width())
        self.assertEqual(intermediate.title.text,before.title.text)
        selection=intermediate.selection
        self.assertEqual(selection.rect.width(),selection.rect.height())
        self.assertNotEqual(selection.rect.center(),before.selection.rect.center())
        self.assertNotEqual(selection.rect.center(),after.selection.rect.center())
        self.assertGreater(selection.tick.alpha(),0)
        self.assertNotIn(before.selection.rect,tuple(deco[0] for deco in before.decorations))
        self.assertNotIn(after.selection.rect,tuple(deco[0] for deco in after.decorations))
        view.view_list.click();flush()
        self.assertIn(ref,motion.mode_from)
        self.assertLess(motion.mode_from[ref].bounds.width(),view.scroll.viewport().width()-30)
        until(lambda:not motion.mode_from and not motion.timer.isActive())
        self.assertIsInstance(view._item_widgets[ref],FileRow)
        self.assertIn(ref,view.selected)
        self.assertFalse(motion.mode_from)

    def test_light_pinned_card_glyph_contrasts_on_selected_card(self):
        from native_ui_common import colors
        from test_html_native_workspace import contrast_colors, until

        view=self.view;ref=view.visible_refs()[0]
        view.view_grid.click()
        until(lambda:ref in view._item_widgets and not view.item_transitions.mode_from)
        view.toggle_pin(ref)
        until(lambda:ref in view._item_widgets and view._item_widgets[ref].pin_button.glyph=='star-filled')
        control=view._item_widgets[ref].pin_button
        self.assertTrue(control.property('shelfPin'))
        palette=colors('light')
        self.assertGreaterEqual(contrast_colors(QColor(palette['shelf_pin']),QColor(palette['file_card_selected'])),4.5)

    def test_sort_animates_only_a_real_reorder_with_spring_overshoot(self):
        from time import perf_counter
        from test_html_native_workspace import until

        view=self.view;motion=view.item_transitions
        refs=view.visible_refs()
        ranks={ref:rank for rank,ref in enumerate(refs)}
        with patch.object(view,'_sort_size',side_effect=ranks.__getitem__):
            view.cycle_sort('size')
            self.assertEqual(view.visible_refs(),refs)
            self.assertFalse(motion.mode_from)
            view.cycle_sort('size')
            until(lambda:motion.mode_to and motion.mode=='sort')
            moved=next(key for key in motion.mode_to if motion.mode_from[key].bounds!=motion.mode_to[key].bounds)
            original=motion.entry_started
            try:
                target=motion.mode_to[moved].bounds.y()
                start=motion.mode_from[moved].bounds.y()
                motion.entry_started=perf_counter()-.28
                pose=next(pose for key,pose,*_ in motion._mode_frames() if key==moved)
                self.assertGreater((pose.bounds.y()-target)*(target-start),0)
            finally:motion.entry_started=original

    def test_sidebar_resizes_grid_before_the_sidebar_spring_finishes(self):
        from test_html_native_workspace import until

        view=self.view;view.view_grid.click()
        ref=view.visible_refs()[0]
        until(lambda:ref in view._item_widgets and not view.item_transitions.mode_from)
        width=view._item_widgets[ref].width()
        view.sidebar_toggle.click()
        until(lambda:view._item_widgets[ref].width()!=width)
        self.assertTrue(view._sidebar_collapse.width._timer.isActive())
        until(lambda:not view._sidebar_collapse.width._timer.isActive())
        self.assertEqual(view._item_widgets[ref].width(),view._grid_card_width())

    def test_single_tab_pill_stretches_and_retargets(self):
        from PySide6.QtCore import QRectF
        from test_html_native_workspace import flush, until

        view=self.view;bar=view.type_body
        first=QRectF(view.type_buttons['all'].geometry())
        view.type_buttons['audio'].click()
        QTest.qWait(63)
        mid=QRectF(bar._selection_rect)
        self.assertGreater(mid.width(),first.width())
        self.assertGreater(mid.right()-first.right(),mid.left()-first.left())
        view.type_buttons['all'].click()
        self.assertEqual(bar._selection_rect,mid)
        until(lambda:bar._selection_rect==first and not bar._right_edge._timer.isActive())
        flush()
        self.assertEqual(view.type_filter,'all')
