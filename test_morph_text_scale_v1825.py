"""Qt raster regressions, not claims about native compositor refresh rate."""
import unittest
from unittest.mock import patch
from PySide6.QtCore import QPoint,QRect,QRectF,QSizeF,Qt
from PySide6.QtGui import QColor,QImage,QPainter,QPixmap
from PySide6.QtWidgets import QWidget
import test_html_native_workspace as ws
from surface_morph import SurfaceSnapshot


class MorphTextScaleTests(unittest.TestCase):
    def pixmap(self,width,height,dpr,color='#1c1c1e'):
        image=QPixmap(round(width*dpr),round(height*dpr))
        image.setDevicePixelRatio(dpr);image.fill(QColor(color));return image

    def marker_bounds(self,image,channel):
        image=image.toImage().convertToFormat(QImage.Format.Format_RGBA8888);pixels=[]
        data=bytes(image.constBits());width=image.width()
        for offset in range(0,len(data),4):
            values=data[offset:offset+3]
            if values[channel]>65 and values[channel]>max(v for i,v in enumerate(values) if i!=channel)*1.8:
                pixels.append(((offset//4)%width,(offset//4)//width))
        self.assertTrue(pixels,'The content marker disappeared instead of retaining its normal size')
        dpr=image.devicePixelRatioF()
        return ((max(x for x,y in pixels)-min(x for x,y in pixels)+1)/dpr,
                (max(y for x,y in pixels)-min(y for x,y in pixels)+1)/dpr)

    def test_both_directions_keep_endpoint_glyph_aspect_and_never_enlarge_text(self):
        for dpr in (1.,1.25,2.):
            owner=QWidget();snapshot=SurfaceSnapshot(owner)
            compact=self.pixmap(348,360,dpr);overview=self.pixmap(1024,580,dpr)
            p=QPainter(compact);p.fillRect(QRectF(20,200,20,8),QColor('red'));p.end()
            p=QPainter(overview);p.fillRect(QRectF(30,300,18,6),QColor('#00ff00'));p.end()
            for source,target in ((compact,overview),(overview,compact)):
                with self.subTest(dpr=dpr,expanding=source is compact):
                    snapshot.begin(source,target,QRect(0,0,376,387),present=False)
                    snapshot.mix=.5;image=self.pixmap(686,470,dpr)
                    p=QPainter(image);snapshot.paint_frame(p,QRectF(0,0,686,470));p.end()
                    red=self.marker_bounds(image,0);green=self.marker_bounds(image,1)
                    for actual,original in ((red,(20,8)),(green,(18,6))):
                        self.assertLessEqual(actual[0],original[0]+1.,'A compact title/glyph was enlarged across the overview')
                        self.assertLessEqual(actual[1],original[1]+1.)
                        self.assertLessEqual(abs(actual[0]/original[0]-actual[1]/original[1]),.18,'Text was stretched non-uniformly')
            snapshot.finish();owner.deleteLater();ws.flush()

    def test_frames_reuse_endpoint_pixmaps_without_full_surface_scratch_allocations(self):
        owner=QWidget();snapshot=SurfaceSnapshot(owner)
        source=self.pixmap(348,360,2.);target=self.pixmap(1024,580,2.)
        snapshot.begin(source,target,QRect(0,0,376,387),present=False)
        output=self.pixmap(1024,580,2.)
        # The output and cached endpoints already exist, as in MotionShadow.
        # Any per-frame scratch construction is a real regression, not merely
        # a hidden progress value or an altered timer frequency.
        with patch('surface_morph.QPixmap',side_effect=AssertionError('per-frame full-surface allocation')):
            for progress in (.02,.2,.5,.74,.8,.95,1.):
                snapshot.mix=progress;p=QPainter(output)
                try:snapshot.paint_frame(p,QRectF(0,0,348+676*progress,360+220*progress))
                finally:p.end()
        snapshot.finish();owner.deleteLater();ws.flush()

    def test_reverse_captures_last_painted_live_pixmap_after_tree_has_changed(self):
        owner=QWidget();snapshot=SurfaceSnapshot(owner)
        dpr=owner.devicePixelRatioF()
        source=self.pixmap(120,80,dpr,'red');target=self.pixmap(120,80,dpr,'green')
        snapshot.begin(source,target,QRect(0,0,148,107),present=False)
        current=[self.pixmap(120,80,dpr,'blue')];frozen=[None];acquisitions=[]
        def live(painter):
            # Same one-acquisition closure contract as MotionShadow.draw.
            if frozen[0] is None:frozen[0]=current[0];acquisitions.append(1)
            painter.drawPixmap(QPoint(),frozen[0])
        snapshot.mix=.9;painted=self.pixmap(120,80,dpr,'transparent')
        p=QPainter(painted);snapshot.paint_frame(p,QRectF(0,0,120,80),live,QSizeF(120,80));p.end()
        current[0]=self.pixmap(120,80,dpr,'yellow')
        # Progress may be scheduled before a paint. A reversal must start at
        # the actual last rendered mixture rather than the newly pending one.
        snapshot.mix=.99
        captured=snapshot.capture_current(owner,())
        self.assertEqual(captured.toImage(),painted.toImage())
        self.assertEqual(acquisitions,[1]);snapshot.finish()
        self.assertIsNone(snapshot._last_paint);owner.deleteLater();ws.flush()


if __name__=='__main__':unittest.main()
