"""Real Qt endpoint/body content inside the native repaint transaction.

Offscreen rendering proof only: no desktop input, network, native hooks or FPS
claim. Actual normal-EXE captured frames remain a separate acceptance gate.
"""
import unittest
from unittest.mock import patch
from PySide6.QtCore import QEvent,QObject,QPoint,QRect
from PySide6.QtGui import QImage
import test_html_native_workspace as ws
from surface_morph import SurfaceSnapshot


def colored_body(image,rect=None):
    if rect is not None:image=image.copy(rect)
    image=image.convertToFormat(QImage.Format.Format_RGBA8888)
    raw=bytes(image.constBits());count=0
    for offset in range(0,len(raw),4):
        red,green,blue,alpha=raw[offset:offset+4]
        if alpha>=180 and max(red,green,blue)-min(red,green,blue)>40:count+=1
    return count


class OwnerPaints(QObject):
    def __init__(self,owner):super().__init__(owner);self.events=[];owner.installEventFilter(self)
    def eventFilter(self,owner,event):
        if event.type()==QEvent.Paint:self.events.append(1)
        return False


class MorphSnapshotContentTests(unittest.TestCase):
    def setUp(self):
        self.fixture=ws.WorkspaceTests('runTest');self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.shelf=self.fixture.shelf
        # Right-aligned and preallocated, as in a normal dock reveal. The
        # existing fixture's left-screen compact cannot expand to its left.
        self.shelf.hide();self.shelf._show_at(QPoint(1400,160),'manual',QPoint());ws.flush()
        self.patch=patch('native_ui_common.reduced_motion',return_value=False)
        self.patch.start();self.addCleanup(self.patch.stop)
        self.shelf.geometry_morph.motion=True;self.shelf.presentation.motion=True
        self.shelf.presentation.jump(dict(sx=1,sy=1,dx=0,dy=0,alpha=1))

    def controls(self):
        shelf=self.shelf
        return (shelf.network_status,shelf.cloud_button,shelf.lan_button,shelf.pin_button,
                shelf.expand_button,shelf.menu_button,shelf.close_button)

    def header_rects(self):
        return [QRect(button.mapToGlobal(QPoint()),button.size()) for button in self.controls() if button is not self.shelf.lan_button]

    def body_pixels(self):
        shelf=self.shelf;rect=shelf.visual_geometry();image=shelf.grab().toImage();dpr=image.devicePixelRatioF()
        body=QRect(round((rect.x()-shelf.x()+18)*dpr),round((rect.y()-shelf.y()+62)*dpr),
                   round((rect.width()-36)*dpr),round((rect.height()-90)*dpr))
        return colored_body(image,body)

    def test_real_compact_and_overview_endpoints_and_every_sampled_morph_body_have_content(self):
        shelf=self.shelf;native=QRect(shelf.geometry());header=self.header_rects()
        shelf.show_contents();shelf.geometry_morph.animation.pause();shelf.presentation.stop()
        snapshot=shelf._surface_snapshot
        for image in (snapshot.source.toImage(),snapshot.target.toImage()):
            dpr=image.devicePixelRatioF();body=QRect(0,round(54*dpr),image.width(),image.height()-round(54*dpr))
            self.assertGreater(colored_body(image,body),50,'A captured endpoint lost its actual file/button descendants')
        for progress in (0.,.2,.5,.75,.9,1.):
            with self.subTest(progress=progress):
                shelf.geometry_morph._frame(progress);ws.flush()
                self.assertGreater(self.body_pixels(),50,'Body became background-only during mini → overview')
                self.assertEqual(shelf.geometry(),native,'Morph resized its reserved native host')
        shelf.geometry_morph.complete();ws.flush()
        self.assertEqual(self.header_rects(),header)
        self.assertFalse(snapshot.active);self.assertTrue(snapshot.source.isNull());self.assertTrue(snapshot.target.isNull())
        shelf.collapse_contents();shelf.geometry_morph.animation.pause();shelf.presentation.stop()
        for progress in (0.,.2,.5,.75,.9,1.):
            with self.subTest(reverse_progress=progress):
                shelf.geometry_morph._frame(progress);ws.flush()
                self.assertGreater(self.body_pixels(),50,'Body became background-only during overview → mini')
                self.assertEqual(shelf.geometry(),native)
        shelf.geometry_morph.complete();ws.flush();self.assertEqual(self.header_rects(),header)

    def test_capture_restores_nested_transaction_barrier_without_publishing_owner_paint(self):
        shelf=self.shelf;observer=OwnerPaints(shelf);snapshot=SurfaceSnapshot(shelf)
        original_visibility=[not c.isHidden() for c in self.controls()];effect=shelf.card.graphicsEffect()
        with shelf._surface_transaction():
            self.assertFalse(shelf.updatesEnabled());self.assertFalse(shelf.card.updatesEnabled())
            with shelf._surface_transaction():
                image=snapshot.capture_live(shelf.card,self.controls()).toImage()
                self.assertFalse(shelf.updatesEnabled());self.assertFalse(shelf.card.updatesEnabled())
            self.assertFalse(shelf.updatesEnabled())
            self.assertGreater(colored_body(image,QRect(0,54,image.width(),image.height()-54)),50)
            self.assertEqual(observer.events,[],'Capture dispatched a native owner paint inside the transaction')
            self.assertEqual([not c.isHidden() for c in self.controls()],original_visibility)
            self.assertTrue(effect.isEnabled())
        self.assertTrue(shelf.updatesEnabled());self.assertTrue(shelf.card.updatesEnabled())

    def test_failed_offscreen_render_restores_visibility_effect_and_original_updates_policy(self):
        shelf=self.shelf;snapshot=SurfaceSnapshot(shelf);visibility=[not c.isHidden() for c in self.controls()]
        for suppressed in (False,True):
            with self.subTest(suppressed=suppressed):
                shelf.setUpdatesEnabled(not suppressed)
                try:
                    with patch.object(shelf.card,'render',side_effect=RuntimeError('controlled render failure')):
                        with self.assertRaisesRegex(RuntimeError,'controlled render failure'):
                            snapshot.capture_live(shelf.card,self.controls())
                    self.assertEqual(shelf.updatesEnabled(),not suppressed)
                    self.assertEqual([not c.isHidden() for c in self.controls()],visibility)
                    self.assertTrue(shelf.card.graphicsEffect().isEnabled())
                finally:shelf.setUpdatesEnabled(True)


if __name__=='__main__':unittest.main()
