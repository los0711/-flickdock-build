"""Outer-only shape, bounded real-icon piles, and preview-first native routing."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
from types import SimpleNamespace
import tempfile,time,unittest
from unittest.mock import Mock,patch
from PySide6.QtCore import QCoreApplication,QEvent,QMimeData,QPoint,QPointF,QRect,Qt,QUrl
from PySide6.QtGui import QColor,QDragEnterEvent,QDropEvent,QImage,QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from native_ui_common import (RADIUS_WINDOW_PX,RADIUS_SMALL_PX,RADIUS_SUBCONTROL_PX,
    RADIUS_CONTROL_PX,RADIUS_CARD_PX,RADIUS_PANEL_PX,colors)
from edge_absorb import EdgeAbsorb,target_in_area
from edge_native_icons import NativeIconCache
from edge_drop_animation import DropArtwork,file_stack_artwork
from edge_file_drag import EdgeFileDrag
from surface_morph import surface_clip

APP=QApplication.instance() or QApplication([]);APP.setQuitOnLastWindowClosed(False)
def flush():APP.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
def settle(check):
    deadline=time.monotonic()+3
    while not check() and time.monotonic()<deadline:QTest.qWait(5);flush()
    assert check(),'operation did not settle'
def marker(color):
    image=QImage(64,64,QImage.Format_ARGB32_Premultiplied);image.fill(QColor(color));return image

class OuterOnlyShapeTests(unittest.TestCase):
    def test_compact_empty_label_and_desktop_field_style(self):
        from reference_widgets import COMPACT_EMPTY_LABEL
        from edge_magnetic_model import DESKTOP_MAGNETIC_CONFIG
        self.assertEqual(COMPACT_EMPTY_LABEL,'拖入文件')
        self.assertEqual((DESKTOP_MAGNETIC_CONFIG.glow_width_px,DESKTOP_MAGNETIC_CONFIG.glow_height_px,DESKTOP_MAGNETIC_CONFIG.glow_intensity),(150,360,128))
    def test_inner_radii_are_unchanged(self):
        self.assertEqual((RADIUS_SMALL_PX,RADIUS_SUBCONTROL_PX,RADIUS_CONTROL_PX,RADIUS_CARD_PX,RADIUS_PANEL_PX),(4,6,8,12,16))
    def test_every_window_uses_same_larger_radius_for_both_platforms(self):
        from transfer_workbench import TRANSFER_CORNER_RADIUS_PX
        self.assertEqual(RADIUS_WINDOW_PX,22);self.assertEqual(TRANSFER_CORNER_RADIUS_PX,22)
        for platform in ('win','mac'):
            for theme in ('light','dark'):self.assertEqual(colors(theme,platform)['radius'],22)
    def test_snapshot_clip_uses_outer_radius_without_changing_bounds(self):
        rect=QRect(0,0,330,312);path=surface_clip(rect)
        self.assertEqual(path.boundingRect(),rect)
        self.assertFalse(path.contains(QPointF(2,2)));self.assertTrue(path.contains(QPointF(22,1)))
    def test_compact_and_shake_share_the_same_outer_surface(self):
        from shelf_window import _ShelfSurface
        surface=_ShelfSurface()
        try:self.assertEqual(surface.surface_style()[2],22)
        finally:surface.deleteLater();flush()
    def test_settings_surface_has_clear_outer_corners(self):
        from app_settings import SettingsSurface
        surface=SettingsSurface();surface.setAttribute(Qt.WA_TranslucentBackground);surface.resize(300,200)
        try:
            for dpr in (1,1.25,2):
                image=QImage(round(300*dpr),round(200*dpr),QImage.Format_ARGB32_Premultiplied);image.setDevicePixelRatio(dpr);image.fill(Qt.transparent)
                p=QPainter(image);surface.render(p,QPoint());p.end()
                self.assertLess(image.pixelColor(round(2*dpr),round(2*dpr)).alpha(),20)
        finally:surface.deleteLater();flush()

class FilePileTests(unittest.TestCase):
    def test_pointer_proximity_accelerates_the_same_particle_continuously(self):
        from edge_magnetic_model import MagneticField,Particle
        speeds=[]
        for distance in (150,80,10):
            field=MagneticField(seed=7);field.follow(200,distance,0,400,reset=True)
            field.particles=[Particle(100,200,-20,0,.4,3,2,.1,100,200)]
            field._simulate(1/120);speeds.append(abs(field.particles[0].vx))
        self.assertLess(speeds[0],speeds[1]);self.assertLess(speeds[1],speeds[2])
    def test_real_layers_are_visible_and_count_is_preserved(self):
        images=[marker(color) for color in ('#e12f3f','#238d54','#3775d4','#e3ba32')]
        composed=file_stack_artwork(images,1000)
        self.assertFalse(composed.isNull());self.assertNotEqual(composed,images[0])
        found=set()
        for y in range(composed.height()):
            for x in range(composed.width()):
                pixel=composed.pixelColor(x,y)
                if pixel.alpha()>240:found.add(pixel.name())
        self.assertTrue({'#e12f3f','#238d54','#3775d4'}.issubset(found))
    def test_single_icon_is_not_redrawn_or_badged(self):
        source=marker('#a92557');self.assertEqual(file_stack_artwork([source],1),source)
    def test_many_file_lookup_is_bounded_to_four(self):
        cache=NativeIconCache(reader=lambda ref,size,cancel:marker('#2676b1'));visual=EdgeAbsorb(icon_cache=cache)
        urls=[QUrl.fromLocalFile(str(Path('fixture-'+str(index)+'.txt').resolve())) for index in range(1000)]
        try:
            visual.prefetch_icons(urls);self.assertLessEqual(len(cache._jobs),4)
            area=APP.primaryScreen().geometry();visual.release(QPoint(area.right()-5,area.center().y()),urls=urls)
            settle(lambda:visual.drop_artwork is not None)
            self.assertEqual(visual.drop_artwork.file_count,1000);self.assertEqual(visual.drop_artwork.layer_count,4)
            self.assertLessEqual(len(cache._cache),4)
        finally:visual.close();cache.pool.waitForDone(1000);cache.deleteLater();flush()
    def test_four_edge_piles_stay_inside_screen_and_finish(self):
        area=QRect(-800,-200,800,600);images=[marker(color) for color in ('red','green','blue','yellow')]
        for edge,point in (('left',QPoint(-800,-180)),('right',QPoint(-1,380)),('top',QPoint(-790,-200)),('bottom',QPoint(-10,399))):
            art=DropArtwork(target_in_area(point,area,edge,160),[None]*12,images=images)
            for moment in (0,105,210,350,440):
                art.elapsed_ms=moment
                image=QImage(820,620,QImage.Format_ARGB32_Premultiplied);image.fill(Qt.transparent)
                p=QPainter(image);art.paint(p,QPoint(-810,-210));p.end()
                self.assertTrue(all(image.pixelColor(x,y).alpha()==0 for x,y in ((0,0),(819,0),(0,619),(819,619))))
            self.assertFalse(art.running)

class PreviewPriorityTests(unittest.TestCase):
    def setUp(self):
        self.visual=EdgeAbsorb(seed=7);self.area=APP.primaryScreen().geometry()
        self.point=QPoint(self.area.right()-6,self.area.center().y())
        self.preview=QRect(self.area.right()-180,self.point.y()-100,181,200)
        self.edge_received=[];self.preview_received=[];self.moves=[]
        self.gate=EdgeFileDrag(self.visual,lambda mime,pos:self.edge_received.append(mime.urls()) or True,
            pointer=lambda:self.point,on_file_move=self.moves.append,preview_regions=lambda:(self.preview,),
            on_preview_drop=lambda mime,pos:self.preview_received.append(mime.urls()) or True)
        self.mime=QMimeData();self.mime.setUrls([QUrl.fromLocalFile(str(Path('synthetic-priority.txt').resolve()))])
        self.gate.update_pointer(self.area.center(),False);self.gate.update_pointer(self.point,True)
    def tearDown(self):self.gate.close();self.visual.close();flush()
    def enter(self):
        event=QDragEnterEvent(QPoint(),Qt.CopyAction,self.mime,Qt.LeftButton,Qt.NoModifier)
        self.gate.observe_native_move(event,self.point,entering=True);return event
    def drop(self,point=None):
        event=QDropEvent(QPointF(),Qt.CopyAction,self.mime,Qt.NoButton,Qt.NoModifier)
        self.gate._native_drop(event,point or self.point);return event
    def test_preview_region_is_excluded_from_native_edge_hit_testing(self):
        window=self.gate._windows['right'];self.assertFalse(window.mask().contains(window.mapFromGlobal(self.point)))
        self.assertTrue(self.gate.prepared)
    def test_preview_and_field_are_active_together(self):
        self.enter();self.assertTrue(self.moves);self.assertTrue(self.visual.overlay.isVisible());self.assertTrue(self.gate.validated)
    def test_preview_wins_even_a_late_drop_addressed_to_edge(self):
        self.enter()
        with patch.object(self.visual,'release') as release:
            self.assertTrue(self.drop().isAccepted());flush();release.assert_not_called()
        self.assertEqual(len(self.preview_received),1);self.assertFalse(self.edge_received)
        self.assertFalse(self.drop().isAccepted());self.assertEqual(len(self.preview_received),1)
    def test_outside_preview_uses_edge_and_absorption(self):
        point=QPoint(self.area.right()-6,self.preview.bottom()+30)
        with patch.object(self.visual,'release') as release:
            self.assertTrue(self.drop(point).isAccepted());flush();release.assert_called_once()
        self.assertFalse(self.preview_received);self.assertEqual(len(self.edge_received),1)
    def test_plain_pointer_and_nonfile_mime_do_not_authorize_field(self):
        self.assertFalse(self.visual.overlay.isVisible());mime=QMimeData();mime.setText('synthetic text')
        event=QDragEnterEvent(QPoint(),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier)
        self.gate.observe_native_move(event,self.point,entering=True)
        self.assertFalse(self.visual.overlay.isVisible());self.assertFalse(self.moves)
    def test_native_leave_then_preview_enter_restores_proof(self):
        self.enter();self.gate._native_leave();self.assertFalse(self.gate.validated)
        self.enter();self.assertTrue(self.gate.validated)

class IntegratedPreviewTests(unittest.TestCase):
    def test_real_manager_reveals_preview_without_retiring_field_and_receives_once(self):
        from contextlib import ExitStack
        from PySide6.QtWidgets import QSystemTrayIcon
        import flickdock,app_settings
        with tempfile.TemporaryDirectory(prefix='flickdock-priority-integration-') as directory,ExitStack() as mocks:
            base=Path(directory)
            mocks.enter_context(patch.dict(os.environ,APPDATA=str(base/'profile')))
            for name,value in (('BASE',base/'state'),('STATE_FILE',base/'state/shelves.json'),('OLD_STATE_FILE',base/'old/shelves.json')):
                mocks.enter_context(patch.object(flickdock,name,value))
            mocks.enter_context(patch.object(QSystemTrayIcon,'show'));mocks.enter_context(patch.object(QSystemTrayIcon,'hide'))
            mocks.enter_context(patch.object(app_settings,'_read_startup_value'));mocks.enter_context(patch.object(app_settings,'_write_startup_value'))
            manager=flickdock.ShelfManager(APP);manager.timer.stop()
            try:
                manager.motion=False;manager.shelf.presentation.motion=False;manager.shelf.geometry_morph.motion=False
                manager.settings.update(dock_position=50);manager.update_handles()
                point=manager.edge.activation_region().center();area=manager.edge.display.geometry()
                gate=manager.edge_file_drag;gate.update_pointer(area.center(),False);gate.update_pointer(point,True)
                fixture=base/'dragged.txt';fixture.write_text('synthetic file',encoding='utf-8')
                mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(fixture))])
                event=QDragEnterEvent(QPoint(),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier)
                gate._native_move(event,point,entering=True);flush()
                self.assertTrue(event.isAccepted());self.assertTrue(manager.shelf.isVisible())
                self.assertEqual(manager.shelf.presentation_kind,'dock');self.assertTrue(manager.edge_absorb.overlay.isVisible())
                self.assertTrue(manager.shelf.zones['permanent'].hint.isHidden())
                self.assertEqual(manager.edge_absorb.config.glow_width_px,150)
                hit=manager.shelf.visual_geometry().intersected(gate.window.geometry())
                self.assertFalse(hit.isEmpty());point=hit.center()
                local=manager.shelf.mapFromGlobal(point)
                drop=QDropEvent(QPointF(local),Qt.CopyAction,mime,Qt.NoButton,Qt.NoModifier)
                with patch.object(manager.shelf,'receive_mime',return_value=True) as receive,patch.object(manager.edge_absorb,'release') as release:
                    manager.shelf.dropEvent(drop);flush()
                    self.assertTrue(drop.isAccepted());receive.assert_called_once();release.assert_not_called()
            finally:manager.quit();flush()

if __name__=='__main__':unittest.main(verbosity=2)
