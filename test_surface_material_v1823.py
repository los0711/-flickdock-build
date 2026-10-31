"""Real Qt alpha and compositor pose, injectable native boundary only."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import unittest
from PySide6.QtCore import Qt,QRect,QRectF
from PySide6.QtGui import QColor,QPixmap,QPainter,QTransform
from PySide6.QtWidgets import QApplication,QWidget,QLabel
from continuous_controls import G2Frame,PanelStyle,set_g2_style
from flickdock_motion import MotionShadow
from surface_morph import SurfaceSnapshot,surface_clip
from surface_material import ShelfMaterial
from window_material import MaterialCapabilities
import ui_theme

APP=QApplication.instance() or QApplication([])

class Backend:
    def __init__(self,attached=True):self.supported=True;self.attach_ok=attached;self.regions=();self.calls=0
    def capabilities(self):return MaterialCapabilities('isolated',self.supported,True,'test')
    def set_policy_callback(self,callback):self.callback=callback
    def attach(self,owner,role):return self.attach_ok
    def set_regions(self,regions,revision=0):self.regions=tuple(regions);self.calls+=1
    def set_theme(self,theme):pass
    def set_visible(self,visible):self.visible=visible
    def close(self):self.regions=()

class SurfaceMaterialTests(unittest.TestCase):
    def setUp(self):
        self.theme=ui_theme.current_theme();ui_theme.set_theme('dark')
        self.owner=QWidget();self.owner.setAttribute(Qt.WA_TranslucentBackground);self.owner.resize(700,400)
        self.owner.card=G2Frame(self.owner);self.owner.card.setGeometry(50,40,220,160)
        set_g2_style(self.owner.card,PanelStyle(10,None,None,0))
        self.owner.surface_effect=MotionShadow(self.owner.card);self.owner.card.setGraphicsEffect(self.owner.surface_effect)
        self.backend=Backend();self.controller=ShelfMaterial(self.owner,backend=self.backend)
        self.foreground=QLabel(self.owner.card);self.foreground.setGeometry(40,30,32,32)
        self.foreground.setStyleSheet('background:#FF0000;border:0;')
        self.owner.show();APP.processEvents()

    def tearDown(self):
        self.controller.handle.close();self.owner.hide();self.owner.deleteLater();APP.processEvents();ui_theme.set_theme(self.theme)

    def pixel(self,x,y):
        image=self.owner.grab().toImage();ratio=image.devicePixelRatio()
        return image.pixelColor(round(x*ratio),round(y*ratio))

    def test_single_90_percent_base_preserves_actual_opaque_foreground(self):
        self.assertAlmostEqual(self.pixel(200,150).alpha(),230,delta=1)
        self.assertEqual(self.pixel(100,85),QColor('#FF0000'))
        self.assertEqual(self.pixel(20,20).alpha(),0)
        self.assertEqual(len(self.backend.regions),1)
        self.assertEqual(self.backend.regions[0].path.boundingRect(),QRectF(self.owner.card.geometry()))

    def test_policy_fallback_and_recovery_change_only_the_single_base(self):
        geometry=self.owner.geometry();self.backend.supported=False;self.backend.callback();APP.processEvents()
        self.assertEqual(self.pixel(200,150).alpha(),255)
        self.assertEqual(self.pixel(100,85),QColor('#FF0000'))
        self.backend.supported=True;self.backend.callback();APP.processEvents()
        self.assertAlmostEqual(self.pixel(200,150).alpha(),230,delta=1)
        self.assertEqual(self.owner.geometry(),geometry)

    def test_inactive_public_capture_preserves_composited_base_but_foreground_capture_does_not(self):
        snapshot=SurfaceSnapshot(self.owner)
        full=snapshot.capture_current(self.owner.card,()).toImage()
        foreground=snapshot.capture_current(self.owner.card,(),foreground_only=True).toImage()
        ratio=full.devicePixelRatio()
        self.assertAlmostEqual(full.pixelColor(round(150*ratio),round(100*ratio)).alpha(),230,delta=1)
        self.assertEqual(foreground.pixelColor(round(150*ratio),round(100*ratio)).alpha(),0)
        self.assertEqual(full.pixelColor(round(50*ratio),round(45*ratio)),QColor('#FF0000'))

    def test_native_mask_follows_existing_effect_pose_without_host_resize(self):
        geometry=self.owner.geometry();self.owner.surface_effect.set_motion(sx=1.03,sy=.98,dx=4,dy=3)
        rect=QRectF(self.owner.card.geometry());center=rect.center();transform=QTransform()
        transform.translate(center.x()+4,center.y()+3);transform.scale(1.03,.98);transform.translate(-center.x(),-center.y())
        path=self.backend.regions[0].path
        self.assertEqual(path,transform.map(surface_clip(rect)))
        self.assertEqual(sum(path.elementAt(i).isCurveTo() for i in range(path.elementCount())),8)
        self.assertEqual(self.owner.geometry(),geometry)
        count=self.backend.calls;self.controller.sync();self.controller.sync()
        self.assertEqual(self.backend.calls,count)

    def test_cached_crossfade_and_reversal_do_not_stack_the_base_alpha(self):
        snapshot=SurfaceSnapshot(self.owner)
        def blank():
            pm=QPixmap(220,160);pm.fill(Qt.transparent);return pm
        snapshot.begin(blank(),blank(),QRect(36,33,248,187),present=False)
        self.owner.surface_effect.begin_surface_morph(snapshot)
        for mix in (0.,.2,.5,.95,1.):
            snapshot.mix=mix;pm=blank();p=QPainter(pm);snapshot.paint_frame(p,QRectF(0,0,220,160));p.end()
            self.assertAlmostEqual(pm.toImage().pixelColor(150,100).alpha(),230,delta=1)
        snapshot.mix=.5;pm=blank();p=QPainter(pm);snapshot.paint_frame(p,QRectF(0,0,220,160));p.end()
        source=snapshot.capture_current(self.owner.card,(),foreground_only=True)
        self.assertEqual(source.toImage().pixelColor(150,100).alpha(),0)
        snapshot.begin(source,blank(),QRect(36,33,248,187),present=False)
        snapshot.mix=.5;pm=blank();p=QPainter(pm);snapshot.paint_frame(p,QRectF(0,0,220,160));p.end()
        self.assertAlmostEqual(pm.toImage().pixelColor(150,100).alpha(),230,delta=1)
        self.owner.surface_effect.end_surface_morph();snapshot.finish()

    def test_attach_failure_keeps_opaque_fallback(self):
        owner=QWidget();owner.setAttribute(Qt.WA_TranslucentBackground);owner.resize(300,220)
        owner.card=G2Frame(owner);owner.card.setGeometry(20,20,220,160);set_g2_style(owner.card,PanelStyle(10,None,None,0))
        owner.surface_effect=MotionShadow(owner.card);owner.card.setGraphicsEffect(owner.surface_effect)
        controller=ShelfMaterial(owner,backend=Backend(attached=False));owner.show();APP.processEvents()
        self.assertFalse(controller.handle.status()['attached'])
        self.assertEqual(controller.background_color('#FFFFFF').alpha(),255)
        controller.handle.close();owner.hide();owner.deleteLater();APP.processEvents()

if __name__=='__main__':unittest.main()
