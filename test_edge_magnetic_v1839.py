"""Deterministic model and Qt-event tests; these are not Explorer/Finder tests."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
import math
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QPoint, QPointF, QRect, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDropEvent, QImage
from PySide6.QtWidgets import QApplication

from edge_absorb import EdgeAbsorb, canvas_rect, input_band, target_in_area
from edge_file_drag import EdgeFileDrag
from edge_magnetic_model import DEFAULT_CONFIG, MagneticConfig, MagneticField

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


def flush():
    APP.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


class FieldModelTests(unittest.TestCase):
    def test_idle_does_no_work_and_starts_without_a_flash(self):
        model = MagneticField(seed=5)
        model.step(200)
        self.assertFalse(model.running)
        self.assertEqual(model.particles, [])
        model.follow(500, 149, 0, 1079)
        self.assertEqual(model.state, 'approaching')
        self.assertEqual(model.alpha, 0)
        model.step(.01)
        self.assertLess(model.alpha, .2)

    def test_limited_particles_curve_inwards_then_shrink(self):
        model = MagneticField(seed=6)
        model.follow(550, 8, 0, 1079)
        seen = {}
        curved = False
        for _ in range(600):
            model.step(1/120)
            self.assertLessEqual(len(model.particles), DEFAULT_CONFIG.particle_count)
            for item in model.particles:
                if id(item) in seen:
                    self.assertLessEqual(item.depth, seen[id(item)][1] + .001)
                seen[id(item)] = (item, item.depth)
                curved |= abs(item.vy) > .1
                self.assertTrue(math.isfinite(item.tangent))
        self.assertTrue(curved)
        self.assertGreater(len(model.particles), 3)

    def test_time_based_center_is_consistent_at_30_60_144_hz(self):
        centers = []
        for hz in (30, 60, 144):
            model = MagneticField(seed=10)
            model.follow(250, 20, 0, 1079)
            model.follow(700, 20, 0, 1079)
            for _ in range(hz//2): model.step(1/hz)
            centers.append(model.center)
        self.assertLess(max(centers)-min(centers), .01)
        self.assertGreater(centers[0], 699)

    def test_exit_stops_births_and_finishes_at_wall_time_deadline(self):
        model = MagneticField(seed=12)
        model.follow(500, 10, 0, 1079)
        for _ in range(45): model.step(1/60)
        model.retreat();old = len(model.particles)
        model.step(.1)
        self.assertLessEqual(len(model.particles), old)
        model.step(.16)
        self.assertEqual(model.state, 'idle')
        self.assertEqual(model.particles, [])
        self.assertEqual(model.alpha, 0)

    def test_drop_does_not_respawn_or_flash_and_has_a_bounded_end(self):
        model = MagneticField(seed=15)
        model.follow(500, 0, 0, 1079)
        for _ in range(30): model.step(1/60)
        alpha = model.alpha;count=len(model.particles)
        model.drop();model.step(.1)
        self.assertEqual(model.state, 'dropping')
        self.assertLess(model.contraction, 1)
        self.assertLessEqual(model.alpha, alpha)
        self.assertLessEqual(len(model.particles), count)
        model.step(2)
        self.assertEqual(model.state, 'idle')

    def test_reentry_retargets_the_current_alpha(self):
        model=MagneticField(seed=4)
        model.follow(400,20,0,1079);model.step(.1);model.retreat();model.step(.07)
        previous=model.alpha
        model.follow(550,12,0,1079)
        self.assertEqual(model.alpha,previous)
        self.assertEqual(model.center,400)
        model.step(.02)
        self.assertGreater(model.alpha,previous)
        self.assertLess(model.center,550)

    def test_configuration_is_central_immutable_and_bounded(self):
        with self.assertRaises(ValueError): MagneticConfig(particle_count=1000)
        with self.assertRaises(ValueError): MagneticConfig(glow_color=(1,2,900))
        with self.assertRaises(ValueError): MagneticConfig(particle_speed_px_s=float('nan'))


class FieldGeometryTests(unittest.TestCase):
    def test_mac_accepts_the_rounded_outermost_pixel_without_extending_the_display(self):
        from types import SimpleNamespace
        visual=EdgeAbsorb(config=replace(DEFAULT_CONFIG,edge_position='right'),seed=10)
        try:
            area=APP.primaryScreen().geometry()
            point=QPoint(area.right()+1,area.center().y())
            with patch('edge_absorb.sys',SimpleNamespace(platform='darwin')):
                target=visual.target_at(point)
                self.assertIsNotNone(target)
                self.assertEqual(target.distance,0)
                self.assertEqual(target.point.x(),area.right())
                self.assertIsNone(visual.target_at(point+QPoint(2,0)))
        finally:visual.close();flush()

    def test_all_edges_negative_origins_and_adjacent_screens_stay_inside(self):
        for area in (QRect(0,0,1920,1080),QRect(-2560,-400,2560,1440),QRect(1920,200,1707,960)):
            for edge in ('left','right','top','bottom'):
                for ratio in (0, .5, 1):
                    if edge in ('left','right'):
                        position=QPoint(area.left()+8 if edge=='left' else area.right()-8,round(area.top()+(area.height()-1)*ratio))
                    else:
                        position=QPoint(round(area.left()+(area.width()-1)*ratio),area.top()+8 if edge=='top' else area.bottom()-8)
                    target=target_in_area(position,area,edge,150)
                    self.assertIsNotNone(target)
                    self.assertTrue(area.contains(target.point))
                    tangent=position.y() if edge in ('left','right') else position.x()
                    self.assertTrue(area.contains(canvas_rect(target,tangent)))
                    self.assertTrue(area.contains(input_band(target,150)))
                    self.assertLessEqual(canvas_rect(target,tangent).width()*canvas_rect(target,tangent).height(),160*330)
            self.assertIsNone(target_in_area(area.topLeft()-QPoint(1,0),area,'left',150))

    def test_paint_uses_local_coordinates_and_clears_old_particles(self):
        visual=EdgeAbsorb(config=replace(DEFAULT_CONFIG,edge_position='right'),seed=10)
        try:
            area=QRect(-1920,-200,1920,1080)
            target=target_in_area(QPoint(-8,330),area,'right',150,object())
            visual._follow_target(target);visual._timer.stop()
            for _ in range(50):visual.field.step(1/120)
            visual._update_geometry()
            image=visual.overlay.grab().toImage()
            self.assertGreater(max(image.pixelColor(image.width()-2,y).alpha() for y in range(image.height())),0)
            points=[visual._particle_point(item.depth,item.tangent)-QPointF(visual.overlay.pos()) for item in visual.field.particles]
            self.assertTrue(any(0<=p.x()<image.width() and 0<=p.y()<image.height() for p in points))
            self.assertTrue(visual.overlay.windowFlags() & Qt.WindowTransparentForInput)
            self.assertFalse(visual.overlay.acceptDrops())
            visual.field.clear()
            clean=visual.overlay.grab().toImage()
            self.assertEqual(max(clean.pixelColor(x,y).alpha() for x in range(0,clean.width(),8) for y in range(0,clean.height(),8)),0)
        finally:visual.close();flush()


class NativeBoundaryEventTests(unittest.TestCase):
    """Synthetic Qt events exercise acceptance/lifetime, not OS detection."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-edge-mime-')
        self.file=Path(self.temp.name)/'native-source.txt';self.file.write_bytes(b'synthetic fixture')
        self.visual=EdgeAbsorb(seed=2)
        self.area=QApplication.primaryScreen().geometry()
        self.point=QPoint(self.area.right()-25,self.area.center().y())
        self.pointer=QPoint(self.point)
        self.received=[];self.notes=[]
        self.gate=EdgeFileDrag(self.visual,lambda mime,pos:self.received.append((mime.urls(),QPoint(pos))) or True,
            pointer=lambda:QPoint(self.pointer),note=self.notes.append,movement_threshold=8)
        self.mime=QMimeData();self.mime.setUrls([QUrl.fromLocalFile(str(self.file))])

    def tearDown(self):
        self.gate.close();self.visual.close();flush();self.temp.cleanup()

    def arm(self):
        self.gate.update_pointer(self.point-QPoint(90,0),True)
        self.gate.update_pointer(self.point,True)
        self.assertTrue(self.gate.armed)

    def enter(self,mime=None):
        event=QDragEnterEvent(self.gate.window.mapFromGlobal(self.point),Qt.CopyAction,mime or self.mime,Qt.LeftButton,Qt.NoModifier)
        QApplication.sendEvent(self.gate.window,event)
        return event

    def test_long_hold_or_mouse_motion_alone_never_lights(self):
        for _ in range(100):self.gate.update_pointer(self.point,True)
        self.assertFalse(self.gate.armed)
        self.assertEqual(self.visual.state,'idle')
        self.arm()
        self.assertEqual(self.visual.state,'idle')
        self.assertFalse(self.visual._timer.isActive())

    def test_only_local_file_mime_lights_not_text_image_or_remote_link(self):
        for kind in ('text','image','url'):
            self.gate.cancel();self.arm();mime=QMimeData()
            if kind=='text':mime.setText('some selected text')
            elif kind=='image':mime.setImageData(QImage(2,2,QImage.Format_ARGB32))
            else:mime.setUrls([QUrl('https://example.invalid/file.png')])
            self.assertFalse(self.enter(mime).isAccepted());flush()
            self.assertEqual(self.visual.state,'idle')
        self.gate.cancel();self.arm()
        self.assertTrue(self.enter().isAccepted())
        self.assertTrue(self.gate.validated)
        self.assertIn(self.visual.state,('active','approaching'))

    def test_drop_uses_native_event_payload_once_after_mouse_up_poll(self):
        self.arm();self.enter()
        self.gate.update_pointer(self.point,False)
        self.assertTrue(self.gate.armed)
        drop=QDropEvent(QPointF(self.gate.window.mapFromGlobal(self.point)),Qt.CopyAction,self.mime,Qt.NoButton,Qt.NoModifier)
        self.gate.window.dropEvent(drop)
        self.assertTrue(drop.isAccepted());self.assertEqual(drop.dropAction(),Qt.CopyAction)
        self.assertEqual(len(self.received),1)
        self.assertEqual(Path(self.received[0][0][0].toLocalFile()),self.file)
        self.assertEqual(self.visual.state,'dropping')
        flush();self.assertFalse(self.gate.armed)

    def test_native_cancel_cannot_reuse_file_validation_with_button_held(self):
        self.arm();self.enter()
        QApplication.sendEvent(self.gate.window,QDragLeaveEvent());flush()
        self.assertFalse(self.gate.validated)
        for _ in range(20):self.gate.update_pointer(self.point,True)
        self.assertFalse(self.gate.armed)
        self.assertEqual(self.visual.state,'exiting')

    def test_rejected_receiver_and_exceptions_still_retire_the_input_surface(self):
        self.arm();self.enter();self.gate.on_drop=lambda mime,pos:False
        drop=QDropEvent(QPointF(self.gate.window.mapFromGlobal(self.point)),Qt.CopyAction,self.mime,Qt.LeftButton,Qt.NoModifier)
        self.gate.window.dropEvent(drop);flush()
        self.assertFalse(drop.isAccepted());self.assertFalse(self.gate.armed)
        self.gate.cancel();self.arm();self.enter()
        def fail(mime,pos):raise ValueError('expected fixture failure')
        self.gate.on_drop=fail
        with self.assertRaisesRegex(ValueError,'expected fixture failure'):
            self.gate.window.dropEvent(drop)
        flush();self.assertFalse(self.gate.armed)

    def test_close_is_idempotent_and_invalidates_queued_hide(self):
        self.arm();self.enter();self.gate._defer_stop()
        self.gate.close();self.visual.close();flush()
        self.gate.close();self.visual.close()
        self.assertFalse(self.visual._timer.isActive())

    def test_non_drop_area_is_not_covered_by_native_destination(self):
        self.arm()
        self.assertTrue(self.gate.window.geometry().contains(self.point))
        self.assertFalse(self.gate.window.geometry().contains(self.point-QPoint(200,0)))
        self.assertFalse(self.visual.overlay.acceptDrops())


if __name__=='__main__':unittest.main(verbosity=2)
