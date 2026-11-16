"""Actual Qt endpoint, file-identity and interrupted-frame presentation checks."""
import unittest
from unittest.mock import patch

from PySide6.QtCore import QPointF,QRectF,Qt
from PySide6.QtGui import QPainter,QPixmap

import test_html_native_workspace as workspace


def render(snapshot):
    width=snapshot.rect_global.width()-28;height=snapshot.rect_global.height()-27
    image=QPixmap(width,height);image.fill(Qt.transparent)
    painter=QPainter(image);snapshot.paint_frame(painter,QRectF(0,0,width,height));painter.end()
    return image.toImage().convertToFormat(__import__('PySide6.QtGui',fromlist=['QImage']).QImage.Format_RGBA8888)


def difference(first,last):
    assert first.size()==last.size()
    a=bytes(first.constBits());b=bytes(last.constBits())
    return sum(abs(x-y) for x,y in zip(a,b))/len(a)


class SharedElementTests(unittest.TestCase):
    def test_capture_walk_excludes_hidden_and_clipped_widget_subtrees(self):
        from PySide6.QtCore import QRect
        from PySide6.QtWidgets import QWidget
        from surface_morph import visible_render_widgets
        root=QWidget();root.resize(200,100)
        visible=QWidget(root);visible.setGeometry(10,10,50,50)
        hidden=QWidget(root);hidden.hide();hidden_child=QWidget(hidden)
        clipped=QWidget(root);clipped.setGeometry(300,0,20,20);clipped_child=QWidget(clipped)
        partial=QWidget(root);partial.setGeometry(190,0,50,50)
        inside=QWidget(partial);inside.setGeometry(0,0,5,5)
        outside=QWidget(partial);outside.setGeometry(30,0,5,5)
        explicit=QWidget(root);explicit.setGeometry(0,0,10,10);explicit.setUpdatesEnabled(False)
        root.show();workspace.flush()
        try:
            walked=set(visible_render_widgets(root))
            self.assertEqual(walked,{root,visible,partial,inside})
            self.assertNotIn(hidden_child,walked);self.assertNotIn(clipped_child,walked)
            self.assertNotIn(outside,walked);self.assertFalse(explicit.updatesEnabled())
        finally:root.close();root.deleteLater();workspace.flush()

    def setUp(self):
        self.fixture=workspace.WorkspaceTests();self.fixture.setUp();self.shelf=self.fixture.shelf
        self.fixture.patches.enter_context(patch('native_ui_common.reduced_motion',return_value=False))
        self.shelf.manager.motion=True;self.shelf.geometry_morph.motion=True

    def tearDown(self):self.fixture.tearDown()

    def expand(self):
        self.shelf.show_contents();self.shelf.geometry_morph.pause();self.shelf.presentation.stop()
        return self.shelf._surface_snapshot

    def test_interrupted_native_frames_release_overlay_and_restore_overview_input(self):
        from PySide6.QtTest import QTest
        self.shelf.show_contents();morph=self.shelf.geometry_morph
        self.assertTrue(morph.active);self.assertTrue(self.shelf._morph_host_active)
        morph._frames.stop();QTest.qWait(morph.animation.duration()+150);workspace.flush()
        self.assertFalse(morph.active);self.assertFalse(self.shelf._morph_host_active)
        self.assertFalse(self.shelf._surface_snapshot.active)
        control=self.shelf.popup.category_buttons['files'];self.assertTrue(control.isVisible())
        QTest.mouseClick(control,Qt.LeftButton);workspace.flush()
        self.assertEqual(self.shelf.popup.category,'files')

    def test_file_cards_use_original_endpoint_fades_without_repainting_widgets_each_frame(self):
        for mode in ('list','icons'):
            with self.subTest(mode=mode):
                self.shelf.manager.settings.update(view_mode=mode)
                snapshot=self.expand();elements=snapshot.elements
                self.assertFalse(any(key[0]=='file' for key in elements.keys))
                self.assertNotIn(('brand',),elements.keys);self.assertNotIn(('clipboard',),elements.keys)
                native=self.shelf.geometry();live=self.shelf.card.geometry();captures=snapshot.capture_count
                with patch.object(snapshot,'capture_live',side_effect=AssertionError('per-frame widget capture')):
                    for fraction in (.03,.12,.3,.55,.8,.99):
                        self.shelf.geometry_morph._frame(fraction);render(snapshot)
                        self.assertEqual(self.shelf.geometry(),native);self.assertEqual(self.shelf.card.geometry(),live)
                self.assertEqual(snapshot.capture_count,captures)
                self.shelf.geometry_morph.complete();self.shelf.collapse_contents();self.shelf.geometry_morph.complete()

    def test_reversing_from_a_partial_frame_keeps_current_pixels_and_card_positions(self):
        snapshot=self.expand()
        for fraction in (.18,.38,.65):
            self.shelf.geometry_morph._frame(fraction)
            before=render(snapshot);positions={key:QRectF(rect) for key,rect,_,_ in snapshot.elements.frames(
                snapshot.mix,*snapshot._element_offsets(QRectF(0,0,before.width(),before.height())),snapshot.choreography)}
            self.shelf.toggle_contents();self.shelf.geometry_morph.pause();self.shelf.presentation.stop()
            after=render(snapshot)
            self.assertLess(difference(before,after),1.5,'reversal changed the already-presented frame')
            for key,rect,_,_ in snapshot.elements.frames(0,*snapshot._element_offsets(QRectF(0,0,after.width(),after.height())),0):
                if key in positions:self.assertLess((rect.center()-positions[key].center()).manhattanLength(),.01)
        self.shelf.geometry_morph.complete()

    def test_empty_state_and_long_distance_controls_fade_in_place_without_file_preview_content(self):
        self.shelf.remove_all('permanent');workspace.flush()
        snapshot=self.expand()
        self.assertFalse(snapshot.elements.keys,'Unrelated empty-state text and distant controls must use the endpoint fades')
        self.assertFalse(any(key[0]=='file' for key in snapshot.elements.keys))
        for fraction in (.1,.5,.9):self.shelf.geometry_morph._frame(fraction);render(snapshot)
        self.assertIsNone(self.shelf.popup.preview.current_path)
        self.shelf.geometry_morph.complete()
        self.assertEqual(self.shelf.popup.preview.stack.currentWidget(),self.shelf.popup.preview.empty_preview)

    def test_files_remain_in_their_layout_when_category_changes(self):
        folder=self.fixture.base/'other';folder.mkdir();other=folder/'report.txt';other.write_text('different file')
        self.shelf.add_paths('permanent',[str(other)]);workspace.flush()
        snapshot=self.expand()
        roots={key[1] for key in snapshot.elements.roots if key[0]=='file'}
        self.assertFalse(roots)
        self.shelf.geometry_morph.complete();workspace.flush()
        self.shelf.popup.set_category('images');workspace.flush()
        self.shelf.collapse_contents();self.shelf.geometry_morph.pause();self.shelf.presentation.stop()
        self.assertFalse(any(key[0]=='file' for key in self.shelf._surface_snapshot.elements.keys))

    def test_reduced_motion_changes_to_live_endpoint_without_starting_compositor(self):
        with patch('native_ui_common.reduced_motion',return_value=True):
            self.shelf.show_contents()
            self.assertFalse(self.shelf.geometry_morph.active);self.assertFalse(self.shelf._morph_host_active)
            self.shelf.collapse_contents();self.assertFalse(self.shelf.geometry_morph.active)

    def test_chrome_and_mini_content_exit_and_arrive_near_the_end(self):
        for empty in (False,True):
            if empty:self.shelf.remove_all('permanent');workspace.flush()
            snapshot=self.expand();elements=snapshot.elements
            mini={key[-1] for key in elements.source if key[:2]==('chrome','mini')}
            self.assertEqual(mini,{'brand','transfer','capacity','clipboard','count','content','hint'})
            self.assertTrue(self.shelf.lan_button.isHidden())
            def layers(t):
                self.shelf.geometry_morph._frame(t)
                rect=QRectF(0,0,snapshot.rect_global.width()-28,snapshot.rect_global.height()-27)
                offsets=snapshot._element_offsets(rect)
                return list(elements.frames(snapshot.mix,*offsets,snapshot.choreography)),offsets
            early,offsets=layers(.15)
            brand=next(frame for frame in early if frame[0]==('chrome','mini','brand'))
            self.assertLess(brand[1].x(),elements.source[brand[0]].rect.x()+offsets[0].x())
            self.assertGreater(brand[3][0][1],0);self.assertLess(brand[3][0][1],1)
            middle,_=layers(.45)
            self.assertFalse(any(frame[0][0]=='chrome' for frame in middle))
            late,_=layers(.78)
            self.assertTrue(any(frame[0][:2]==('chrome','overview') for frame in late))
            self.shelf.geometry_morph.complete();self.assertTrue(self.shelf.lan_button.isHidden())
            self.shelf.collapse_contents();self.shelf.geometry_morph.pause();self.shelf.presentation.stop()
            snapshot=self.shelf._surface_snapshot;elements=snapshot.elements
            early,_=layers(.45)
            self.assertFalse(any(frame[0][:2]==('chrome','mini') for frame in early))
            late,_=layers(.78)
            arriving={frame[0][-1] for frame in late if frame[0][:2]==('chrome','mini')}
            self.assertEqual(arriving,mini)
            content=next(frame for frame in late if frame[0]==('chrome','mini','content'))
            self.assertGreater(content[1].y(),elements.target[content[0]].rect.y()+snapshot._element_offsets(
                QRectF(0,0,snapshot.rect_global.width()-28,snapshot.rect_global.height()-27))[1].y())
            self.shelf.geometry_morph.complete();self.assertTrue(self.shelf.lan_button.isVisible())

    def test_collapse_uses_a_bounded_elastic_return(self):
        self.expand();self.shelf.geometry_morph.complete()
        self.shelf.collapse_contents();morph=self.shelf.geometry_morph;morph.pause();self.shelf.presentation.stop()
        target=morph._to;right=target.right();widths=[]
        for step in range(101):
            morph._frame(step/100)
            widths.append(morph.current.width());self.assertEqual(morph.current.right(),right)
        self.assertLess(min(widths),target.width())
        self.assertGreaterEqual(min(widths),target.width()-max(16,round((morph._from.width()-target.width())*.02)))
        self.assertEqual(widths[-1],target.width());morph.complete()

    def test_open_and_collapse_ease_out_with_subtle_rebound(self):
        from PySide6.QtCore import QEasingCurve
        self.expand();morph=self.shelf.geometry_morph
        for direction in ('open','collapse'):
            self.assertEqual(morph._geometry_curve.type(),QEasingCurve.OutBack)
            start=morph._from.width();end=morph._to.width();distance=abs(end-start)
            morph._frame(.25);early=abs(morph.current.width()-start)
            morph._frame(.9);remaining=abs(morph.current.width()-end)
            self.assertGreater(early,distance*.5)
            self.assertLess(remaining,distance*.02)
            morph.complete()
            if direction=='open':self.shelf.collapse_contents();morph.pause();self.shelf.presentation.stop()

    def test_light_and_dark_endpoint_pixels_match_the_live_layout_without_preview_ghosts(self):
        import ui_theme
        from reference_widgets import refresh_tree
        for theme in ('dark','light'):
            ui_theme.set_theme(theme);ui_theme.apply_application(workspace.APP,workspace.shelf_ui_common.FONT_FAMILY)
            refresh_tree(self.shelf)
            for mode in ('list','icons'):
                self.shelf.manager.settings.update(view_mode=mode);snapshot=self.expand()
                for fraction in (0.,1.):
                    self.shelf.geometry_morph._frame(fraction);shared=render(snapshot)
                    elements=snapshot.elements;snapshot.elements=None
                    original=render(snapshot);snapshot.elements=elements
                    self.assertLess(difference(shared,original),1.2,(theme,mode,fraction))
                self.assertIsNone(self.shelf.popup.preview.current_path)
                self.shelf.geometry_morph.complete();self.shelf.collapse_contents();self.shelf.geometry_morph.complete()


if __name__=='__main__':unittest.main(verbosity=2)
