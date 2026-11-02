"""Qt raster regressions, not claims about native compositor refresh rate."""
import unittest
from unittest.mock import patch
from PySide6.QtCore import QPoint,QRect,QRectF,QSizeF,Qt
from PySide6.QtGui import QColor,QImage,QPainter,QPixmap
from PySide6.QtWidgets import QWidget
import test_html_native_workspace as ws
from surface_morph import SurfaceMorph,SurfaceSnapshot


class MorphTextScaleTests(unittest.TestCase):
    def setUp(self):
        import ui_theme
        original=ui_theme.color
        # These synthetic endpoint rasters explicitly use the dark material.
        # The global application may still be on the OS light theme before
        # any real workspace fixture runs. A fade must use its matching base;
        # this is not a simulated light/dark theme change during a morph.
        material=patch('ui_theme.color',side_effect=lambda key: '#1c1c1e' if key=='surface' else original(key))
        material.start();self.addCleanup(material.stop)

    def pixmap(self,width,height,dpr,color='#1c1c1e'):
        image=QPixmap(round(width*dpr),round(height*dpr))
        image.setDevicePixelRatio(dpr);image.fill(QColor(color));return image

    def marker_pixels(self,image,channel):
        image=image.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        data=bytes(image.constBits());width=image.width()
        colors=zip(data[0::4],data[1::4],data[2::4])
        if channel==0:
            return {(index%width,index//width) for index,(r,g,b) in enumerate(colors) if r>65 and r>g*1.8 and r>b*1.8}
        return {(index%width,index//width) for index,(r,g,b) in enumerate(colors) if g>65 and g>r*1.8 and g>b*1.8}

    def marker_bounds(self,image,channel,required=True):
        pixels=self.marker_pixels(image,channel)
        if not pixels:
            if required:self.fail('The content marker disappeared instead of retaining its normal size')
            return None
        dpr=image.devicePixelRatioF()
        return ((max(x for x,y in pixels)-min(x for x,y in pixels)+1)/dpr,
                (max(y for x,y in pixels)-min(y for x,y in pixels)+1)/dpr)

    def test_same_region_endpoint_ink_is_never_cross_composited(self):
        # The real compact title/drop copy and overview details occupy the
        # same screen region during expansion. Distinct solid ink gives a
        # strict image counterexample: the old alpha overlay creates yellow
        # pixels, even though neither original endpoint contains yellow.
        for dpr in (1.,1.25,2.):
            owner=QWidget();snapshot=SurfaceSnapshot(owner)
            source=self.pixmap(120,80,dpr);target=self.pixmap(120,80,dpr)
            for image,ink in ((source,'#ff0000'),(target,'#00ff00')):
                p=QPainter(image);p.fillRect(QRectF(20,30,80,20),QColor(ink));p.end()
            large=self.pixmap(240,160,dpr)
            p=QPainter(large);p.fillRect(QRectF(80/.75,30/.75,80/.75,20/.75),QColor('#00ff00'));p.end()
            for name,a,b,rect in (('same',source,target,QRectF(0,0,120,80)),
                                  ('expand',source,large,QRectF(0,0,180,120)),
                                  ('collapse',large,source,QRectF(0,0,180,120))):
                snapshot.begin(a,b,QRect(0,0,148,107),present=False)
                for progress in (.2,.5,.8):
                    with self.subTest(dpr=dpr,progress=progress,direction=name):
                        snapshot.mix=progress;image=self.pixmap(round(rect.width()),round(rect.height()),dpr)
                        p=QPainter(image);snapshot.paint_frame(p,rect);p.end()
                        image=image.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
                        data=bytes(image.constBits())
                        doubled=sum(r>45 and g>45 and b<10 for r,g,b,a in zip(data[0::4],data[1::4],data[2::4],data[3::4]))
                        self.assertEqual(doubled,0,'Two endpoint inks overlap in the same pixels')
            snapshot.finish();owner.deleteLater();ws.flush()

    def test_both_directions_keep_endpoint_glyph_aspect_and_never_enlarge_text(self):
        for dpr in (1.,1.25,2.):
            owner=QWidget();snapshot=SurfaceSnapshot(owner)
            compact=self.pixmap(348,360,dpr);overview=self.pixmap(1024,580,dpr)
            p=QPainter(compact);p.fillRect(QRectF(20,200,20,8),QColor('red'));p.end()
            p=QPainter(overview);p.fillRect(QRectF(30,300,18,6),QColor('#00ff00'));p.end()
            for source,target in ((compact,overview),(overview,compact)):
                with self.subTest(dpr=dpr,expanding=source is compact):
                    snapshot.begin(source,target,QRect(0,0,376,387),present=False)
                    for progress in (0.,.02,.1,.2,.4,.5,.65,.75,.9,1.):
                        a,b=source.deviceIndependentSize(),target.deviceIndependentSize()
                        rect=QRectF(0,0,a.width()+(b.width()-a.width())*progress,
                                    a.height()+(b.height()-a.height())*progress)
                        snapshot.mix=progress;image=self.pixmap(round(rect.width()),round(rect.height()),dpr)
                        p=QPainter(image);snapshot.paint_frame(p,rect);p.end()
                        red=self.marker_bounds(image,0,required=False);green=self.marker_bounds(image,1,required=False)
                        self.assertTrue(red or green,'Both endpoint contents disappeared during the handoff')
                        for actual,original,endpoint in ((red,(20,8),compact),(green,(18,6),overview)):
                            if actual is None:continue
                            self.assertLessEqual(actual[0],original[0]+1.,'A compact title/glyph was enlarged across the overview')
                            self.assertLessEqual(actual[1],original[1]+1.)
                            logical=endpoint.deviceIndependentSize()
                            scale=min(1.,rect.width()/logical.width(),rect.height()/logical.height())
                            reference=self.pixmap(round(rect.width()),round(rect.height()),dpr)
                            p=QPainter(reference);p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                            destination=QRectF(rect.right()-logical.width()*scale,rect.top(),logical.width()*scale,logical.height()*scale)
                            p.drawPixmap(destination,endpoint,QRectF(endpoint.rect()));p.end()
                            channel=0 if endpoint is compact else 1
                            actual_pixels=self.marker_pixels(image,channel);reference_pixels=self.marker_pixels(reference,channel)
                            self.assertTrue(actual_pixels<=reference_pixels,'Visible glyph pixels differ from the uniformly fitted original')
                            # Preserve the original midpoint aspect check for
                            # fully revealed ink. Additional early samples can
                            # be only 2 pixels tall; their exact raster shape
                            # is checked above instead of loosening its ratio.
                            if progress==.5 and actual_pixels==reference_pixels:
                                self.assertLessEqual(abs(actual[0]/original[0]-actual[1]/original[1]),.18,'Text was stretched non-uniformly')
                        if progress==0.:
                            self.assertIsNotNone(red if source is compact else green,'The source endpoint lost its marker')
                        if progress==1.:
                            self.assertIsNotNone(green if target is overview else red,'The target endpoint lost its marker')
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

    def test_repeated_clip_handoff_reversal_starts_at_the_last_actual_pixels(self):
        for dpr in (1.,1.25,2.):
            owner=QWidget();snapshot=SurfaceSnapshot(owner)
            source=self.pixmap(120,80,dpr,'red');target=self.pixmap(120,80,dpr,'green')
            for progress in (.15,.4,.65,.25):
                snapshot.begin(source,target,QRect(0,0,148,107),present=False)
                snapshot.mix=progress;painted=self.pixmap(120,80,dpr,'transparent')
                p=QPainter(painted);snapshot.paint_frame(p,QRectF(0,0,120,80));p.end()
                snapshot.mix=.99  # A scheduled value is not the last paint.
                with patch.object(owner,'devicePixelRatioF',return_value=dpr):
                    captured=snapshot.capture_current(owner,())
                self.assertEqual(captured.toImage(),painted.toImage())
                target=source;source=captured
                snapshot.begin(source,target,QRect(0,0,148,107),present=False)
                start=self.pixmap(120,80,dpr,'transparent');p=QPainter(start)
                snapshot.paint_frame(p,QRectF(0,0,120,80));p.end()
                # The unchanged rounded outer clip resamples its AA corner
                # once on any restart (also on the old compositor). Content
                # inside that contour must be exactly the last actual paint;
                # full captured RGBA equality is asserted above separately.
                body=QRect(round(10*dpr),round(10*dpr),round(100*dpr),round(60*dpr))
                self.assertEqual(start.toImage().copy(body),painted.toImage().copy(body),'Reversal jumped to a different content frame')
            snapshot.finish();owner.deleteLater();ws.flush()

    def test_reduced_motion_skips_capture_and_dynamic_reduce_lands_once(self):
        owner=QWidget();updates=[];prepared=[];settled=[];done=[];reduced=[True]
        a=QRect(0,0,148,107);b=QRect(-120,0,268,167)
        motion=SurfaceMorph(owner,a,lambda r:updates.append(r),prepare=lambda *r:prepared.append(r),settle=lambda r:settled.append(r))
        with patch('native_ui_common.reduced_motion',side_effect=lambda:reduced[0]):
            motion.animate(b,lambda:done.append(1))
            self.assertEqual(prepared,[]);self.assertEqual(settled,[b]);self.assertEqual(done,[1])
            reduced[0]=False;motion.animate(a,lambda:done.append(2));motion._frame(.3)
            self.assertTrue(motion.active);self.assertEqual(len(prepared),1)
            reduced[0]=True;motion._frame(.4);motion._finish()
            self.assertFalse(motion.active);self.assertEqual(motion.current,a)
            self.assertEqual(settled,[b,a]);self.assertEqual(done,[1,2])
        owner.deleteLater();ws.flush()

    def test_missing_target_keeps_real_source_and_same_footprint_never_has_an_empty_handoff(self):
        for dpr in (1.,1.25,2.):
            owner=QWidget();snapshot=SurfaceSnapshot(owner)
            source=self.pixmap(120,80,dpr,'red');target=self.pixmap(120,80,dpr,'green')
            snapshot.begin(source,QPixmap(),QRect(0,0,148,107),present=False)
            original=None
            for progress in (0.,.2,.375,.5,.75,1.):
                snapshot.mix=progress;image=self.pixmap(120,80,dpr,'transparent')
                p=QPainter(image);snapshot.paint_frame(p,QRectF(0,0,120,80));p.end()
                if original is None:original=image.toImage()
                self.assertEqual(image.toImage(),original,'An unavailable target erased existing content')
            snapshot.begin(source,target,QRect(0,0,148,107),present=False)
            for progress in (index/40 for index in range(41)):
                snapshot.mix=progress;image=self.pixmap(120,80,dpr,'transparent')
                p=QPainter(image);snapshot.paint_frame(p,QRectF(0,0,120,80));p.end()
                self.assertTrue(self.marker_pixels(image,0) or self.marker_pixels(image,1),'The handoff erased both same-size endpoints')
            snapshot.finish();owner.deleteLater();ws.flush()

    def test_source_only_region_does_not_drop_at_the_cached_live_boundary(self):
        for dpr in (1.,1.25,2.):
            owner=QWidget();snapshot=SurfaceSnapshot(owner)
            source=self.pixmap(240,160,dpr,'red');target=self.pixmap(120,80,dpr,'green')
            snapshot.begin(source,target,QRect(0,0,208,147),present=False);images=[]
            for progress in (.7499,.7501):
                snapshot.mix=progress;image=self.pixmap(180,120,dpr,'transparent')
                p=QPainter(image);snapshot.paint_frame(p,QRectF(0,0,180,120));p.end();images.append(image.toImage())
            # The target is still finishing its own fade. Compare only the
            # retiring source-exclusive area, with exact pixels (no tolerance).
            exclusive=QRect(round(10*dpr),round(20*dpr),round(40*dpr),round(60*dpr))
            self.assertEqual(images[0].copy(exclusive),images[1].copy(exclusive),'Source-only content vanished abruptly at the cached/live handoff')
            snapshot.finish();owner.deleteLater();ws.flush()


if __name__=='__main__':unittest.main()
