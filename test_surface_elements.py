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
    def setUp(self):
        self.fixture=workspace.WorkspaceTests();self.fixture.setUp();self.shelf=self.fixture.shelf
        self.shelf.manager.motion=True;self.shelf.geometry_morph.motion=True

    def tearDown(self):self.fixture.tearDown()

    def expand(self):
        self.shelf.show_contents();self.shelf.geometry_morph.pause();self.shelf.presentation.stop()
        return self.shelf._surface_snapshot

    def test_list_and_grid_match_real_file_ids_without_repainting_widgets_each_frame(self):
        for mode in ('list','icons'):
            with self.subTest(mode=mode):
                self.shelf.manager.settings.update(view_mode=mode)
                snapshot=self.expand();elements=snapshot.elements
                self.assertEqual({key[1] for key in elements.roots if key[0]=='file'},set(self.fixture.paths))
                self.assertIn(('brand',),elements.keys);self.assertIn(('clipboard',),elements.keys)
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
                snapshot.mix,*snapshot._element_offsets(QRectF(0,0,before.width(),before.height())))}
            self.shelf.toggle_contents();self.shelf.geometry_morph.pause();self.shelf.presentation.stop()
            after=render(snapshot)
            self.assertLess(difference(before,after),1.5,'reversal changed the already-presented frame')
            for key,rect,_,_ in snapshot.elements.frames(0,*snapshot._element_offsets(QRectF(0,0,after.width(),after.height()))):
                if key in positions:self.assertLess((rect.center()-positions[key].center()).manhattanLength(),.01)
        self.shelf.geometry_morph.complete()

    def test_empty_drop_target_is_shared_and_does_not_create_file_preview_content(self):
        self.shelf.remove_all('permanent');workspace.flush()
        snapshot=self.expand()
        self.assertIn(('empty','drop','skin'),snapshot.elements.roots)
        self.assertFalse(any(key[0]=='file' for key in snapshot.elements.keys))
        for fraction in (.1,.5,.9):self.shelf.geometry_morph._frame(fraction);render(snapshot)
        self.assertIsNone(self.shelf.popup.preview.current_path)
        self.shelf.geometry_morph.complete()
        self.assertEqual(self.shelf.popup.preview.stack.currentWidget(),self.shelf.popup.preview.empty_preview)

    def test_same_name_files_stay_distinct_and_hidden_scrolled_rows_are_not_matched(self):
        folder=self.fixture.base/'other';folder.mkdir();other=folder/'report.txt';other.write_text('different file')
        self.shelf.add_paths('permanent',[str(other)]);workspace.flush()
        snapshot=self.expand()
        roots={key[1] for key in snapshot.elements.roots if key[0]=='file'}
        self.assertIn(str(other),roots);self.assertIn(self.fixture.paths[0],roots)
        self.shelf.geometry_morph.complete();workspace.flush()
        self.shelf.popup.set_category('images');workspace.flush()
        self.shelf.collapse_contents();self.shelf.geometry_morph.pause();self.shelf.presentation.stop()
        self.assertFalse(any(key[0]=='file' for key in self.shelf._surface_snapshot.elements.keys))

    def test_reduced_motion_changes_to_live_endpoint_without_starting_compositor(self):
        self.shelf.geometry_morph.motion=False
        self.shelf.show_contents()
        self.assertFalse(self.shelf.geometry_morph.active);self.assertFalse(self.shelf._morph_host_active)
        self.shelf.collapse_contents();self.assertFalse(self.shelf.geometry_morph.active)

    def test_light_and_dark_endpoint_pixels_match_the_live_layout_without_preview_ghosts(self):
        import ui_theme
        from reference_widgets import refresh_tree
        for theme in ('dark','light'):
            ui_theme.set_theme(theme);ui_theme.apply_application(workspace.APP,workspace.subject.FONT_FAMILY)
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
