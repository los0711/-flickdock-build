"""New workspace contracts, using disposable profiles and native Qt fixtures."""
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget
import test_html_native_workspace as fixture
from file_pins import FilePins
import ui_theme


class WorkspaceTests(unittest.TestCase):
    setUp=fixture.WorkspaceTests.setUp
    overview=fixture.WorkspaceTests.overview
    def tearDown(self):
        if self.shelf.popup:self.shelf.popup.preview._close_reveal()
        if hasattr(self.shelf,'file_pins'):self.shelf.file_pins.close()
        fixture.WorkspaceTests.tearDown(self)
    def test_scopes_and_type_filters_are_independent_and_pin_orders_first(self):
        popup=self.overview();self.assertEqual(set(popup.scope_buttons),{'all','pinned','uploading'})
        self.assertEqual(list(popup.type_buttons),['all','files','links','text','images','video','audio'])
        ref=self.paths[1];popup.toggle_pin(ref);fixture.flush()
        self.assertEqual(popup.visible_refs()[0],ref);self.assertEqual(popup.nav_counts['pinned'].text(),'1')
        popup.set_scope('pinned');self.assertEqual(popup.visible_refs(),[ref])
        popup.set_type('images');self.assertEqual(popup.scope,'pinned');self.assertFalse(popup.visible_refs())
        popup.set_type('all');self.assertEqual(popup.visible_refs(),[ref])
        popup.scope_buttons['all'].click();self.assertEqual(len(popup.visible_refs()),3)
        self.assertTrue(all(control.height()==26 for control in popup.type_buttons.values()))
        self.assertTrue(all(control.text().split()[-1].isdigit() for control in popup.type_buttons.values()))
    def test_list_pin_does_not_select_or_transfer_and_draws_thumbnail_badge(self):
        popup=self.overview();row=popup._item_widgets[self.paths[1]]
        fixture.APP.sendEvent(row,QEvent(QEvent.Enter));self.assertTrue(row.pin_button.isVisible())
        with patch.object(self.shelf,'drag_files') as drag:
            row.pin_button.click();fixture.flush();self.assertTrue(popup.is_pinned(self.paths[1]));drag.assert_not_called()
        self.assertFalse(popup.selected);self.assertEqual(row.pin_button.glyph,'star-filled')
        self.assertEqual(row.pin_button.property('glyphSize'),14)
    def test_type_indicator_retargets_current_spring_and_runs_beside_file_entrance(self):
        popup=self.overview();self.manager.motion=True
        with patch('ui_transitions.motion_allowed',return_value=True),patch('native_ui_common.reduced_motion',return_value=False):
            popup.set_type('files');motion=popup.type_body._selection_animation
            self.assertTrue(motion._timer.isActive());QTest.qWait(25);fixture.flush()
            values=dict(motion.values);popup.set_type('text');self.assertEqual(motion.values,values)
            self.assertTrue(motion._timer.isActive());self.assertEqual(popup.type_filter,'text')
            fixture.until(lambda:popup.item_transitions.entry_started is not None)
            self.assertTrue(motion._timer.isActive())
        self.manager.motion=False;motion.jump(motion._targets)
    def test_card_pin_is_only_at_thumbnail_and_selected_remove_remains_visible(self):
        popup=self.overview();popup.change_view_mode('icons');fixture.flush();fixture.until(lambda:len(popup._item_widgets)==3)
        tile=popup._item_widgets[self.paths[1]];self.assertEqual(tile.width(),178)
        fixture.APP.sendEvent(tile,QEvent(QEvent.Enter));tile.pin_button.click();fixture.flush()
        self.assertTrue(popup.is_pinned(tile.ref));self.assertFalse(popup.selected)
        self.assertLess(tile.pin_button.geometry().center().x(),tile.width()-40)
        popup.select_item(tile.ref);tile.setFocus(Qt.MouseFocusReason);fixture.flush()
        self.assertTrue(tile.remove_button.isVisible());self.assertTrue(tile.pin_button.isVisible())
        popup.selected.clear();tile.set_selected(False);tile.refresh_pin();self.assertTrue(tile.pin_button.isVisible())
    def test_three_card_edges_match_toolbar_and_width_never_moves_on_arrival(self):
        popup=self.overview();popup.change_view_mode('icons');fixture.flush();fixture.until(lambda:len(popup._item_widgets)==3)
        tiles=sorted(popup._item_widgets.values(),key=lambda item:item.x())
        first=fixture.WorkspaceTests.bounds(self,tiles[0],popup.middle)
        last=fixture.WorkspaceTests.bounds(self,tiles[-1],popup.middle)
        search=fixture.WorkspaceTests.bounds(self,popup.search_edit,popup.middle)
        right=popup.middle.width()-last.right()-1
        self.assertLessEqual(abs(first.left()-search.left()),1);self.assertLessEqual(abs(right-search.left()),2)
        width=tiles[0].width();extra=self.base/'arrival.zip';extra.write_bytes(b'synthetic')
        self.shelf.add_paths('permanent',[str(extra)]);fixture.flush();fixture.until(lambda:len(popup._item_widgets)==4)
        self.assertEqual({item.width() for item in popup._item_widgets.values()},{width})
        popup.remove_path(str(extra));fixture.flush();self.assertEqual({item.width() for item in popup._item_widgets.values()},{width})
    def test_path_block_double_click_reveals_off_gui_and_copy_button_is_excluded(self):
        popup=self.overview();popup.select_item(self.paths[0]);fixture.flush();calls=[];main=threading.get_ident()
        def reveal(ref,**options):calls.append((ref,options,threading.get_ident()))
        with patch('transfer_workbench.reveal_file',side_effect=reveal):
            QTest.mouseDClick(popup.preview.path_label,Qt.LeftButton);fixture.until(lambda:bool(calls))
            self.assertEqual(calls[0][0],self.paths[0]);self.assertEqual(calls[0][1],{'check_exists':False})
            self.assertNotEqual(calls[0][2],main)
            fixture.until(lambda:popup.preview._reveal_job is None)
            self.assertNotIn(popup.preview.copy_path_button,popup.preview._path_hit_parts)
    def test_disabled_trigger_has_no_hit_region_and_cannot_reveal(self):
        from dock_handle import DockHandle
        self.settings.update(trigger_visible=False)
        self.manager.edge_suppressed=False;self.manager.show_docked=lambda:self.events.append('reveal')
        edge=DockHandle(self.manager)
        try:
            edge.show_handle();self.assertFalse(edge.isVisible());self.assertFalse(edge.active());self.assertTrue(edge.activation_region().isEmpty())
            fixture.APP.sendEvent(edge,QEvent(QEvent.Enter));self.assertNotIn('reveal',self.events)
        finally:edge.appearance.stop();edge.hide();edge.deleteLater();fixture.flush()


class PinStoreTests(unittest.TestCase):
    def test_async_load_preserves_edits_and_remap_roundtrips(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'file-pins.json';path.write_text(json.dumps(dict(schema_version=1,refs=['old','keep'])),encoding='utf8')
            pins=FilePins(directory);pins.set('old',False);pins.set('new',True)
            fixture.until(lambda:pins.ready);self.assertEqual(pins.refs,{'keep','new'})
            pins.remap({'new':'verified'});pins.close()
            pins=FilePins(directory);fixture.until(lambda:pins.ready);self.assertEqual(pins.refs,{'keep','verified'});pins.close()
    def test_corruption_is_visible_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'file-pins.json';raw=b'{damaged';path.write_bytes(raw);errors=[]
            pins=FilePins(directory);pins.error.connect(errors.append);fixture.until(lambda:pins.ready)
            pins.set('new',True);pins.close();self.assertTrue(errors);self.assertEqual(path.read_bytes(),raw)


class BorderTests(unittest.TestCase):
    def test_highlight_changes_only_existing_stroke_and_is_idle_without_motion(self):
        from border_reveal import install_border_reveal
        from native_ui_common import paint_rounded_surface
        controller=install_border_reveal(fixture.APP)
        class Surface(QWidget):
            stroke=True
            def paintEvent(self,event):
                p=QPainter(self);paint_rounded_surface(p,self.rect(),12,QColor('#303030'),QColor('#555555') if self.stroke else None);p.end()
        surface=Surface();surface.resize(220,140);surface.show();fixture.flush()
        try:
            ui_theme.set_theme('dark');controller.set_position(None,None);fixture.flush();before=surface.grab().toImage()
            controller.set_position(surface.mapToGlobal(QPoint(110,0)),surface);fixture.flush();after=surface.grab().toImage()
            self.assertEqual(before.pixelColor(110,70),after.pixelColor(110,70))
            self.assertGreater(after.pixelColor(110,0).red(),before.pixelColor(110,0).red())
            self.assertFalse(controller.pending.isActive())
            surface.stroke=False;surface.update();fixture.flush();without=surface.grab().toImage()
            self.assertEqual(without.pixelColor(110,0),without.pixelColor(110,70))
        finally:surface.hide();surface.deleteLater();fixture.flush();controller.set_position(None,None);fixture.flush()
    def test_multi_file_pile_has_both_left_and_right_offsets(self):
        from edge_drop_animation import file_stack_artwork
        from edge_magnetic_model import DEFAULT_CONFIG
        images=[];tints=('#ff0000','#00ff00','#0000ff','#ffff00')
        for tint in tints:
            image=QImage(64,64,QImage.Format_ARGB32_Premultiplied);image.fill(QColor(tint));images.append(image)
        result=file_stack_artwork(images,4,DEFAULT_CONFIG)
        counts={tint:0 for tint in tints}
        for y in range(result.height()):
            for x in range(result.width()):
                pixel=result.pixelColor(x,y).name()
                if pixel in counts:counts[pixel]+=1
        self.assertTrue(all(count>80 for count in counts.values()),counts)
    def test_blank_native_zip_icon_uses_identifiable_format_fallback(self):
        import edge_native_icons
        with tempfile.TemporaryDirectory() as directory:
            request=Path(directory)/'request.json';ref=Path(directory)/'archive.zip';ref.write_bytes(b'fixture')
            request.write_text(json.dumps(dict(path=str(ref),size=96)),encoding='utf8')
            with patch('flickdock_icons._native_icon',return_value=QPixmap()):self.assertEqual(edge_native_icons._helper(request),0)
            result=QImage(str(Path(directory)/'icon.png'));self.assertFalse(result.isNull())
            self.assertGreater(sum(result.pixelColor(x,48).alpha()>0 for x in range(96)),30)


if __name__=='__main__':unittest.main(verbosity=2)
