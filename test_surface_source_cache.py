"""Actual Qt source invalidation while animated transforms reuse QWidget pixels."""
import unittest
from unittest.mock import patch
from PySide6.QtCore import QCoreApplication,QEvent,QObject,QPoint,Qt
from PySide6.QtWidgets import QLabel
from PySide6.QtGui import QColor
import test_html_native_workspace as workspace


class Counter(QObject):
    def __init__(self):super().__init__();self.paints=0
    def eventFilter(self,watched,event):
        if event.type()==QEvent.Paint:self.paints+=1
        return False


class SourceCacheTests(unittest.TestCase):
    def setUp(self):
        self.fixture=workspace.WorkspaceTests();self.fixture.setUp()
        self.shelf=self.fixture.shelf;self.preview=self.fixture.overview().preview
        self.counter=Counter();self.shelf.popup.installEventFilter(self.counter)
    def tearDown(self):self.fixture.tearDown()

    def paint(self):
        self.shelf.update();QCoreApplication.sendPostedEvents(None,QEvent.UpdateRequest);workspace.APP.processEvents()

    def test_transform_frames_reuse_content_and_real_text_changes_invalidate_it(self):
        effect=self.shelf.surface_effect
        effect._cache_while=lambda:True
        effect.set_motion(sx=.98,sy=.97);self.paint()
        baseline=self.counter.paints
        for index in range(20):
            effect.set_motion(sx=.96+.002*index,sy=.95+.002*index,dx=index/2);self.paint()
        self.assertEqual(self.counter.paints,baseline,'A pure transform repainted the overview subtree')
        old=effect._source_cache.cacheKey()
        self.preview.show_path(self.fixture.paths[1])
        workspace.until(lambda:not effect._source_cache.isNull() and effect._source_cache.cacheKey()!=old)
        self.assertGreater(self.counter.paints,baseline)
        self.assertNotEqual(effect._source_cache.cacheKey(),old)
        effect._cache_while=lambda:False;before=self.counter.paints
        self.preview.name.setText('Resting widgets always remain live')
        workspace.until(lambda:self.counter.paints>before)

    def test_geometry_and_hide_reopen_do_not_restore_stale_preview_pixels(self):
        effect=self.shelf.surface_effect;effect.set_motion(sx=.98,sy=.98);self.paint()
        previous=effect._source_cache.size()
        self.shelf.collapse_contents();self.shelf.geometry_morph.complete();self.paint()
        self.assertNotEqual(effect._source_cache.size(),previous)
        self.shelf.hide();self.shelf.show();self.shelf.show_contents();self.shelf.geometry_morph.complete();self.paint()
        self.assertEqual(effect._source_cache.deviceIndependentSize().toSize(),self.shelf.card.size())

    def test_blocked_native_transaction_still_captures_real_descendant_pixels(self):
        from surface_morph import SurfaceSnapshot
        marker=QLabel('Visible content',self.shelf.card);marker.setGeometry(80,160,180,40)
        marker.setStyleSheet('background:#e12378;color:white');marker.show();workspace.flush()
        explicitly_blocked=QLabel('blocked',self.shelf.card);explicitly_blocked.setUpdatesEnabled(False)
        snapshot=SurfaceSnapshot(self.shelf)
        try:
            with self.shelf._surface_transaction():
                self.assertFalse(self.shelf.updatesEnabled());self.assertFalse(marker.updatesEnabled())
                image=snapshot.capture_live(self.shelf.card,()).toImage()
                self.assertFalse(self.shelf.updatesEnabled());self.assertFalse(marker.updatesEnabled())
                self.assertFalse(explicitly_blocked.updatesEnabled());self.assertTrue(explicitly_blocked.testAttribute(Qt.WA_ForceUpdatesDisabled))
                ratio=image.devicePixelRatio();point=marker.pos()+QPoint(2,2)
                self.assertEqual(image.pixelColor(round(point.x()*ratio),round(point.y()*ratio)).name(),'#e12378')
            self.assertTrue(marker.updatesEnabled());self.assertFalse(explicitly_blocked.updatesEnabled())
        finally:marker.deleteLater();explicitly_blocked.deleteLater();snapshot.deleteLater();workspace.flush()


if __name__=='__main__':unittest.main(verbosity=2)
