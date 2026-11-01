"""Disabled ink is distinct while Qt interaction/readonly contracts stay intact."""
import os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
os.environ['QT_QPA_PLATFORM']='offscreen'
if os.environ.get('FLICKDOCK_FIELD_BASELINE'):sys.path.insert(0,os.environ['FLICKDOCK_FIELD_BASELINE'])
from PySide6.QtCore import QObject,Signal,Qt,QRect,QPoint
from PySide6.QtGui import QKeySequence,QPalette
from PySide6.QtWidgets import QLineEdit,QKeySequenceEdit,QWidget,QVBoxLayout,QLabel,QWidgetItem
from PySide6.QtTest import QTest
import test_html_native_workspace as ws
from test_html_native_lan import FakeLan
from app_settings import SettingsStore,SettingsDialog,SettingsToggleSwitch
from account_panel import AccountPanel
from lan_panel import LanDialog
from native_ui_common import UiSwitch
from media_preview import MediaPreview
import ui_theme

class Account(QObject):
 account_changed=Signal(object);status_changed=Signal(str);busy_changed=Signal(bool);devices_changed=Signal(list);password_changed=Signal()
 def __init__(self):super().__init__();self.busy=False;self.account=None;self.signed_in=False;self.device_id='fixture';self.devices=[]
 def list_devices(self):self.devices_changed.emit([])
 def login(self,*a):pass
 def register(self,*a):pass

class DisabledFieldTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.windows=[]
  self.native=patch('app_settings._startup_runtime_allowed',return_value=False);self.native.start()
  self.screen=patch('window_chrome.WindowChrome._available',return_value=QRect(0,0,1920,1200));self.screen.start()
  self.reduce=patch('native_ui_common.reduced_motion',return_value=True);self.reduce.start()
 def tearDown(self):
  for w in self.windows:w.hide();w.deleteLater()
  ws.flush();self.reduce.stop();self.screen.stop();self.native.stop();self.temp.cleanup()
 def image(self,w):w.clearFocus();ws.flush();return w.grab().toImage()
 def different_when_disabled(self,w):
  self.assertTrue(w.isVisible());w.setEnabled(True);size=w.size();hint=w.minimumSizeHint();before=self.image(w)
  w.setEnabled(False);after=self.image(w);self.assertNotEqual(before,after,type(w).__name__);self.assertEqual(w.size(),size);self.assertEqual(w.minimumSizeHint(),hint)
 def settings(self,theme):
  store=SettingsStore(Path(self.temp.name)/theme);store.update(theme=theme);w=SettingsDialog(store);self.windows.append(w);w.show();ws.flush();return w
 def test_settings_each_disabled_input_class_has_ink_and_cannot_edit_then_reenables(self):
  for theme in ('light','dark'):
   s=self.settings(theme);s.show_settings_category('sync');ws.flush();line=s.folder_edit;line.setText('fixture')
   self.different_when_disabled(line);old=line.text();QTest.keyClicks(line,'blocked');self.assertEqual(line.text(),old);line.setEnabled(True);line.setFocus(Qt.TabFocusReason);QTest.keyClicks(line,'z');self.assertNotEqual(line.text(),old)
   s.show_settings_category('displays');ws.flush();row=next(iter(s.display_trigger_editor.rows.values()));row.set_expanded(True,animate=False);ws.flush()
   combo=row.edge;self.different_when_disabled(combo);old=combo.currentIndex();QTest.keyClick(combo,Qt.Key_Down);self.assertEqual(combo.currentIndex(),old);combo.setEnabled(True);combo.setFocus(Qt.TabFocusReason);QTest.keyClick(combo,Qt.Key_Down);self.assertNotEqual(combo.currentIndex(),old)
   spin=row.controls['position'];self.different_when_disabled(spin);old=spin.value();QTest.keyClick(spin,Qt.Key_Up);self.assertEqual(spin.value(),old);spin.setEnabled(True);spin.setFocus(Qt.TabFocusReason);QTest.keyClick(spin,Qt.Key_Up);self.assertNotEqual(spin.value(),old)
   slider=row.sliders['position'];self.different_when_disabled(slider);old=slider.value();QTest.keyClick(slider,Qt.Key_Right);self.assertEqual(slider.value(),old);slider.setEnabled(True);slider.setFocus(Qt.TabFocusReason);QTest.keyClick(slider,Qt.Key_Right);self.assertNotEqual(slider.value(),old)
   s.show_settings_category('general');ws.flush();shortcut=next(w for w in s.findChildren(QKeySequenceEdit) if w.isVisible());shortcut.setKeySequence(QKeySequence('Ctrl+1'))
   self.different_when_disabled(shortcut);old=shortcut.keySequence();shortcut.setFocus(Qt.TabFocusReason);self.assertFalse(shortcut.hasFocus());QTest.keyClick(ws.APP.focusWidget() or shortcut.window(),Qt.Key_9,Qt.ControlModifier);self.assertEqual(shortcut.keySequence(),old);shortcut.setEnabled(True);shortcut.setFocus(Qt.TabFocusReason);QTest.keyClick(shortcut,Qt.Key_9,Qt.ControlModifier);QTest.keyClick(shortcut,Qt.Key_Tab);self.assertNotEqual(shortcut.keySequence(),old)
 def test_real_account_busy_disables_visibly_without_changing_username_and_can_resume(self):
  for theme in ('light','dark'):
   s=self.settings(theme);service=Account();panel=AccountPanel(service,theme=theme);s.set_account_panel(panel);s.show_settings_category('account');ws.flush();panel.show_auth();ws.flush();panel.username_edit.setText('fixture')
   before=self.image(panel.username_edit);size=panel.username_edit.size();service.busy=True;service.busy_changed.emit(True);ws.flush()
   self.assertFalse(panel.username_edit.isEnabled());self.assertNotEqual(before,self.image(panel.username_edit));QTest.keyClicks(panel.username_edit,'blocked');self.assertEqual(panel.username_edit.text(),'fixture');self.assertEqual(panel.username_edit.size(),size)
   service.busy=False;service.busy_changed.emit(False);panel.username_edit.setFocus(Qt.TabFocusReason);QTest.keyClicks(panel.username_edit,'z');self.assertNotEqual(panel.username_edit.text(),'fixture')
   self.different_when_disabled(panel.password_edit);QTest.keyClicks(panel.password_edit,'blocked');self.assertEqual(panel.password_edit.text(),'');panel.password_edit.setEnabled(True);panel.password_edit.setFocus(Qt.TabFocusReason);QTest.keyClicks(panel.password_edit,'local-value');self.assertEqual(panel.password_edit.text(),'local-value')
   panel.close_auth();ws.flush()
 def _account_state_refresh_pixels(self):
  def green_pixels(image):
   return sum(image.pixelColor(x,y).green()>image.pixelColor(x,y).red()+20 and
              image.pixelColor(x,y).green()>image.pixelColor(x,y).blue()+10
              for y in range(image.height()) for x in range(image.width()))
  for theme in ('light','dark'):
   service=Account();panel=AccountPanel(service,theme=theme);self.windows.append(panel)
   panel.setFixedSize(640,480);panel.show();ws.flush()
   service.account={'id':'fixture-account','username':'Fixture'};service.signed_in=True
   service.account_changed.emit(service.account);ws.flush()
   label=panel.account_state_label;self.assertTrue(label.isVisible());self.assertTrue(label.property('online'))
   panel.username_edit.setText('preserved fixture input');size=panel.username_label.size();outer=panel.size();rules=panel.styleSheet()
   online=self.image(label);self.assertGreater(green_pixels(online),8)
   ink=label.palette().color(QPalette.WindowText);self.assertGreater(ink.green(),ink.red()+20)
   service.busy=True;service.busy_changed.emit(True);ws.flush()
   self.assertFalse(label.property('online'));self.assertEqual(label.text(),'处理中…')
   self.assertEqual(green_pixels(self.image(label)),0)
   self.assertFalse(panel.username_edit.isEnabled());self.assertEqual(panel.username_edit.text(),'preserved fixture input')
   self.assertEqual(panel.username_label.size(),size);self.assertEqual(label.height(),24);self.assertEqual(panel.size(),outer);self.assertEqual(panel.styleSheet(),rules)
   service.busy=False;service.busy_changed.emit(False);ws.flush()
   self.assertTrue(label.property('online'));self.assertEqual(label.text(),'已登录')
   self.assertEqual(self.image(label),online)
   self.assertTrue(panel.username_edit.isEnabled());self.assertEqual(panel.username_edit.text(),'preserved fixture input')
   self.assertEqual(panel.username_label.size(),size);self.assertEqual(label.height(),24);self.assertEqual(panel.size(),outer);self.assertEqual(panel.styleSheet(),rules)
 def test_account_state_refresh_survives_wrong_private_style_wrapper(self):
  owner=QWidget();self.windows.append(owner);wrong=QWidgetItem(owner)
  with patch.object(QLabel,'style',return_value=wrong) as getter:
   self._account_state_refresh_pixels();getter.assert_not_called()
 def test_account_state_refresh_never_materializes_throwing_private_style_wrapper(self):
  with patch.object(QLabel,'style',side_effect=RuntimeError('private style wrapper unavailable')) as getter:
   self._account_state_refresh_pixels();getter.assert_not_called()
 def test_remote_code_and_devices_disabled_are_distinct_and_readonly_stays_enabled(self):
  for theme in ('light','dark'):
   service=FakeLan();service.mode='wan';store=SettingsStore(Path(self.temp.name)/('wan-'+theme));store.update(theme=theme);w=LanDialog(service,store);self.windows.append(w);w.show();ws.flush();p=w.panel
   code=p.peer_code_input;code.setText('1234');self.different_when_disabled(code);QTest.keyClicks(code,'5');self.assertEqual(code.text(),'1234');code.setEnabled(True);code.setFocus(Qt.TabFocusReason);QTest.keyClick(code,Qt.Key_End);QTest.keyClicks(code,'5');self.assertEqual(code.text(),'12345')
   p.devices.clear();p.devices.addItem('A','a');p.devices.addItem('B','b');p.devices.setCurrentIndex(0);self.different_when_disabled(p.devices);QTest.keyClick(p.devices,Qt.Key_Down);self.assertEqual(p.devices.currentIndex(),0);p.devices.setEnabled(True);p.devices.setFocus(Qt.TabFocusReason);QTest.keyClick(p.devices,Qt.Key_Down);self.assertEqual(p.devices.currentIndex(),1)
   path=p.receive_path;self.assertTrue(path.isReadOnly());self.assertTrue(path.isEnabled());value=path.text();before=self.image(path);QTest.keyClicks(path,'blocked');self.assertEqual(path.text(),value);self.assertTrue(path.isEnabled());self.assertEqual(self.image(path),before)
 def test_history_folder_without_saved_path_is_visibly_disabled_and_never_activates(self):
  for theme in ('light','dark'):
   store=SettingsStore(Path(self.temp.name)/('history-'+theme));store.update(theme=theme);w=LanDialog(FakeLan(),store);self.windows.append(w);w.show();ws.flush();p=w.panel
   p._history=[dict(name='fixture.txt',peer='Mac',state='完成',direction='send',when='10:30',paths=[])];p._render_history();ws.flush();button=p.history_rows[0].reveal_button;self.assertFalse(button.isEnabled());disabled=self.image(button);size=button.size();events=[];button.clicked.connect(lambda:events.append(True));QTest.keyClick(button,Qt.Key_Space);self.assertEqual(events,[])
   button.setEnabled(True);self.assertNotEqual(disabled,self.image(button));button.setFocus(Qt.TabFocusReason);QTest.keyClick(button,Qt.Key_Space);self.assertEqual(events,[True]);self.assertEqual(button.size(),size)
 def test_existing_switch_disabled_states_are_distinct_from_off_and_block_activation(self):
  host=QWidget();layout=QVBoxLayout(host);self.windows.append(host);controls=[SettingsToggleSwitch('Fixture'),UiSwitch()]
  for control in controls:layout.addWidget(control)
  host.show();ws.flush()
  for control in controls:
   control.setChecked(False);self.different_when_disabled(control);QTest.keyClick(control,Qt.Key_Space);self.assertFalse(control.isChecked());control.setEnabled(True);control.setFocus(Qt.TabFocusReason);QTest.keyClick(control,Qt.Key_Space);self.assertTrue(control.isChecked())
 def test_media_seek_and_volume_disabled_ink_and_keyboard_without_native_player(self):
  class RecordedPlayer:
   def __init__(self):self.positions=[]
   def isSeekable(self):return True
   def setPosition(self,value):self.positions.append(value)
  for theme in ('light','dark'):
   ui_theme.set_theme(theme);host=QWidget();layout=QVBoxLayout(host);m=MediaPreview(motion=False);layout.addWidget(m);self.windows.append(host);host.resize(400,340);host.show();ws.flush()
   self.assertFalse(m.seek_slider.isEnabled());self.assertIsNone(m.player)
   path=Path(self.temp.name)/(theme+'-synthetic.bin');path.write_bytes(b'local synthetic marker, never decoded');m.path=str(path)
   player=RecordedPlayer();m.player=player;m.seek_slider.setRange(0,1000);m.seek_slider.setValue(100);player.positions.clear()
   try:
    self.different_when_disabled(m.seek_slider);QTest.keyClick(m.seek_slider,Qt.Key_Right);self.assertEqual(m.seek_slider.value(),100);self.assertEqual(player.positions,[])
    m.seek_slider.setEnabled(True);m.seek_slider.setFocus(Qt.TabFocusReason);QTest.keyClick(m.seek_slider,Qt.Key_Right);self.assertEqual(player.positions,[m.seek_slider.value()]);self.assertGreater(m.seek_slider.value(),100)
    old=m.volume_slider.value();self.different_when_disabled(m.volume_slider);QTest.keyClick(m.volume_slider,Qt.Key_Right);self.assertEqual(m.volume_slider.value(),old)
    m.volume_slider.setEnabled(True);m.volume_slider.setFocus(Qt.TabFocusReason);QTest.keyClick(m.volume_slider,Qt.Key_Right);self.assertGreater(m.volume_slider.value(),old);self.assertIsNone(m.audio_output)
   finally:m.player=None;m.path=''

if __name__=='__main__':unittest.main(verbosity=2)
