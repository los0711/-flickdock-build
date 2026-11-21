"""Four-edge recovery / native-boundary events and true post-drop mesh geometry.

Qt drag events below are synthetic; they are not proof of Explorer/Finder IO.
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')

from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock,patch

from PySide6.QtCore import QCoreApplication,QEvent,QMimeData,QPoint,QPointF,QRect,Qt,QUrl
from PySide6.QtGui import QBitmap,QColor,QDragEnterEvent,QDragLeaveEvent,QDragMoveEvent,QDropEvent,QEnterEvent,QImage,QPainter,QRegion
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from edge_absorb import EdgeAbsorb,target_in_area
from edge_file_drag import EdgeFileDrag
from edge_magnetic_model import DEFAULT_CONFIG

APP=QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)

def flush():
    APP.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)


class FourEdgeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.visual=EdgeAbsorb(seed=8)
        self.area=APP.primaryScreen().geometry()
        self.pointer=QPoint(self.area.center())
        self.received=[]
        self.gate=EdgeFileDrag(self.visual,lambda mime,pos:self.received.append(mime.urls()) or True,
                               pointer=lambda:QPoint(self.pointer),movement_threshold=8)
        self.mime=QMimeData();self.mime.setUrls([QUrl.fromLocalFile(str(Path('fixture-no-open.mp4').resolve()))])

    def tearDown(self):self.gate.close();self.visual.close();flush()

    def point(self,edge,depth=10):
        a=self.area
        return {'left':QPoint(a.left()+depth,a.center().y()),'right':QPoint(a.right()-depth,a.center().y()),
                'top':QPoint(a.center().x(),a.top()+depth),'bottom':QPoint(a.center().x(),a.bottom()-depth)}[edge]

    def arm(self,edge='right',depth=10):
        self.pointer=self.point(edge,depth)
        self.gate.update_pointer(self.area.center(),False)
        self.gate.update_pointer(self.pointer,True)

    def enter(self,point=None):
        point=point or self.pointer
        event=QDragEnterEvent(self.gate.window.mapFromGlobal(point),Qt.CopyAction,self.mime,Qt.LeftButton,Qt.NoModifier)
        self.gate.window.dragEnterEvent(event)
        return event

    def drop(self):
        event=QDropEvent(QPointF(self.gate.window.mapFromGlobal(self.pointer)),Qt.CopyAction,self.mime,Qt.NoButton,Qt.NoModifier)
        self.gate.window.dropEvent(event);return event

    def test_default_resolves_all_four_edges(self):
        for edge in ('left','right','top','bottom'):
            with self.subTest(edge=edge):self.assertEqual(self.visual.target_at(self.point(edge)).edge,edge)

    def test_fast_first_pressed_sample_retains_idle_movement(self):
        self.arm()
        self.assertTrue(self.gate.armed)
        self.assertEqual(self.visual.state,'idle')
        self.assertTrue(self.enter().isAccepted())

    def test_prepares_perimeter_before_entering_the_visible_band(self):
        self.arm(depth=230)
        self.assertTrue(self.gate.prepared)
        self.assertEqual(self.visual.state,'idle')
        self.assertEqual(len(self.gate._windows),4)
        for edge in ('left','right','top','bottom'):
            self.assertTrue(self.gate._windows[edge].geometry().contains(self.point(edge)))
            self.assertFalse(self.gate._windows[edge].geometry().contains(self.area.center()))

    def test_each_edge_accepts_current_file_mime(self):
        for edge in ('left','right','top','bottom'):
            with self.subTest(edge=edge):
                self.gate.cancel();self.arm(edge)
                self.assertTrue(self.enter().isAccepted())
                self.assertEqual(self.visual.edge,edge)
                self.assertTrue(self.drop().isAccepted());flush()
        self.assertEqual(len(self.received),4)

    def test_leave_requires_fresh_mime_but_does_not_lose_same_drag(self):
        self.arm();self.enter()
        self.gate.window.dragLeaveEvent(QDragLeaveEvent());flush()
        self.assertFalse(self.gate.validated)
        for _ in range(5):self.gate.update_pointer(self.pointer,True)
        self.assertFalse(self.gate.validated)
        self.assertTrue(self.enter().isAccepted())
        self.assertTrue(self.gate.validated)
        self.assertTrue(self.drop().isAccepted())

    def test_drop_can_arrive_after_an_early_mouse_up_poll(self):
        self.arm();self.enter();self.gate.update_pointer(self.pointer,False)
        QTest.qWait(110)
        self.assertTrue(self.drop().isAccepted());flush()
        self.assertEqual(len(self.received),1)

    def test_corner_turn_does_not_move_native_receivers(self):
        self.arm();self.enter()
        before={edge:window.geometry().getRect() for edge,window in self.gate._windows.items()}
        self.pointer=QPoint(self.area.right()-8,self.area.top()+2)
        self.gate.update_pointer(self.pointer,True)
        self.assertTrue(self.enter().isAccepted())
        self.assertEqual(self.visual.edge,'top')
        self.assertEqual(before,{edge:window.geometry().getRect() for edge,window in self.gate._windows.items()})

    def test_one_native_drop_is_received_once_per_press(self):
        self.arm();self.enter()
        self.assertTrue(self.drop().isAccepted())
        self.assertFalse(self.drop().isAccepted())
        flush();self.assertEqual(len(self.received),1)
        self.assertFalse(self.gate.prepared)
        self.gate.update_pointer(self.pointer,True)
        self.assertFalse(self.gate.prepared)

    def test_accepted_drop_is_guarded_if_closed_before_queued_art(self):
        self.arm();self.enter();self.drop()
        self.gate.close();self.visual.close();flush()
        self.assertEqual(len(self.received),1)
        self.assertFalse(self.visual._timer.isActive())
        self.assertIsNone(self.visual.drop_artwork)

    def test_current_move_payload_invalidates_a_stale_queued_hide(self):
        self.arm();self.enter();self.gate._native_leave();self.gate._defer_stop()
        move=QDragMoveEvent(self.gate.window.mapFromGlobal(self.pointer),Qt.CopyAction,self.mime,Qt.LeftButton,Qt.NoModifier)
        self.gate.window.dragMoveEvent(move);flush()
        self.assertTrue(move.isAccepted());self.assertTrue(self.gate.validated)

    def test_drop_receiver_may_close_owner_or_reenter_without_duplicate(self):
        self.arm();self.enter()
        def receive(mime,pos):
            self.received.append(mime.urls())
            self.assertFalse(self.drop().isAccepted())
            self.gate.close();self.visual.close()
            return True
        self.gate.on_drop=receive
        self.assertTrue(self.drop().isAccepted());flush()
        self.assertEqual(len(self.received),1)

    def test_old_hover_handle_does_not_steal_a_prepared_drag(self):
        from dock_handle import DockHandle
        self.arm();self.enter();self.gate._native_leave()
        self.assertTrue(self.gate.prepared);self.assertFalse(self.gate.armed)
        manager=SimpleNamespace(motion=False,edge_suppressed=False,edge_file_drag=self.gate,show_docked=Mock())
        manager.settings=SimpleNamespace(dock_edge='right',dock_position=50,dock_length=160,dock_thickness=8,
            trigger_visible=True,dock_color='#567daf',dock_opacity=54,display_triggers={})
        handle=DockHandle(manager)
        try:
            handle.enterEvent(QEnterEvent(QPointF(),QPointF(),QPointF(self.pointer)))
            manager.show_docked.assert_not_called()
            event=QDragEnterEvent(handle.rect().center(),Qt.CopyAction,self.mime,Qt.LeftButton,Qt.NoModifier)
            handle.dragEnterEvent(event)
            self.assertTrue(event.isAccepted());self.assertTrue(self.gate.validated)
            manager.show_docked.assert_not_called()
            event=QDropEvent(QPointF(handle.rect().center()),Qt.CopyAction,self.mime,Qt.NoButton,Qt.NoModifier)
            handle.dropEvent(event);flush()
            self.assertTrue(event.isAccepted());self.assertEqual(len(self.received),1)
        finally:handle.appearance.stop();handle.deleteLater();flush()


class DropMeshTests(unittest.TestCase):
    def test_closing_neck_has_no_seams_or_unbounded_raster_projection(self):
        from edge_drop_animation import DropArtwork
        marker=QImage(64,64,QImage.Format_ARGB32_Premultiplied);marker.fill(Qt.white)
        area=QRect(0,0,400,300);target=target_in_area(QPoint(399,150),area,'right',160)
        with patch('edge_drop_animation.file_artwork',return_value=marker):art=DropArtwork(target)
        for ratio in (.2,.5,.8,.95):
            with self.subTest(ratio=ratio):
                art.elapsed_ms=DEFAULT_CONFIG.drop_settle_ms+DEFAULT_CONFIG.drop_absorb_ms*ratio
                pose=art.pose
                image=QImage(area.size(),QImage.Format_ARGB32_Premultiplied);image.fill(Qt.transparent)
                painter=QPainter(image);art.paint(painter,QPoint());painter.end()
                rect=QRegion(QBitmap.fromImage(image.createAlphaMask())).boundingRect()
                self.assertGreaterEqual(rect.left(),int(399.5-pose.outer)-2)
                self.assertLessEqual(rect.right(),400)
                self.assertLessEqual(rect.width(),pose.outer-pose.inner+4)
                if ratio==.5:
                    for depth in range(round(pose.inner)+2,round(pose.outer)-2):
                        u=(depth-pose.inner)/(pose.outer-pose.inner)
                        _,center,_=pose.span_at(u)
                        self.assertGreater(image.pixelColor(round(399.5-depth),round(150+center)).alpha(),240)

    def test_flush_edge_release_first_moves_back_then_pinches(self):
        from edge_drop_animation import drop_pose
        a=drop_pose(0,0,800);b=drop_pose(DEFAULT_CONFIG.drop_settle_ms,0,800)
        self.assertAlmostEqual((b.inner+b.outer-a.inner-a.outer)/2,51)
        self.assertEqual(b.neck,0)
        middle=drop_pose(DEFAULT_CONFIG.drop_settle_ms+DEFAULT_CONFIG.drop_absorb_ms*.5,0,800)
        self.assertLess(middle.span_at(.05)[2],middle.span_at(.95)[2]*.2)
        self.assertLess(middle.inner,5)
        self.assertGreater(middle.outer,70)
        self.assertTrue(drop_pose(1000,0,800).done)

    def test_no_io_for_art_even_for_videos_and_missing_sources(self):
        from edge_drop_animation import file_artwork
        urls=[QUrl.fromLocalFile(str(Path('nonexistent-video.mp4').resolve()))]
        with patch.object(Path,'open',side_effect=AssertionError('must not read a file')), \
             patch.object(Path,'stat',side_effect=AssertionError('must not stat a file')):
            image=file_artwork(urls)
        self.assertFalse(image.isNull())

    def test_art_is_upright_on_every_edge(self):
        from edge_drop_animation import DropArtwork
        marker=QImage(64,64,QImage.Format_ARGB32_Premultiplied)
        p=QPainter(marker)
        for rect,color in ((QRect(0,0,32,32),'red'),(QRect(32,0,32,32),'green'),
                           (QRect(0,32,32,32),'blue'),(QRect(32,32,32,32),'yellow')):p.fillRect(rect,QColor(color))
        p.end();area=QRect(0,0,400,300)
        for edge,point in (('left',QPoint(0,150)),('right',QPoint(399,150)),('top',QPoint(200,0)),('bottom',QPoint(200,299))):
            with self.subTest(edge=edge),patch('edge_drop_animation.file_artwork',return_value=marker):
                target=target_in_area(point,area,edge,160)
                art=DropArtwork(target);pose=art.pose
                image=QImage(area.size(),QImage.Format_ARGB32_Premultiplied);image.fill(Qt.transparent)
                p=QPainter(image);art.paint(p,QPoint());p.end()
                if edge=='left':x,y=area.left()+.5+pose.inner,art.tangent-pose.half_height
                elif edge=='right':x,y=area.right()+.5-pose.outer,art.tangent-pose.half_height
                elif edge=='top':x,y=art.tangent-pose.half_height,area.top()+.5+pose.inner
                else:x,y=art.tangent-pose.half_height,area.bottom()+.5-pose.outer
                for dx,dy,color in ((10,10,'red'),(54,10,'green'),(10,54,'blue'),(54,54,'yellow')):
                    self.assertEqual(image.pixelColor(round(x+dx),round(y+dy)),QColor(color))

    def test_long_delayed_frame_retires_image_and_clock(self):
        visual=EdgeAbsorb(seed=5)
        try:
            area=APP.primaryScreen().geometry();point=QPoint(area.right(),area.center().y())
            visual.release(point);self.assertIsNotNone(visual.drop_artwork)
            visual._last=time.perf_counter()-5;visual._tick()
            self.assertEqual(visual.state,'idle');self.assertIsNone(visual.drop_artwork)
            self.assertFalse(visual.overlay.isVisible());self.assertFalse(visual._timer.isActive())
        finally:visual.close();flush()


if __name__=='__main__':unittest.main(verbosity=2)
