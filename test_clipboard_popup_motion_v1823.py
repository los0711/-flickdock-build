"""Real clipboard preview widgets, isolated clipboard and fixed-host pixels."""
import unittest
from unittest.mock import patch
from PySide6.QtCore import QPoint,QRect,QRectF,QSize,Qt,QMimeData,QVariantAnimation,QEvent
from PySide6.QtGui import QImage,QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton,QApplication,QMenu
import test_html_native_workspace as ws
from test_clipboard_preview_v1821 import PreviewTests
from flickdock import ShelfManager


class ClipboardPopupTests(unittest.TestCase):
    def setUp(self):
        shared=patch('native_ui_common.reduced_motion',return_value=False)
        shared.start();self.addCleanup(shared.stop)
        self.fixture=PreviewTests('test_each_failed_retry_has_error_state_but_notice_remains_deduplicated');self.fixture.setUp()
        self.manager=self.fixture.manager;self.shelf=self.fixture.shelf;self.manager.motion=True
        self.manager.show_clipboard_preview=lambda source=None:ShelfManager.show_clipboard_preview(self.manager,source)
        self.motion=patch('clipboard_popup_motion.motion_allowed',return_value=True);self.motion.start()
        self.fixture.clip.text('original clipboard text 中文');ws.until(lambda:self.fixture.model.state.get('phase')=='synced')
    def tearDown(self):
        dialog=self.manager.clipboard_preview_dialog
        if dialog is not None:dialog.dismiss(immediate=True)
        self.motion.stop();self.fixture.tearDown()
    def open(self,overview=False):
        source=self.fixture.overview().clipboard_button if overview else self.shelf.zones['permanent'].clipboard_button
        source.click();dialog=self.manager.clipboard_preview_dialog
        self.assertTrue(dialog.isVisible());return source,dialog,dialog._popup_motion
    def land(self,motion):motion.animation.setCurrentTime(motion.animation.duration());ws.flush()

    def test_real_width_note_hint_is_compact_and_resets_after_narrow_open(self):
        source,dialog,motion=self.open();dialog.dismiss(immediate=True)
        note=dialog.sync_limit_note;natural_width=note.sizeHint().width()
        # Match observed Mac natural wrapping heuristic versus real-width
        # height, without depending on the host platform's font fallback.
        with patch.object(note,'sizeHint',new=lambda:QSize(natural_width,64)), \
             patch.object(note,'heightForWidth',new=lambda width:32):
            dialog.status.setMinimumHeight(17)
            note.updateGeometry();dialog.status.updateGeometry();dialog.body_layout.invalidate()
            for stage,area,expected_width in (
                    ('normal',QRect(0,0,1200,900),420),
                    ('narrow',QRect(0,0,360,480),304),
                    ('normal-again',QRect(0,0,1200,900),420)):
                with self.subTest(stage=stage),patch.object(motion,'available',return_value=area):
                    try:
                        dialog.open_from(source)
                        host=QRect(dialog.geometry());surface=dialog.surface.size();native=dialog.winId()
                        self.land(motion)
                        # These assertions also prove the instance virtual-method
                        # treatment reached Qt's actual layout cache, not only a
                        # Python call that QWidgetItem ignored.
                        self.assertEqual(note.sizeHint().height(),64)
                        self.assertEqual(note.heightForWidth(note.width()),32)
                        self.assertGreater(dialog.body_layout.sizeHint().height(),330)
                        self.assertEqual(dialog.body_layout.totalHeightForWidth(dialog.surface.width()),329)
                        self.assertLess(motion.target_rect.height(),330)
                        self.assertEqual(motion.target_rect.width(),expected_width)
                        self.assertEqual(dialog.surface.width(),expected_width)
                        viewport=dialog.scroll.viewport()
                        self.assertTrue(viewport.rect().contains(QRect(note.mapTo(viewport,QPoint()),note.size())))
                        self.assertGreaterEqual(note.height(),note.heightForWidth(note.width()))
                        self.assertEqual(dialog.scroll.verticalScrollBar().maximum(),0)
                        self.assertEqual(dialog.geometry(),host);self.assertEqual(dialog.surface.size(),surface)
                        self.assertEqual(dialog.winId(),native)
                        self.assertEqual(dialog.body_layout.getContentsMargins(),(20,20,20,20))
                        self.assertEqual(dialog.body_layout.spacing(),16)
                    finally:
                        dialog.dismiss(immediate=True);ws.flush()

    def test_sync_limit_note_fits_and_click_closes_without_upload_or_host_action(self):
        for overview in (False,True):
            source,dialog,motion=self.open(overview=overview);self.land(motion)
            note=dialog.sync_limit_note
            self.assertIn('100 MB 以内',note.text());self.assertIn('不会同步给其他设备',note.text())
            self.assertTrue(note.isVisible());self.assertTrue(note.wordWrap())
            self.assertEqual(note.textInteractionFlags(),Qt.NoTextInteraction)
            rect=QRect(note.mapTo(dialog.surface,QPoint()),note.size())
            self.assertTrue(dialog.surface.rect().contains(rect))
            writes=[call for call in self.fixture.hub.calls if call[0]!='GET']
            QTest.mouseClick(note,Qt.LeftButton);self.assertEqual(motion.target,0)
            self.land(motion);self.assertFalse(dialog.isVisible())
            self.assertEqual([call for call in self.fixture.hub.calls if call[0]!='GET'],writes)
            self.assertEqual(self.fixture.clip.mimeData().text(),'original clipboard text 中文')

    def test_real_source_to_content_pixels_and_fixed_native_geometry(self):
        source,dialog,motion=self.open();geometry=QRect(dialog.geometry());native=dialog.winId()
        self.assertEqual(motion.source_rect,QRectF(QRect(source.mapToGlobal(QPoint()),source.size())))
        self.assertFalse(dialog.surface.isVisible());self.assertIsNone(dialog.graphicsEffect())
        samples=[]
        for time in (0,60,130,230):
            motion.animation.setCurrentTime(time);ws.flush()
            samples.append(dialog.grab().toImage())
            self.assertEqual(dialog.geometry(),geometry);self.assertEqual(dialog.winId(),native)
        self.assertNotEqual(samples[0],samples[1]);self.assertNotEqual(samples[1],samples[2])
        self.assertTrue(dialog.surface.isVisible());self.assertIs(dialog.content.currentWidget(),dialog.text)
        self.assertEqual(dialog.text.toPlainText(),'original clipboard text 中文')
        self.assertTrue(motion.source_image.isNull());self.assertTrue(motion.target_image.isNull())
        dialog.close_button.click();self.assertTrue(motion.presenting);self.assertEqual(motion.target,0)
        motion.animation.setCurrentTime(80);self.assertEqual(dialog.geometry(),geometry)
        self.land(motion);self.assertFalse(dialog.isVisible());self.assertFalse(motion._app_filter)
        self.assertEqual(self.fixture.clip.mimeData().text(),'original clipboard text 中文')

    def test_fast_reverse_reuses_current_pose_and_does_not_reallocate_host(self):
        source,dialog,motion=self.open();geometry=QRect(dialog.geometry())
        motion.animation.setCurrentTime(95);before=motion.current_rect();image=dialog.grab().toImage()
        source.click();self.assertEqual(motion.target,0);self.assertEqual(motion.current_rect(),before)
        self.assertEqual(dialog.grab().toImage(),image)
        motion.animation.setCurrentTime(40);before=motion.current_rect();image=dialog.grab().toImage()
        source.click();self.assertEqual(motion.target,1);self.assertEqual(motion.current_rect(),before)
        self.assertEqual(dialog.grab().toImage(),image);self.assertEqual(dialog.geometry(),geometry)
        self.land(motion);self.assertTrue(dialog.surface.isVisible())

    def test_overview_state_preserved_live_updates_and_blank_click_keeps_original_action(self):
        source,dialog,motion=self.open(overview=True);self.land(motion)
        popup=self.shelf.popup;scroll=popup.scroll.verticalScrollBar().value();selected=set(popup.selected)
        self.assertIs(dialog.owner_window,self.shelf)
        self.assertTrue(self.shelf.hover_region_contains(motion.target_rect.center().toPoint()))
        self.fixture.clip.text('fresh while viewing');ws.until(lambda:dialog.text.toPlainText()=='fresh while viewing')
        self.assertEqual(popup.scroll.verticalScrollBar().value(),scroll);self.assertEqual(popup.selected,selected);self.assertTrue(self.shelf.expanded)
        # A real blank host click closes; no modal/global hook is involved.
        point=QPoint(1,1)
        self.assertFalse(motion.current_rect().contains(dialog.mapToGlobal(point)))
        QTest.mouseClick(dialog,Qt.LeftButton,pos=point);self.assertEqual(motion.target,0)
        self.land(motion);self.assertFalse(dialog.isVisible());self.assertTrue(self.shelf.expanded)
        dialog.open_from(source);self.land(motion)
        action=QPushButton('action',self.shelf);action.move(self.shelf.mapFromGlobal(motion.host_rect.topLeft()+point));action.resize(20,20)
        action.show();clicked=[];action.clicked.connect(lambda:clicked.append(True))
        QTest.mouseClick(action,Qt.LeftButton);self.assertEqual(clicked,[True]);self.assertEqual(motion.target,0)
        self.land(motion);action.deleteLater();ws.flush()

    def test_owner_hide_source_hide_and_destruction_cancel_all_motion(self):
        source,dialog,motion=self.open();motion.animation.setCurrentTime(80)
        self.shelf.hide_immediately();ws.flush();self.assertFalse(dialog.isVisible());self.assertFalse(motion.presenting)
        self.shelf.show();ws.flush();source.show();dialog.open_from(source);self.land(motion)
        source.hide();ws.flush();self.assertFalse(dialog.isVisible());self.assertFalse(motion.animation.state()==motion.animation.State.Running)
        source.show();spare=QPushButton('clip',self.shelf);spare.setGeometry(source.geometry());spare.show()
        dialog.open_from(spare);self.land(motion);spare.deleteLater();ws.flush()
        self.assertFalse(dialog.isVisible());self.assertIsNone(dialog.owner_window);self.assertFalse(motion._app_filter)

    def test_small_screen_clamp_and_reduced_motion_still_shows_real_content(self):
        source=self.shelf.zones['permanent'].clipboard_button
        self.manager.show_clipboard_preview(source);dialog=self.manager.clipboard_preview_dialog;dialog.dismiss(immediate=True)
        motion=dialog._popup_motion
        area=QRect(0,0,480,360)
        with patch.object(motion,'available',return_value=area):dialog.open_from(source)
        self.land(motion);self.assertTrue(area.contains(motion.target_rect.toAlignedRect()))
        self.assertTrue(area.contains(dialog.geometry()));self.assertTrue(dialog.scroll.isVisible())
        note=dialog.sync_limit_note;viewport=dialog.scroll.viewport()
        self.assertTrue(viewport.rect().contains(QRect(note.mapTo(viewport,QPoint()),note.size())))
        self.assertGreaterEqual(note.height(),note.heightForWidth(note.width()))
        self.assertEqual(dialog.scroll.verticalScrollBar().maximum(),0)
        dialog.dismiss(immediate=True)
        with patch('clipboard_popup_motion.motion_allowed',return_value=False):
            dialog.open_from(source);self.assertTrue(dialog.surface.isVisible());self.assertFalse(motion.transitioning)
            self.assertEqual(motion.progress,1);dialog.close_button.click();self.assertFalse(dialog.isVisible())

    def test_image_and_status_refresh_during_expansion_are_real_and_cancel_on_owner_move(self):
        source,dialog,motion=self.open();motion.animation.setCurrentTime(80);before=motion.target_image.toImage()
        self.fixture.client._set_view(('text',{'text':'new snapshot body','html':''}),'error','next')
        ws.flush();self.assertEqual(dialog.text.toPlainText(),'new snapshot body');self.assertEqual(dialog.badge.status,'error')
        self.assertNotEqual(motion.target_image.toImage(),before)
        self.land(motion);self.shelf.move(self.shelf.pos()+QPoint(10,10));ws.flush()
        self.assertFalse(dialog.isVisible());self.assertFalse(motion.presenting)

    def test_original_image_preview_and_endpoint_content_pixels_in_both_themes(self):
        image=QImage(120,80,QImage.Format_RGB32);image.fill(QColor('#2679ab'))
        path=self.fixture.base/'motion-original.png';image.save(str(path),'PNG');original=path.read_bytes()
        mime=QMimeData();mime.setImageData(image);mime.setData('image/png',original);self.fixture.clip.setMimeData(mime)
        ws.until(lambda:self.fixture.model.state.get('phase')=='synced' and not self.fixture.model.thumbnail.isNull())
        for theme in ('dark','light'):
            self.manager.settings.update(theme=theme)
            source,dialog,motion=self.open();motion.animation.pause();motion.animation.setCurrentTime(80)
            geometry=QRect(dialog.geometry());surface_size=dialog.surface.size();native=dialog.winId()
            ws.until(lambda:not self.fixture.model.large.isNull())
            self.assertIs(dialog.content.currentWidget(),dialog.image)
            self.assertEqual(dialog.image.image.pixelColor(30,30),image.pixelColor(30,30))
            motion.animation.stop();motion._frame(1.);snapshot=dialog.grab().toImage()
            motion._finished();ws.flush();live=dialog.grab().toImage()
            # Inner content must be identical at the cached→live handoff.
            dpr=dialog.devicePixelRatioF();rect=dialog.surface.geometry().adjusted(12,12,-12,-12)
            crop=QRect(round(rect.x()*dpr),round(rect.y()*dpr),round(rect.width()*dpr),round(rect.height()*dpr))
            self.assertEqual(snapshot.copy(crop),live.copy(crop))
            self.assertEqual(dialog.geometry(),geometry);self.assertEqual(dialog.surface.size(),surface_size)
            self.assertEqual(dialog.winId(),native);self.assertEqual(dialog.scroll.verticalScrollBar().maximum(),0)
            note_rect=QRect(dialog.sync_limit_note.mapTo(dialog.scroll.viewport(),QPoint()),dialog.sync_limit_note.size())
            self.assertTrue(dialog.scroll.viewport().rect().contains(note_rect))
            self.assertEqual(bytes(self.fixture.clip.mimeData().data('image/png')),original)
            self.assertEqual(path.read_bytes(),original);dialog.dismiss(immediate=True)

    def test_repeated_reopen_and_live_reduced_motion_leave_one_clock_no_effect(self):
        source,dialog,motion=self.open();native=dialog.winId()
        for _ in range(20):
            motion.animation.setCurrentTime(50);dialog.dismiss();dialog.open_from(source);self.land(motion)
            self.assertEqual(dialog.winId(),native);self.assertEqual(len(motion.findChildren(QVariantAnimation)),1)
            self.assertIsNone(dialog.graphicsEffect());self.assertEqual(len(motion._anchors),2)
        before=dialog.grab().toImage()
        dialog.open_from(source);self.assertFalse(motion.transitioning);self.assertTrue(dialog.surface.isVisible())
        self.assertEqual(dialog.grab().toImage(),before)
        dialog.dismiss(immediate=True);dialog.open_from(source)
        with patch('clipboard_popup_motion.motion_allowed',return_value=False):motion._frame(.5)
        self.assertTrue(dialog.surface.isVisible());self.assertFalse(motion.transitioning)
        dialog.dismiss(immediate=True);self.assertEqual(motion._anchors,[]);self.assertFalse(motion._app_filter)

    def test_local_deactivate_closes_pinned_popup_but_new_intent_and_owned_menu_win(self):
        source,dialog,motion=self.open();self.land(motion)
        with patch.object(QApplication,'activeWindow',return_value=self.shelf):
            QApplication.sendEvent(dialog,QEvent(QEvent.WindowDeactivate));dialog.open_from(source);ws.flush()
            self.assertEqual(motion.target,1);self.assertTrue(dialog.surface.isVisible())
            menu=QMenu(dialog.text)
            with patch.object(QApplication,'activePopupWidget',return_value=menu):
                QApplication.sendEvent(dialog,QEvent(QEvent.WindowDeactivate));ws.flush();self.assertEqual(motion.target,1)
            menu.deleteLater();ws.flush()
            with patch.object(QApplication,'activePopupWidget',return_value=None):
                QApplication.sendEvent(dialog,QEvent(QEvent.WindowDeactivate));ws.flush();self.assertEqual(motion.target,0)
        self.land(motion);self.assertFalse(dialog.isVisible());self.assertTrue(self.shelf.isVisible());self.assertTrue(self.shelf.pinned)

    def test_geometry_rebound_is_small_stays_unclipped_and_lands_at_exact_target(self):
        source,dialog,motion=self.open();dialog.dismiss(immediate=True)
        # Both available screen rectangles contain the actually visible source.
        # A fake viewport excluding that source would clip it before any motion.
        for area in (QRect(0,0,1280,900),QRect(0,60,480,360)):
            with patch.object(motion,'available',return_value=area):dialog.open_from(source)
            motion.animation.pause();geometry=QRect(dialog.geometry());native=dialog.winId();widths=[]
            for progress in (0.,.25,.5,.65,.7,.75,7/9,.8,.9,.95,1.):
                motion._frame(progress);rect=motion.current_rect();widths.append(rect.width())
                self.assertTrue(QRectF(geometry).contains(rect),(area.getRect(),progress,geometry.getRect(),rect.getRect(),motion.source_rect.getRect()))
                self.assertTrue(QRectF(area).contains(rect))
                self.assertEqual(dialog.geometry(),geometry);self.assertEqual(dialog.winId(),native)
                self.assertGreaterEqual(motion.progress,0);self.assertLessEqual(motion.progress,1)
                pixels=dialog.grab().toImage();width,height=pixels.width(),pixels.height()
                edge_alpha=max(max(pixels.pixelColor(x,0).alpha(),pixels.pixelColor(x,height-1).alpha()) for x in range(width))
                edge_alpha=max(edge_alpha,max(max(pixels.pixelColor(0,y).alpha(),pixels.pixelColor(width-1,y).alpha()) for y in range(height)))
                self.assertLessEqual(edge_alpha,2,(area.getRect(),progress,edge_alpha))
            overshoot=max(widths)/motion.target_rect.width()-1
            self.assertGreater(overshoot,.005);self.assertLess(overshoot,.012)
            self.assertGreater(max(widths),widths[-1]);self.assertEqual(motion.current_rect(),motion.target_rect)
            motion._finished();dialog.dismiss();self.land(motion)
            self.assertEqual(motion.current_rect(),motion.source_rect);self.assertFalse(dialog.isVisible())

    def test_reverse_at_actual_rebound_peak_keeps_identical_visible_pixels(self):
        source,dialog,motion=self.open();motion.animation.pause();motion._frame(7/9)
        rect=motion.current_rect();self.assertGreater(rect.width(),motion.target_rect.width())
        before=dialog.grab().toImage();source.click()
        self.assertEqual(motion.target,0);self.assertEqual(motion.current_rect(),rect)
        self.assertEqual(dialog.grab().toImage(),before)
        motion.animation.pause();motion._frame(.7);rect=motion.current_rect();before=dialog.grab().toImage()
        source.click();self.assertEqual(motion.target,1);self.assertEqual(motion.current_rect(),rect)
        self.assertEqual(dialog.grab().toImage(),before);self.land(motion)
        self.assertEqual(motion.current_rect(),motion.target_rect);self.assertTrue(dialog.surface.isVisible())


if __name__=='__main__':unittest.main()
