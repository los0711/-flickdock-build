"""Actual drop targets, local Qt events and original disposable payloads."""
import os,tempfile,unittest
os.environ['QT_QPA_PLATFORM']='offscreen'
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PySide6.QtCore import QEvent,QPoint,QPointF,QMimeData,QUrl,Qt,QTimer
from PySide6.QtGui import QMouseEvent,QDragEnterEvent,QDropEvent
from PySide6.QtWidgets import QApplication,QWidget
from lan_panel import _DropZone
from reference_widgets import MiniStack,OverviewEmptyDropZone
from shelf_content import ContentRepository
import ui_theme

APP=QApplication.instance() or QApplication([])
def flush():
    for _ in range(5):APP.processEvents()
def move(widget,point):
    APP.sendEvent(widget,QMouseEvent(QEvent.MouseMove,QPointF(point),QPointF(widget.mapToGlobal(point)),Qt.NoButton,Qt.NoButton,Qt.NoModifier))


class DropTargetTests(unittest.TestCase):
    def setUp(self):
        self.host=QWidget();self.host.resize(950,280)
        self.shelf=SimpleNamespace(content=ContentRepository(),manager=SimpleNamespace(motion=False),preview_cache={})
        self.carrier=QWidget(self.host);self.carrier.setGeometry(10,10,300,250)
        self.mini=MiniStack(self.shelf,self.carrier,motion=False);self.mini.setGeometry(30,20,240,170)
        self.mini.set_stack(self.shelf.content,[],{})
        self.overview=OverviewEmptyDropZone(self.host);self.overview.setGeometry(330,20,280,220);self.overview.setFixedSize(280,220)
        self.transfer=_DropZone(self.host);self.transfer.setGeometry(640,65,280,116);self.transfer.setFixedSize(280,116)
        self.targets=(self.mini,self.overview,self.transfer)
        self.host.show();flush()
    def tearDown(self):
        self.host.hide();self.host.deleteLater();flush();ui_theme.set_theme('light')

    def test_mini_hover_is_only_the_real_rounded_inner_box(self):
        motion=self.mini.drop_motion
        with patch('drop_target_motion.perf_counter',return_value=10.),patch('native_ui_common.reduced_motion',return_value=False):
            move(self.carrier,self.mini.mapTo(self.carrier,QPoint(5,5)))
            self.assertFalse(motion.active);self.assertFalse(motion.timer.isActive())
            rect=self.mini.empty_drop_rect()
            move(self.carrier,self.mini.mapTo(self.carrier,rect.topLeft().toPoint()))
            self.assertFalse(motion.active,'transparent rounded corner is outside the target')
            move(self.carrier,self.mini.mapTo(self.carrier,rect.center().toPoint()))
            self.assertTrue(motion.active);self.assertTrue(motion.timer.isActive())
            motion._tick(10.2);self.assertEqual(motion.value,1.)
            phase=motion.phase
        with patch('drop_target_motion.perf_counter',return_value=10.2),patch('native_ui_common.reduced_motion',return_value=False):
            move(self.carrier,self.mini.mapTo(self.carrier,QPoint(5,5)))
            self.assertFalse(motion.active);motion._tick(10.4)
            self.assertEqual(motion.phase,phase);self.assertEqual(motion.value,0.);self.assertFalse(motion.timer.isActive())

    def test_all_three_targets_hover_ants_reverse_and_stop_on_hide(self):
        for target in self.targets:
            motion=target.drop_motion
            with patch('drop_target_motion.perf_counter',return_value=20.),patch('native_ui_common.reduced_motion',return_value=False):
                motion.pointer_at(motion.rect().center());motion._tick(20.05)
                value=motion.value;self.assertGreater(value,0);self.assertLess(value,1)
            with patch('drop_target_motion.perf_counter',return_value=20.05),patch('native_ui_common.reduced_motion',return_value=False):
                motion.pointer_at(QPointF(-10,-10));self.assertEqual(motion._from,value)
                frozen=motion.phase;motion._tick(20.3);self.assertEqual(motion.phase,frozen)
                self.assertEqual(motion.value,0);self.assertFalse(motion.timer.isActive())
                motion.pointer_at(motion.rect().center());self.assertTrue(motion.timer.isActive())
            target.hide();self.assertFalse(motion.timer.isActive());self.assertEqual(motion.value,0)

    def test_reduced_motion_is_static_and_uses_one_target_clock(self):
        for target in self.targets:
            motion=target.drop_motion
            self.assertEqual(len(motion.findChildren(QTimer)),1)
            with patch('native_ui_common.reduced_motion',return_value=True):
                motion.pointer_at(motion.rect().center());self.assertEqual(motion.value,1)
                self.assertEqual(motion.icon_scale,1);self.assertFalse(motion.timer.isActive())
                phase=motion.phase;motion._tick();self.assertEqual(motion.phase,phase)
                motion.pointer_at(QPointF(-5,-5));self.assertEqual(motion.value,0)

    def test_transfer_payload_and_full_bounds_click_do_not_change(self):
        accepted=[];clicked=[];self.transfer.dropped.connect(accepted.append);self.transfer.clicked.connect(lambda:clicked.append(True))
        APP.sendEvent(self.transfer,QMouseEvent(QEvent.MouseButtonPress,QPointF(2,2),QPointF(self.transfer.mapToGlobal(QPoint(2,2))),Qt.LeftButton,Qt.LeftButton,Qt.NoModifier))
        self.assertEqual(clicked,[True])
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'original.bin';path.write_bytes(b'original bytes')
            mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(path))])
            enter=QDragEnterEvent(QPoint(25,25),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier)
            self.transfer.dragEnterEvent(enter);self.assertTrue(enter.isAccepted());self.assertTrue(self.transfer._active)
            drop=QDropEvent(QPointF(25,25),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier)
            self.transfer.dropEvent(drop);self.assertTrue(drop.isAccepted());self.assertEqual(accepted,[[str(path)]])
            self.assertEqual(path.read_bytes(),b'original bytes');self.assertFalse(self.transfer._active)

    def test_mini_timer_stops_when_content_replaces_empty_target(self):
        motion=self.mini.drop_motion
        with patch('native_ui_common.reduced_motion',return_value=False):
            motion.pointer_at(motion.rect().center());self.assertTrue(motion.timer.isActive())
            ref=self.shelf.content.add_text('local sample');self.mini.set_stack(self.shelf.content,[ref],{})
            self.assertFalse(motion.timer.isActive());self.assertEqual(motion.value,0)

    def test_transfer_content_is_vertically_centered_even_when_hint_hidden(self):
        for height in (116,120,140,220):
            self.transfer.resize(280,height);self.transfer.setFixedHeight(height);flush()
            top=self.transfer.illustration.geometry().top()
            bottom=height-1-self.transfer.title.geometry().bottom()
            self.assertLessEqual(abs(top-bottom),1,(height,top,bottom))

    def test_metadata_refresh_does_not_restart_the_running_ant_clock(self):
        motion=self.mini.drop_motion
        with patch('native_ui_common.reduced_motion',return_value=False):
            motion.pointer_at(motion.rect().center());timer_id=motion.timer.timerId()
            for _ in range(30):
                self.mini.set_stack(self.shelf.content,[],{})
                self.assertEqual(motion.timer.timerId(),timer_id)

    def test_marching_dash_changes_actual_border_pixels_without_moving_geometry(self):
        for target in self.targets:
            motion=target.drop_motion;geometry=target.geometry()
            with patch('drop_target_motion.perf_counter',return_value=40.),patch('native_ui_common.reduced_motion',return_value=False):
                motion.pointer_at(motion.rect().center());motion._tick(40.2)
                first=target.grab().toImage();motion._tick(40.35);second=target.grab().toImage()
                rect=motion.rect()
                changes=sum(first.pixelColor(x,y)!=second.pixelColor(x,y)
                    for y in range(max(0,round(rect.top())-1),round(rect.top())+2)
                    for x in range(round(rect.left()+18),round(rect.right()-18)))
                self.assertGreater(changes,0,type(target).__name__);self.assertEqual(target.geometry(),geometry)


if __name__=='__main__':unittest.main(verbosity=2)
