"""Visible preview continuity, cached intake frames and bounded hover behavior."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent,QPoint,QPointF,QRectF,Qt
from PySide6.QtGui import QColor,QEnterEvent,QImage,QMouseEvent,QPainter,QPixmap
from PySide6.QtTest import QTest

import reference_widgets
import test_html_native_workspace as workspace


from test_html_native_workspace import SyncView

class PreviewContinuityTests(unittest.TestCase):
    def test_clear_all_then_trigger_preview_retires_old_surface_and_card_pixels(self):
        self.shelf.manager.motion=True;self.shelf.geometry_morph.motion=True
        with patch('native_ui_common.reduced_motion',return_value=False):
            self.shelf.show_contents();self.shelf.geometry_morph.pause()
            snapshot=self.shelf._surface_snapshot
            self.assertTrue(snapshot.active)
            self.shelf.remove_all('permanent')
            self.shelf.hide()
            self.shelf._show_at(self.shelf._dock_target,'dock',QPoint())
            tile=self.shelf.zones['permanent'].icon
            self.assertFalse(snapshot.active)
            self.assertTrue(snapshot.source.isNull());self.assertTrue(snapshot.target.isNull())
            self.assertEqual(tile.refs,());self.assertFalse(tile._card_art)
            self.assertFalse(tile._exit_from);self.assertFalse(tile._exit_to)
            self.assertEqual(tile._kind,'empty')
            self.assertEqual(self.shelf.display_refs(),[])
            self.assertIsNone(self.shelf.popup.preview.current_path)
            self.assertFalse(self.shelf.popup.item_transitions.ghosts)

    def test_hover_started_before_files_arrive_retargets_the_existing_stack_spring(self):
        tile=self.shelf.zones['permanent'].icon;tile.motion=True;tile.spread.motion=True
        with patch('native_ui_common.reduced_motion',return_value=False):
            tile.set_stack(self.shelf.content,[]);tile.set_hover(True)
            self.assertTrue(tile._hovered);self.assertEqual(tile.spread._targets['spread'],0)
            tile.set_stack(self.shelf.content,self.fixture.paths[:3])
            tile.set_hover(True)
            self.assertEqual(tile.spread._targets['spread'],1)
            tile.set_hover(False);self.assertEqual(tile.spread._targets['spread'],0)

    def test_reopening_beneath_stationary_pointer_recovers_a_missing_native_enter(self):
        zone=self.shelf.zones['permanent'];tile=zone.icon;tile.motion=True;tile.spread.motion=True
        zone.hide();workspace.flush();self.assertFalse(tile._hovered)
        position=zone.mapToGlobal(tile.geometry().center())
        with patch('flickdock.QCursor.pos',return_value=position),patch('native_ui_common.reduced_motion',return_value=False):
            zone.show();workspace.flush()
            self.assertTrue(tile._hovered);self.assertEqual(tile.spread._targets['spread'],1)

    def test_hidden_empty_preview_discards_hover_so_reopening_does_not_start_an_idle_border_loop(self):
        self.shelf.remove_all('permanent');workspace.flush();tile=self.shelf.zones['permanent'].icon
        tile.set_hover(True);self.assertEqual(tile.border_motion.animation.state(),tile.border_motion.animation.Running)
        tile.hide();workspace.flush();self.assertEqual(tile.border_motion.animation.state(),tile.border_motion.animation.Stopped)
        tile.show();workspace.flush()
        self.assertFalse(tile._hovered);self.assertFalse(tile.border_motion.active)
        self.assertEqual(tile.border_motion.animation.state(),tile.border_motion.animation.Stopped)

    def test_group_actions_send_all_selected_and_remove_only_shelf_entries(self):
        popup=self.fixture.overview();refs=self.fixture.paths[:2]
        popup.select_item(refs[0]);popup.select_item(refs[1],toggle=True);workspace.flush()
        preview=popup.preview
        self.assertTrue(preview.group_view.isVisible())
        self.assertEqual(preview.group_zip._reference_style,preview.group_copy._reference_style)
        self.assertNotEqual(preview.group_send._reference_style,preview.group_copy._reference_style)
        self.assertNotEqual(preview.group_remove._reference_style,preview.group_copy._reference_style)
        preview.group_send.click()
        self.assertEqual(self.fixture.events,[('lan',refs)])
        preview.group_remove.click();workspace.flush()
        self.assertEqual(self.shelf.files['permanent'],self.fixture.paths[2:])
        self.assertTrue(all(Path(ref).read_bytes()==f'original {index}'.encode()
                            for index,ref in enumerate(self.fixture.paths)))

    def test_both_item_views_offer_lan_and_remote_actions_for_entire_selection(self):
        popup=self.fixture.overview();refs=self.fixture.paths[:2]
        for mode in ('list','icons'):
            popup.change_view_mode(mode);workspace.flush()
            workspace.until(lambda:popup._build_cursor==len(popup.visible_refs()))
            popup.select_item(refs[0]);popup.select_item(refs[1],toggle=True)
            for title,route in (('使用局域网传输','lan'),('使用远程传输','wan')):
                def execute(menu,_position):
                    action=next(action for action in menu.actions() if action.text()==title)
                    self.assertTrue(action.isEnabled());action.trigger()
                with patch('shelf_ui_common.AnimatedMenu.exec',new=execute):
                    popup.show_item_menu(refs[0],QPoint())
                self.assertEqual(self.fixture.events[-1],(route,refs))
            popup.selected.clear();popup.primary_selection=None

    def test_native_body_double_click_opens_once_and_keeps_the_file_selected_in_both_views(self):
        popup=self.fixture.overview();ref=self.fixture.paths[0]
        with patch('PySide6.QtGui.QDesktopServices.openUrl',return_value=True) as opened:
            for mode in ('list','icons'):
                popup.change_view_mode(mode);workspace.flush()
                workspace.until(lambda:popup._build_cursor==len(popup.visible_refs()))
                popup.selected.clear();popup.primary_selection=None;popup.restore_selected_preview()
                item=popup._item_widgets[ref];point=item.mapTo(self.shelf,item.rect().center())
                QTest.mouseClick(self.shelf.windowHandle(),Qt.LeftButton,pos=point);workspace.flush()
                self.assertIn(ref,popup.selected);opened.assert_not_called()
                QTest.mouseDClick(self.shelf.windowHandle(),Qt.LeftButton,pos=point)
                QTest.mouseRelease(self.shelf.windowHandle(),Qt.LeftButton,pos=point);workspace.flush()
                opened.assert_called_once()
                self.assertEqual(Path(opened.call_args.args[0].toLocalFile()),Path(ref).resolve())
                self.assertIn(ref,popup.selected);self.assertEqual(popup.preview.current_path,ref)
                opened.reset_mock()
                QTest.mouseDClick(item,Qt.RightButton,pos=item.rect().center())
                QTest.mouseRelease(item,Qt.RightButton,pos=item.rect().center());workspace.flush()
                opened.assert_not_called()

    def setUp(self):
        self.fixture=workspace.WorkspaceTests();self.fixture.setUp()
        self.fixture.patches.enter_context(patch('native_ui_common.reduced_motion',return_value=False))
        self.shelf=self.fixture.shelf

    def tearDown(self):self.fixture.tearDown()

    def test_overview_requires_selection_and_never_reveals_old_preview_on_reopen(self):
        for mode in ('list','icons'):
            self.fixture.settings.update(view_mode=mode)
            popup=self.fixture.overview();preview=popup.preview
            self.assertIsNone(preview.current_path)
            self.assertIs(preview.stack.currentWidget(),preview.empty_preview)
            ref=self.fixture.paths[0];item=popup._item_widgets[ref]
            with patch.object(preview,'show_path',wraps=preview.show_path) as show:
                point=QPointF(item.rect().center())
                item.enterEvent(QEnterEvent(point,point,point));item.leaveEvent(QEvent(QEvent.Leave))
                workspace.flush();show.assert_not_called()
            popup.select_item(ref)
            self.assertEqual(preview.current_path,ref)
            self.assertEqual(preview.text.toPlainText(),'original 0')
            self.shelf.collapse_contents();self.shelf.geometry_morph.complete()
            self.fixture.overview();workspace.flush()
            self.assertIsNone(preview.current_path)
            self.assertIs(preview.stack.currentWidget(),preview.empty_preview)
            preview.resize(preview.width()+1,preview.height());workspace.flush()
            self.assertEqual(preview.name.full_text,'');self.assertEqual(preview.path_label.full_text,'')
            self.assertEqual(preview.text.toPlainText(),'');self.assertFalse(preview.copy_button.isEnabled())
            self.assertTrue(all(not button.isEnabled() for button in preview.action_buttons))

    def test_deselect_releases_media_and_late_image_cannot_replace_empty_preview(self):
        popup=self.fixture.overview();preview=popup.preview
        path=self.fixture.base/'movie.mp4';path.write_bytes(b'decoder fixture');ref=str(path)
        self.shelf.add_paths('permanent',[ref]);workspace.flush()
        with patch.object(preview.media,'_load_player',return_value=True):popup.select_item(ref)
        self.assertEqual(preview.media.path,ref)
        popup.select_item(ref,toggle=True)
        self.assertEqual(preview.media.path,'');self.assertIsNone(preview.media.player)
        self.assertTrue(preview.media.surface.image.isNull())
        preview._image_source_ready(self.fixture.paths[0],True)
        self.assertIs(preview.stack.currentWidget(),preview.empty_preview)
        self.assertIsNone(preview.current_path)

    def test_empty_shelf_expands_without_reusing_previous_selection_or_image(self):
        popup=self.fixture.overview();popup.select_item(self.fixture.paths[0])
        self.shelf.remove_all('permanent');workspace.flush()
        self.shelf.collapse_contents();self.shelf.geometry_morph.complete()
        self.shelf.manager.motion=True;self.shelf.geometry_morph.motion=True
        self.shelf.show_contents();self.shelf.geometry_morph.pause()
        snapshot=self.shelf._surface_snapshot
        self.assertLess(snapshot.source.deviceIndependentSize().width(),snapshot.target.deviceIndependentSize().width())
        for progress in (0.,.08,.3,.6,1.):
            self.shelf.geometry_morph._frame(progress);workspace.flush()
            self.assertIsNone(popup.preview.current_path)
            self.assertEqual(popup.preview.text.toPlainText(),'')
            self.assertEqual(popup.preview.media.path,'')
            self.assertTrue(popup.empty_state.isVisible())
        self.shelf.geometry_morph.complete()
        self.assertFalse(snapshot.active);self.assertTrue(snapshot.source.isNull())

    def test_moving_container_never_scales_compact_content_and_discards_it_early(self):
        from surface_morph import SurfaceSnapshot
        snapshot=SurfaceSnapshot(self.shelf)
        try:
            for ratio in (1.,1.25,2.):
                source=QPixmap(round(100*ratio),round(80*ratio));source.setDevicePixelRatio(ratio);source.fill(QColor('#202024'))
                painter=QPainter(source);painter.fillRect(QRectF(10,20,20,20),QColor('#ff0033'));painter.end()
                target=QPixmap(round(400*ratio),round(300*ratio));target.setDevicePixelRatio(ratio);target.fill(QColor('#2040aa'))
                snapshot.source=source;snapshot.target=target
                image=QImage(round(300*ratio),round(240*ratio),QImage.Format_ARGB32_Premultiplied);image.setDevicePixelRatio(ratio)
                def render(progress):
                    snapshot.mix=progress;image.fill(Qt.transparent);painter=QPainter(image)
                    snapshot.paint_frame(painter,QRectF(0,0,300,240));painter.end()
                render(.06)
                marker=image.pixelColor(round(215*ratio),round(25*ratio))
                stretched=image.pixelColor(round(40*ratio),round(75*ratio))
                self.assertGreater(marker.red(),140);self.assertLess(stretched.red(),70)
                # Once overview sections have entered, no compact text/card
                # pixels remain, even though the container is still moving.
                render(.8)
                self.assertEqual(image.pixelColor(round(215*ratio),round(25*ratio)),QColor('#2040aa'))
                self.assertEqual(image.pixelColor(round(40*ratio),round(75*ratio)),QColor('#2040aa'))
                snapshot.target_sections=[(QRectF(0,0,400,300),QPoint())]
                image.fill(Qt.transparent);painter=QPainter(image)
                snapshot.paint_frame(painter,QRectF(0,0,260,200));painter.end()
                self.assertEqual(image.pixelColor(round(280*ratio),round(80*ratio)).alpha(),0)
                self.assertEqual(image.pixelColor(round(180*ratio),round(220*ratio)).alpha(),0)
                snapshot.target_sections=[]
        finally:snapshot.deleteLater();workspace.flush()

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

    def test_native_text_card_never_requests_disk_existence_metadata(self):
        content=self.shelf.content;ref=content.add_text('Native text is an item, never a disk pathname')
        tile=self.shelf.zones['permanent'].icon
        with patch.object(self.shelf.file_metadata,'request',side_effect=AssertionError('Virtual text entered missing-file cleanup')):
            tile.set_stack(content,[ref],self.shelf.preview_cache)
            tile.grab()
        self.assertEqual(tile.refs,(ref,))

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

    def test_unplayed_seek_track_remains_visible_in_both_themes(self):
        from media_preview import MediaPreview
        import ui_theme
        view=MediaPreview(motion=False);view.resize(380,260);view.show()
        try:
            for theme in ('dark','light'):
                ui_theme.set_theme(theme);view.apply_theme()
                slider=view.seek_slider;slider.setRange(0,100);slider.setValue(25);slider.setEnabled(True);workspace.flush()
                image=slider.grab().toImage();ratio=image.devicePixelRatio()
                pixel=image.pixelColor(round(slider.width()*.8*ratio),round(slider.height()*.5*ratio))
                self.assertEqual(pixel.alpha(),255);self.assertEqual(pixel.name(),ui_theme.color('media_track'))
        finally:view.close();view.deleteLater();ui_theme.set_theme('dark');workspace.flush()

    def test_upload_only_uses_top_progress_and_keeps_cancel_and_receive_feedback(self):
        popup=self.fixture.overview();ref=self.fixture.paths[1];popup.select_item(ref)
        preview=popup.preview
        self.shelf.manager.sync=SyncView(download_transfers={},upload_time_for_ref=lambda _:None,
            sync_status_for_ref=lambda _:'local',sync_statuses_for_refs=lambda refs:{ref:'local' for ref in refs},
            is_upload_paused=lambda _:False)
        state=dict(id='preview-transfer',phase='uploading',received=50,total=100,remote=False)
        self.shelf.content.transfers[ref]=state
        for phase in ('queued','validating','uploading','verifying','retrying'):
            state['phase']=phase;preview.update_transfer_status();workspace.flush()
            self.assertFalse(preview.transfer_overlay.isVisible(),phase)
            self.assertTrue(preview.transfer_row.isVisible(),phase)
            self.assertIn('50%',preview.transfer_meta.text(),phase)
            self.assertEqual(preview.cancel_transfer.text(),'取消上传')
        with patch.object(self.shelf,'cancel_transfer') as cancel:
            preview.cancel_transfer.click();cancel.assert_called_once_with(ref)
        state.update(phase='downloading',remote=True)
        self.shelf.manager.sync.download_transfers[state['id']]=object()
        preview.update_transfer_status();workspace.flush()
        self.assertTrue(preview.transfer_overlay.isVisible())
        self.assertEqual(preview.cancel_transfer.text(),'取消下载')
        self.shelf.content.transfers.clear();preview.update_transfer_status();workspace.flush()
        self.assertFalse(preview.transfer_overlay.isVisible());self.assertFalse(preview.transfer_row.isVisible())


if __name__=='__main__':unittest.main(verbosity=2)
