"""Actual state paint, keyboard and Qt popup backing; disposable/offscreen only."""
import os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
os.environ['QT_QPA_PLATFORM']='offscreen'
if os.environ.get('FLICKDOCK_CONTROL_BASELINE'):sys.path.insert(0,os.environ['FLICKDOCK_CONTROL_BASELINE'])
from PySide6.QtCore import Qt,QPoint,QRect
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton
import test_html_native_workspace as ws
from test_html_native_lan import FakeLan
import ui_theme
from app_settings import SettingsStore,SettingsDialog
from update_dialog import UpdateDialog
from lan_panel import LanDialog

class StandardStateTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.patch=patch('app_settings._startup_runtime_allowed',return_value=False);self.patch.start()
  self.screen=patch('window_chrome.WindowChrome._available',return_value=QRect(0,0,1920,1200));self.screen.start()
  self.reduce=patch('native_ui_common.reduced_motion',return_value=True);self.reduce.start();self.windows=[]
 def tearDown(self):
  for w in self.windows:w.hide();w.deleteLater()
  ws.flush();self.reduce.stop();self.screen.stop();self.patch.stop();self.temp.cleanup()
 def image(self,w):ws.flush();return w.grab().toImage()
 def color(self,w):
  im=self.image(w);d=im.devicePixelRatio();return im.pixelColor(round(5*d),round(w.height()/2*d))
 def settings(self,theme):
  store=SettingsStore(Path(self.temp.name)/theme);store.update(theme=theme);s=SettingsDialog(store);s.show();self.windows.append(s);ws.flush();return s
 def test_real_busy_buttons_are_visibly_disabled_and_green_available_survives(self):
  for theme in ('light','dark'):
   s=self.settings(theme);s.set_update_state('checking');self.assertFalse(s.update_button.isEnabled());disabled=self.image(s.update_button)
   s.update_button.setEnabled(True);enabled=self.image(s.update_button);self.assertNotEqual(disabled,enabled,(theme,'settings busy'))
   s.set_update_state('available',version='1.8.99');green=self.color(s.update_button);self.assertGreater(green.green(),green.red()+60);self.assertGreater(green.green(),green.blue()+30)
   old_size=s.update_button.size();s.update_button.setDown(True);pressed=self.color(s.update_button);s.update_button.setDown(False);self.assertNotEqual(pressed,green);self.assertEqual(s.update_button.size(),old_size)
   u=UpdateDialog('1.8.24',theme=theme);u.set_state('checking');u.show();self.windows.append(u);ws.flush();self.assertFalse(u.primary_button.isEnabled());disabled=self.image(u.primary_button)
   u.primary_button.setEnabled(True);self.assertNotEqual(disabled,self.image(u.primary_button),(theme,'update busy'))
   plain=self.image(u.primary_button);size=u.primary_button.size();u.primary_button.setDown(True);self.assertTrue(u.primary_button.isDown());self.assertNotEqual(plain,self.image(u.primary_button));u.primary_button.setDown(False);self.assertEqual(u.primary_button.size(),size)
 def test_tab_focus_is_visible_pointer_focus_is_quiet_and_space_keeps_original_action(self):
  for theme in ('light','dark'):
   u=UpdateDialog('1.8.24',theme=theme);u.show();u.activateWindow();self.windows.append(u);ws.flush()
   for control,other in ((u.primary_button,u.secondary_button),(u.secondary_button,u.primary_button)):
    other.setFocus(Qt.OtherFocusReason);plain=self.image(control);control.setFocus(Qt.TabFocusReason);focused=self.image(control)
    self.assertTrue(control.hasFocus());self.assertTrue(control.property('keyboardFocus'));self.assertNotEqual(plain,focused,(theme,control.text()))
    control.clearFocus();control.setFocus(Qt.MouseFocusReason);self.assertFalse(control.property('keyboardFocus'));self.assertEqual(plain,self.image(control))
   events=[];u.request_check.connect(lambda:events.append(True));u.primary_button.setFocus(Qt.TabFocusReason);QTest.keyClick(u.primary_button,Qt.Key_Space);self.assertEqual(events,[True]);self.assertFalse(u.primary_button.autoDefault())
 def test_devices_popup_rounds_opaque_backing_and_keeps_keyboard_model_and_reuse(self):
  for theme in ('light','dark'):
   store=SettingsStore(Path(self.temp.name)/('lan-'+theme));s=LanDialog(FakeLan(),store);s.apply_theme(theme);s.show();self.windows.append(s);ws.flush();combo=s.panel.devices
   combo.clear();combo.addItem('Device A','a');combo.addItem('Device B','b');combo.setCurrentIndex(0);model=combo.model();delegate=combo.itemDelegate();combo.showPopup();ws.flush();popup=combo.view().window()
   self.assertFalse(popup.mask().isEmpty());self.assertFalse(popup.mask().contains(QPoint(0,0)));self.assertTrue(popup.mask().contains(popup.rect().center()));clip=popup._rounded_popup_clip
   popup.resize(popup.width()+18,popup.height()+10);ws.flush();self.assertEqual(popup.mask().boundingRect(),popup.rect())
   combo.view().setCurrentIndex(model.index(0,0));QTest.keyClick(combo.view(),Qt.Key_Down);QTest.keyClick(combo.view(),Qt.Key_Return);ws.flush();self.assertEqual(combo.currentData(),'b')
   for _ in range(4):combo.showPopup();ws.flush();self.assertIs(combo.view().window()._rounded_popup_clip,clip);combo.hidePopup()
   self.assertIs(combo.model(),model);self.assertIs(combo.itemDelegate(),delegate);self.assertEqual(combo.itemText(0),'Device A')

if __name__=='__main__':unittest.main(verbosity=2)
