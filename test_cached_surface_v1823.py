"""Actual Qt pixel proof of cached→live crossfade, not native/GPU performance."""
import unittest
from PySide6.QtCore import QRect,QRectF,QSizeF,Qt
from PySide6.QtGui import QColor,QPainter,QPixmap
from PySide6.QtWidgets import QWidget
import test_html_native_workspace as ws
from surface_morph import SurfaceSnapshot


class CachedSurfaceTests(unittest.TestCase):
    def test_cached_phase_live_handoff_and_exact_endpoint_at_fractional_dpr(self):
        for dpr in (1.,1.25,2.):
            with self.subTest(dpr=dpr):
                owner=QWidget();owner.resize(120,80);snapshot=SurfaceSnapshot(owner)
                def pixmap(color):
                    p=QPixmap(round(120*dpr),round(80*dpr));p.setDevicePixelRatio(dpr);p.fill(QColor(color));return p
                snapshot.begin(pixmap('red'),pixmap('green'),QRect(0,0,148,107),present=False);calls=[]
                def live(p):calls.append(1);p.fillRect(QRectF(0,0,120,80),QColor('blue'))
                def render(mix,x=60):
                    snapshot.mix=mix;target=pixmap('transparent');p=QPainter(target)
                    snapshot.paint_frame(p,QRectF(0,0,120,80),live,QSizeF(120,80));p.end()
                    return target.toImage().pixelColor(round(x*dpr),round(40*dpr))
                # The cached endpoints hand off complementary regions; a
                # yellow mixed pixel here would restore the actual ghosting.
                self.assertEqual(render(.5,100),QColor('red'))
                self.assertEqual(render(.5,30),QColor('green'));self.assertEqual(len(calls),0)
                previous=0
                for mix in (.75,.80,.90,.99,1.):
                    c=render(mix);self.assertGreaterEqual(c.blue(),previous);previous=c.blue()
                    self.assertEqual(c.alpha(),255)
                self.assertEqual(c,QColor('blue'));self.assertEqual(len(calls),4)
                painted=render(.95)
                captured=snapshot.capture_current(owner,()).toImage()
                self.assertEqual(captured.pixelColor(captured.width()//2,captured.height()//2),painted)
                snapshot.finish();self.assertTrue(snapshot.source.isNull());self.assertTrue(snapshot.target.isNull())
                self.assertTrue(snapshot.source.isNull());self.assertTrue(snapshot.target.isNull())
                self.assertIsNone(snapshot._last_paint)
                for retired in ('_blend','_weighted','_blend_valid'):
                    self.assertFalse(hasattr(snapshot,retired))
                owner.deleteLater();ws.flush()

if __name__=='__main__':unittest.main()
