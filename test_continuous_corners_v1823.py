"""Actual path derivatives and raster pixels; no desktop/window interaction."""
import math
import unittest

from PySide6.QtCore import QPointF,QRectF,Qt
from PySide6.QtGui import QColor,QImage,QPainter,QPainterPath,QPen,QTransform
from continuous_corners import (G2_SMOOTHING,clamped_corner_radii,continuous_rect_path,
                                draw_continuous_rect,paint_continuous_panel,_normalized_rect_path,_normalized_border_ring)


def cubics(path):
    result=[];i=0;current=(0.,0.)
    while i<path.elementCount():
        element=path.elementAt(i);point=(element.x,element.y)
        if element.isMoveTo() or element.isLineTo():current=point
        else:
            second,end=path.elementAt(i+1),path.elementAt(i+2)
            result.append((current,point,(second.x,second.y),(end.x,end.y)))
            current=(end.x,end.y);i+=2
        i+=1
    return result


def subtract(a,b):return (a[0]-b[0],a[1]-b[1])
def scale(a,k):return (a[0]*k,a[1]*k)
def cross(a,b):return a[0]*b[1]-a[1]*b[0]
def norm(a):return math.hypot(*a)
def first(segment,t):
    p0,p1,p2,p3=segment
    return tuple(3*((1-t)**2*(p1[i]-p0[i])+2*(1-t)*t*(p2[i]-p1[i])+t*t*(p3[i]-p2[i])) for i in (0,1))
def second(segment,t):
    p0,p1,p2,p3=segment
    return tuple(6*((1-t)*(p2[i]-2*p1[i]+p0[i])+t*(p3[i]-2*p2[i]+p1[i])) for i in (0,1))
def curvature(segment,t):
    d1=first(segment,t);return cross(d1,second(segment,t))/norm(d1)**3


class ContinuousCornerTests(unittest.TestCase):
    def assertPoint(self,a,b,tolerance=1e-8):
        self.assertAlmostEqual(a[0],b[0],delta=tolerance);self.assertAlmostEqual(a[1],b[1],delta=tolerance)

    def test_eight_real_cubics_have_c2_midpoint_and_zero_curvature_line_contacts(self):
        path=continuous_rect_path(QRectF(13,-7,240,160),32)
        segments=cubics(path);self.assertEqual(len(segments),8)
        directions=((1,0),(0,1),(0,1),(-1,0),(-1,0),(0,-1),(0,-1),(1,0))
        for i in range(0,8,2):
            a,b=segments[i:i+2]
            self.assertPoint(a[-1],b[0]);self.assertPoint(first(a,1),first(b,0));self.assertPoint(second(a,1),second(b,0))
            self.assertGreater(norm(first(a,0)),0);self.assertGreater(norm(first(b,1)),0)
            self.assertAlmostEqual(curvature(a,0),0,delta=1e-10);self.assertAlmostEqual(curvature(b,1),0,delta=1e-10)
            self.assertPoint(scale(first(a,0),1/norm(first(a,0))),directions[i])
            self.assertPoint(scale(first(b,1),1/norm(first(b,1))),directions[i+1])
            for segment in (a,b):
                for j in range(101):
                    self.assertGreater(norm(first(segment,j/100)),0)
                    self.assertGreaterEqual(curvature(segment,j/100),-1e-10)

    def test_actual_radius_anchors_and_bounds_preserved_not_fake_large_radius(self):
        rect=QRectF(-50,11,200,120);path=continuous_rect_path(rect,40)
        self.assertEqual(path.boundingRect(),rect)
        corners=cubics(path)
        self.assertPoint(corners[0][0],(110,11));self.assertPoint(corners[1][-1],(150,51))
        # The chosen corner midpoint differs from a circular arc while keeping
        # its original straight-edge radius endpoints exactly unchanged.
        self.assertPoint(corners[0][-1],(150-G2_SMOOTHING*40,11+G2_SMOOTHING*40))
        self.assertNotAlmostEqual(G2_SMOOTHING,1-math.sqrt(.5),delta=.01)
        self.assertTrue(path.contains(rect.center()));self.assertFalse(path.contains(rect.topLeft()))

    def test_radius_clamp_zero_tiny_rects_and_invalid_numbers(self):
        for rect in (QRectF(0,0,1,1),QRectF(.2,.3,.01,.04),QRectF(0,0,100,1)):
            path=continuous_rect_path(rect,10000)
            self.assertEqual(path.boundingRect(),rect);self.assertEqual(len(cubics(path)),8)
            self.assertEqual(clamped_corner_radii(rect,10000)[0],(min(rect.width(),rect.height())/2,)*2)
        self.assertTrue(continuous_rect_path(QRectF(0,0,0,10),9).isEmpty())
        self.assertTrue(continuous_rect_path(QRectF(0,0,-5,10),9).isEmpty())
        self.assertEqual(len(cubics(continuous_rect_path(QRectF(0,0,10,10),-3))),0)
        for value in (float('nan'),float('inf')):
            with self.assertRaises(ValueError):continuous_rect_path(QRectF(0,0,10,10),value)
            with self.assertRaises(ValueError):continuous_rect_path(QRectF(value,0,10,10),3)

    def test_nonuniform_dpi_affine_transform_keeps_g2_and_c2(self):
        original=continuous_rect_path(QRectF(3,7,130,75),(24,12))
        transform=QTransform();transform.scale(1.25,1.75)
        path=transform.map(original)
        self.assertEqual(path.boundingRect(),transform.mapRect(original.boundingRect()))
        segments=cubics(path)
        for i in range(0,8,2):
            a,b=segments[i:i+2]
            self.assertPoint(first(a,1),first(b,0));self.assertPoint(second(a,1),second(b,0))
            self.assertAlmostEqual(curvature(a,0),0,delta=1e-10);self.assertAlmostEqual(curvature(b,1),0,delta=1e-10)

    def test_selective_corners_and_css_overlap_clamp_are_inside_original_box(self):
        rect=QRectF(0,0,100,60)
        radii=clamped_corner_radii(rect,corner_radii=(80,80,0,0))
        self.assertEqual(radii,((50,50),(50,50),(0,0),(0,0)))
        path=continuous_rect_path(rect,corner_radii=(80,80,0,0))
        self.assertEqual(path.boundingRect(),rect);self.assertEqual(len(cubics(path)),4)
        self.assertTrue(path.contains(QPointF(.1,59.9)))
        self.assertFalse(path.contains(QPointF(.1,.1)))
        asymmetric=continuous_rect_path(rect,corner_radii=((50,20),(30,15),(15,25),(10,20)))
        self.assertEqual(asymmetric.boundingRect(),rect);self.assertEqual(len(cubics(asymmetric)),8)

    def test_raster_is_g2_fill_and_differs_from_equal_radius_arc(self):
        rect=QRectF(10,10,160,120)
        g2=QImage(180,140,QImage.Format_ARGB32_Premultiplied);g2.fill(Qt.transparent)
        arc=QImage(g2.size(),g2.format());arc.fill(Qt.transparent)
        p=QPainter(g2);draw_continuous_rect(p,rect,40,fill='#2485ED',pen=Qt.NoPen);p.end()
        p=QPainter(arc);p.setRenderHint(QPainter.Antialiasing);p.setPen(Qt.NoPen);p.setBrush(QColor('#2485ED'));p.drawRoundedRect(rect,40,40);p.end()
        self.assertGreater(g2.pixelColor(20,20).alpha(),180);self.assertLess(arc.pixelColor(20,20).alpha(),80)
        for image in (g2,arc):
            self.assertEqual(image.pixelColor(0,0).alpha(),0);self.assertEqual(image.pixelColor(90,70).alpha(),255)
            self.assertEqual(image.pixelColor(90,10).getRgb()[:3],QColor('#2485ED').getRgb()[:3])

    def test_pixel_fill_clip_same_path_at_fractional_dpr_and_painter_state_restored(self):
        for dpr in (1.,1.25,1.5,2.):
            with self.subTest(dpr=dpr):
                image=QImage(round(100*dpr),round(80*dpr),QImage.Format_ARGB32_Premultiplied)
                image.setDevicePixelRatio(dpr);image.fill(Qt.transparent)
                rect=QRectF(10,10,80,60);p=QPainter(image);p.setPen(QPen(QColor('red'),3));p.setBrush(Qt.NoBrush)
                pen=QPen(p.pen());hints=p.renderHints();transform=p.transform()
                path=draw_continuous_rect(p,rect,20,fill='#FFFFFF',pen=Qt.NoPen)
                self.assertEqual(p.pen(),pen);self.assertEqual(p.renderHints(),hints);self.assertEqual(p.transform(),transform)
                p.save();p.setClipPath(path);p.fillRect(rect,QColor('#101010'));p.restore();p.end()
                self.assertEqual(image.pixelColor(round(50*dpr),round(40*dpr)).getRgb(),(16,16,16,255))
                self.assertEqual(image.pixelColor(round(10*dpr),round(10*dpr)).alpha(),0)
                self.assertEqual(image.pixelColor(0,0).alpha(),0)

    def test_bounded_geometry_cache_cannot_be_mutated_by_callers(self):
        _normalized_rect_path.cache_clear()
        original=continuous_rect_path(QRectF(0,0,100,50),10)
        original.addRect(QRectF(-100,-100,10,10))
        copied=continuous_rect_path(QRectF(0,0,100,50),10)
        self.assertEqual(copied.boundingRect(),QRectF(0,0,100,50))
        for i in range(1000):continuous_rect_path(QRectF(i,0,100+i/100,50),10)
        self.assertEqual(_normalized_rect_path.cache_info().maxsize,256)
        self.assertLessEqual(_normalized_rect_path.cache_info().currsize,256)

    def test_panel_border_is_real_g2_ring_with_same_outer_fill_and_mask(self):
        image=QImage(180,140,QImage.Format_ARGB32_Premultiplied);image.fill(Qt.transparent)
        p=QPainter(image);outer=paint_continuous_panel(p,QRectF(10,10,160,120),40,
            fill='#2485ED',border='#FF0000',border_width=2);p.end()
        self.assertEqual(image.pixelColor(90,10).getRgb(),(255,0,0,255))
        self.assertEqual(image.pixelColor(90,15).getRgb(),QColor('#2485ED').getRgb())
        self.assertEqual(image.pixelColor(10,10).alpha(),0)
        self.assertGreater(image.pixelColor(20,20).alpha(),180)
        self.assertEqual(outer,continuous_rect_path(QRectF(10,10,160,120),40))
        self.assertEqual(len(cubics(outer)),8)
        ring=_normalized_border_ring(160,120,clamped_corner_radii(QRectF(0,0,160,120),40),2.)
        self.assertEqual(len(cubics(ring)),16)
        self.assertEqual(ring.fillRule(),Qt.OddEvenFill)
        self.assertFalse(ring.contains(QPointF(80,60)))
        self.assertTrue(ring.contains(QPointF(80,.5)))
        ring_segments=cubics(ring)
        for i in range(0,16,2):
            a,b=ring_segments[i:i+2]
            self.assertPoint(first(a,1),first(b,0));self.assertPoint(second(a,1),second(b,0))
            self.assertAlmostEqual(curvature(a,0),0,delta=1e-10);self.assertAlmostEqual(curvature(b,1),0,delta=1e-10)
        image.fill(Qt.transparent);p=QPainter(image)
        _normalized_border_ring.cache_clear()
        for i in range(300):paint_continuous_panel(p,QRectF(10,10,160+i/100,120),40,fill='#2485ED',border='#FF0000')
        p.end();self.assertLessEqual(_normalized_border_ring.cache_info().currsize,256)


if __name__=='__main__':unittest.main()
