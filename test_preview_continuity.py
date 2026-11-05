"""Visible preview continuity, cached intake frames and bounded hover behavior."""
from pathlib import Path
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent,QPoint,Qt
from PySide6.QtGui import QImage,QMouseEvent

import reference_widgets
import test_html_native_workspace as workspace


class PreviewContinuityTests(unittest.TestCase):
    def setUp(self):
        self.fixture=workspace.WorkspaceTests();self.fixture.setUp()
        self.fixture.patches.enter_context(patch('native_ui_common.reduced_motion',return_value=False))
        self.shelf=self.fixture.shelf

    def tearDown(self):self.fixture.tearDown()

    def test_video_morph_and_rest_share_poster_without_intermediate_format_icon(self):
        popup=self.fixture.overview();preview=popup.preview
        path=self.fixture.base/'movie.mp4';path.write_bytes(b'decoder fixture')
        ref=str(path);self.shelf.files['permanent'].append(ref)
        poster=QImage(160,90,QImage.Format_RGB32);poster.fill(0xff238ad1)
        self.shelf.preview_cache[ref]=poster
        self.shelf._transition_preparing=True
        with patch.object(preview.media,'_load_player',return_value=True) as load:
            preview.show_path(ref)
            self.assertIs(preview.stack.currentWidget(),preview.media)
            self.assertEqual(preview.media.surface.image,poster)
            self.assertIsNone(preview.media.surface.fallback)
            self.assertTrue(preview._pending_preview);load.assert_not_called()
            self.shelf._transition_preparing=False;preview.resume_preview()
            self.assertIs(preview.stack.currentWidget(),preview.media)
            self.assertEqual(preview.media.surface.image,poster)
            self.assertIsNone(preview.media.surface.fallback)
            load.assert_called_once_with(poster=True)
        # A different video never inherits the preceding file's cached frame.
        other=self.fixture.base/'other.mp4';other.write_bytes(b'second decoder fixture')
        self.shelf._transition_preparing=True
        preview.show_path(str(other))
        self.assertTrue(preview.media.surface.image.isNull())
        self.assertIsNone(preview.media.surface.fallback)
        self.shelf._transition_preparing=False

    def test_intake_animation_composites_cached_cards_without_disk_or_thumbnail_work(self):
        tile=self.shelf.zones['permanent'].icon
        tile.set_stack(self.shelf.content,self.fixture.paths,self.shelf.preview_cache)
        tile.landing.stop();tile.arrival=1
        with patch.object(reference_widgets,'thumbnail',side_effect=AssertionError('thumbnail inside frame')), \
             patch.object(reference_widgets,'item_detail',side_effect=AssertionError('detail inside frame')), \
             patch.object(self.shelf.file_metadata,'request',side_effect=AssertionError('file query inside frame')):
            for index in range(30):
                tile.arrival=index/29;tile.spread.values['spread']=index/29
                self.assertFalse(tile.grab().isNull())
        self.assertLessEqual(len(tile._card_art),5)

    def test_card_cache_refreshes_changed_metadata_without_rebuilding_other_cards(self):
        tile=self.shelf.zones['permanent'].icon;content=self.shelf.content
        tile.set_stack(content,self.fixture.paths,self.shelf.preview_cache)
        previous={ref:art[1].cacheKey() for ref,art in tile._card_art.items()}
        ref=self.fixture.paths[0];content.metadata.setdefault(ref,{})['title']='renamed.txt'
        tile.set_stack(content,self.fixture.paths,self.shelf.preview_cache)
        self.assertNotEqual(tile._card_art[ref][1].cacheKey(),previous[ref])
        self.assertEqual(tile._card_art[self.fixture.paths[1]][1].cacheKey(),previous[self.fixture.paths[1]])

    def test_pointer_hover_is_limited_to_cards_and_drop_highlight_covers_whole_zone(self):
        zone=self.shelf.zones['permanent'];tile=zone.icon
        tile.motion=True;tile.spread.motion=True
        def move(point):
            event=QMouseEvent(QEvent.MouseMove,point,point,Qt.NoButton,Qt.NoButton,Qt.NoModifier)
            zone.mouseMoveEvent(event)
        move(zone.icon.geometry().center())
        self.assertTrue(tile._hovered);self.assertEqual(tile.spread._targets['spread'],1)
        tile.spread.jump({'spread':0});folded=tile.grab().toImage()
        tile.spread.jump({'spread':1});fanned=tile.grab().toImage()
        self.assertNotEqual(folded,fanned)
        move(QPoint(8,zone.height()-8))
        self.assertFalse(tile._hovered);self.assertEqual(tile.spread._targets['spread'],0)
        self.shelf._highlight_content_drop(True);move(QPoint(8,zone.height()-8))
        self.assertTrue(tile._hovered);self.assertEqual(tile.spread._targets['spread'],1)
        self.shelf._highlight_content_drop(False)
        self.assertFalse(tile._hovered)
        with patch('native_ui_common.reduced_motion',return_value=True):tile.set_hover(True)
        self.assertEqual(tile.spread.values['spread'],0)

    def test_close_mid_expansion_fades_current_composite_without_live_endpoint_swap(self):
        shelf=self.shelf;shelf.pinned=False
        shelf.manager.edge=type('Edge',(),{'geometry':lambda _:shelf.geometry(),
            'values':lambda _:{'dock_edge':'right'}})()
        shelf.manager.edge_suppressed=False
        shelf.geometry_morph.motion=True;shelf.presentation.motion=True;shelf.manager.motion=True
        shelf.show_contents();shelf.geometry_morph.pause()
        shelf.geometry_morph.animation.setCurrentTime(130)
        snapshot=shelf._surface_snapshot;rect=shelf.visual_geometry();progress=snapshot.mix
        shelf.hide_animated(explicit=True)
        self.assertTrue(snapshot.active)
        self.assertEqual(snapshot.mix,progress)
        self.assertEqual(shelf.visual_geometry(),rect)
        self.assertEqual(shelf.visible_state,'hiding')
        workspace.until(lambda:not shelf.isVisible())


if __name__=='__main__':unittest.main(verbosity=2)
