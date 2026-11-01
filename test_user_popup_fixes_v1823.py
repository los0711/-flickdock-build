"""Local Qt input/pixel fixes; no native clipboard, hooks or desktop input."""
import os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
os.environ['QT_QPA_PLATFORM']='offscreen'
if os.environ.get('FLICKDOCK_POPUP_BASELINE'):sys.path.insert(0,os.environ['FLICKDOCK_POPUP_BASELINE'])
from PySide6.QtCore import QObject,Signal,Qt,QPoint,QTimer,QEvent
from PySide6.QtGui import QImage,QColor,QContextMenuEvent
from PySide6.QtWidgets import QWidget,QFrame,QPushButton
from PySide6.QtTest import QTest
from shiboken6 import isValid
import test_html_native_workspace as ws
APP,flush,until=ws.APP,ws.flush,ws.until
from confirm_clear import ClearConfirmOverlay,confirmation_active
from ui_transitions import WindowFade
import flickdock

class ConfirmFixTests(unittest.TestCase):
 def setUp(self):
  self.owner=QWidget();self.owner.resize(850,610);self.owner.setStyleSheet('background:rgb(80,80,80);')
  self.card=QFrame(self.owner);self.card.setGeometry(10,10,830,590);self.card.setStyleSheet('background:rgb(80,80,80);');self.owner.show();flush()
  self.enabled=patch.object(WindowFade,'enabled',return_value=True);self.enabled.start();self.allowed=patch('ui_transitions.motion_allowed',return_value=True);self.allowed.start()
  # These pixel/ghost cases explicitly exercise animation. Both the imported
  # owner gate and shared frame driver must agree, irrespective of runner OS.
  self.confirm_allowed=patch('confirm_clear.motion_allowed',return_value=True);self.confirm_allowed.start()
  self.preference=patch('native_ui_common.reduced_motion',return_value=False);self.preference.start()
  self.dialog=ClearConfirmOverlay(self.card,3,theme='dark')
 def tearDown(self):
  self.dialog.cancel();self.owner.hide();self.owner.deleteLater();flush();self.preference.stop();self.confirm_allowed.stop();self.allowed.stop();self.enabled.stop()
 def edge(self,widget):
  im=self.owner.grab().toImage();point=widget.mapTo(self.owner,QPoint(3,widget.height()//2));ratio=im.devicePixelRatio();return im.pixelColor(round(point.x()*ratio),round(point.y()*ratio)).red()
 def test_scrim_covers_full_card_from_first_frame_and_never_scales_with_panel(self):
  d=self.dialog;d.open();d._window_fade.animation.pause()
  for t in (0,.25,.5,.75,1):
   d._window_fade._frame(t);flush();expected=round(80*(1-128*d._window_fade.alpha/255))
   self.assertLessEqual(abs(self.edge(d)-expected),2,(t,self.edge(d),expected))
  self.assertTrue(d.presentation.isVisible());self.assertTrue(d.panel.isVisible())
  self.assertIsNone(d.graphicsEffect());self.assertIsNotNone(d.panel.graphicsEffect())
 def test_cancel_keeps_logic_immediate_and_exit_scrim_fades_without_edge_reveal(self):
  d=self.dialog;seen=[];d.finished.connect(seen.append);d.open();d._window_fade.animation.setCurrentTime(180);d.cancel()
  self.assertEqual(seen,[False]);self.assertFalse(d.active);self.assertFalse(d.isVisible());self.assertFalse(confirmation_active(self.owner))
  ghost=d._closing_snapshot;self.assertTrue(ghost.isVisible());self.assertTrue(ghost.testAttribute(Qt.WA_TransparentForMouseEvents))
  ghost.animation.pause();ghost._frame(.5);flush();self.assertLessEqual(abs(self.edge(ghost)-60),2)
  self.owner.hide();flush();self.assertFalse(isValid(ghost) and ghost.isVisible())
 def test_configure_reuses_panel_shadow_and_removes_old_ghost_before_reopen(self):
  d=self.dialog;panel=d.panel;shadow=panel.graphicsEffect();d.open();d.cancel();old=d._closing_snapshot
  d.configure(9,theme='light');self.assertTrue(d.open());flush()
  self.assertIs(d.panel,panel);self.assertIs(panel.graphicsEffect(),shadow);self.assertIn('9',d.title.text());self.assertFalse(isValid(old) and old.isVisible())
 def test_shelf_real_clear_entry_reuses_one_overlay_and_preserves_originals_on_cancel(self):
  f=ws.WorkspaceTests();f.setUp()
  try:
   f.overview();s=f.shelf;seen=[]
   for _ in range(2):
    def cancel():seen.append(s._clear_confirmation);s._clear_confirmation.cancel()
    QTimer.singleShot(10,cancel);self.assertFalse(s.confirm_clear_all());flush()
   self.assertIs(seen[0],seen[1]);self.assertEqual(len(s.findChildren(ClearConfirmOverlay)),1)
   self.assertTrue(all(Path(p).read_bytes()==f'original {i}'.encode() for i,p in enumerate(f.paths)))
  finally:f.tearDown()

class Presenter(QObject):
 changed=Signal()
 def __init__(self):super().__init__();self.state={'phase':'synced','value':('text',{'text':'unaltered preview text'})};self.thumbnail=QImage();self.large=QImage();self._large_requested=False
 def request_large(self):self._large_requested=True

class ClipboardClickFixTests(unittest.TestCase):
 def setUp(self):
  self.f=ws.WorkspaceTests();self.f.setUp();self.shelf=self.f.shelf;self.manager=self.f.manager;self.manager.motion=True
  self.presenter=Presenter();self.manager.clipboard_presenter=self.presenter;self.manager.clipboard_preview_dialog=None
  self.manager.show_clipboard_preview=lambda source=None:flickdock.ShelfManager.show_clipboard_preview(self.manager,source)
  self.allow=patch('clipboard_popup_motion.motion_allowed',return_value=True);self.allow.start()
 def tearDown(self):
  d=self.manager.clipboard_preview_dialog
  if d is not None:d.dismiss(immediate=True);getattr(d._popup_motion,'_finish_pointer_swallow',lambda:None)();d.deleteLater();flush()
  self.allow.stop();self.f.tearDown()
 def open(self):
  source=self.shelf.zones['permanent'].clipboard_button;self.manager.show_clipboard_preview(source);d=self.manager.clipboard_preview_dialog;m=d._popup_motion;m.animation.setCurrentTime(m.animation.duration());flush();return d,m
 def finish(self,m):m.animation.setCurrentTime(m.animation.duration());flush()
 def test_preview_is_smaller_and_original_text_is_noninteractive(self):
  d,m=self.open();self.assertLessEqual(m.target_rect.width(),420);self.assertLess(m.target_rect.height(),330)
  self.assertEqual(d.text.textInteractionFlags(),Qt.NoTextInteraction);self.assertEqual(d.text.focusPolicy(),Qt.NoFocus);self.assertEqual(d.text.toPlainText(),'unaltered preview text')
 def test_every_internal_text_label_badge_image_file_and_x_click_closes_without_interaction(self):
  for target in ('title','badge','status','text','image','info','close_button'):
   if target=='image':self.presenter.large=QImage(32,24,QImage.Format_ARGB32);self.presenter.large.fill(QColor('red'));self.presenter.state['value']=('image',{})
   elif target=='info':self.presenter.large=QImage();self.presenter.state['value']=('files',['original.dat'])
   else:self.presenter.large=QImage();self.presenter.state['value']=('text',{'text':'unaltered preview text'})
   d,m=self.open();widget=d.text.viewport() if target=='text' else getattr(d,target)
   QTest.mouseClick(widget,Qt.LeftButton,pos=widget.rect().center());self.assertEqual(m.target,0.,target);self.assertFalse(d.text.textCursor().hasSelection());self.finish(m);self.assertFalse(d.isVisible(),target)
 def test_long_press_release_after_exit_is_swallowed_and_does_not_click_underlying_button(self):
  d,m=self.open();text=d.text.viewport();global_point=text.mapToGlobal(text.rect().center())
  underneath=QPushButton('underlying',self.shelf);underneath.setGeometry(self.shelf.mapFromGlobal(global_point-QPoint(15,15)).x(),self.shelf.mapFromGlobal(global_point-QPoint(15,15)).y(),30,30);underneath.show()
  released=[];underneath.released.connect(lambda:released.append(True));clicked=[];underneath.clicked.connect(lambda:clicked.append(True))
  QTest.mousePress(text,Qt.LeftButton,pos=text.rect().center());self.assertEqual(m.target,0.);self.finish(m);self.assertTrue(m._app_filter)
  # Simulate native receiver changing after the popup has finished closing.
  underneath.setDown(True);QTest.mouseRelease(underneath,Qt.LeftButton,pos=QPoint(15,15));flush()
  self.assertEqual(clicked,[]);self.assertEqual(released,[]);self.assertFalse(m._app_filter);self.assertFalse(m._release_timer.isActive())
  underneath.setDown(False);underneath.deleteLater()
 def test_outside_action_keeps_its_click_and_reduced_motion_internal_click_closes(self):
  d,m=self.open();button=QPushButton('outside',self.shelf);button.setGeometry(1,1,40,25);button.show();clicked=[];button.clicked.connect(lambda:clicked.append(True))
  QTest.mouseClick(button,Qt.LeftButton,pos=QPoint(2,2));self.assertEqual(clicked,[True]);self.assertEqual(m.target,0.);self.finish(m)
  d,m=self.open()
  with patch('clipboard_popup_motion.motion_allowed',return_value=False):QTest.mouseClick(d.text.viewport(),Qt.LeftButton,pos=QPoint(10,10))
  self.assertFalse(d.isVisible());self.assertFalse(m._app_filter);button.deleteLater()
 def test_right_release_after_exit_cannot_open_an_underlying_context_menu(self):
  d,m=self.open();text=d.text.viewport();point=text.rect().center();global_point=text.mapToGlobal(point)
  class Probe(QObject):
   def __init__(self,parent):super().__init__(parent);self.received=[]
   def eventFilter(self,watched,event):
    if event.type()==QEvent.ContextMenu:self.received.append(True);return True
    return False
  target=QPushButton('underlying',self.shelf);target.setGeometry(1,1,40,25);target.show();probe=Probe(target);target.installEventFilter(probe)
  QTest.mousePress(text,Qt.RightButton,pos=point);self.assertEqual(m.target,0.);self.finish(m)
  QTest.mouseRelease(target,Qt.RightButton,pos=QPoint(1,1));self.assertTrue(m._app_filter)
  APP.sendEvent(target,QContextMenuEvent(QContextMenuEvent.Mouse,QPoint(1,1),global_point));self.assertEqual(probe.received,[])
  self.assertFalse(m._app_filter,(m.presenting,m._swallow_button,m._swallow_context,probe.received));self.assertFalse(m._release_timer.isActive());target.removeEventFilter(probe);target.deleteLater()

if __name__=='__main__':unittest.main(verbosity=2)
