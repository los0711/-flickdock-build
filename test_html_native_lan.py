"""Native LAN reference geometry and bindings; no sockets or real profile."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QLabel

from app_settings import SettingsStore
from lan_panel import LanDialog

APP=QApplication.instance() or QApplication([])
for source in (Path(__file__).parent/'fonts').glob('HarmonyOS_Sans_SC_*.ttf'):
    QFontDatabase.addApplicationFont(str(source))


class FakeLan(QObject):
    peers_changed=Signal(list);offer_received=Signal(dict);progress=Signal(dict)
    completed=Signal(dict);status_changed=Signal(str)
    def __init__(self):super().__init__();self.calls=[]
    def peers(self):return [{'id':'mac','name':"Roger's MacBook Pro",'host':'192.168.1.104'}]
    def send(self,peer,paths):self.calls.append(('send',peer,paths));return 'outgoing'
    def accept(self,identifier,path):self.calls.append(('accept',identifier,path));return True
    def reject(self,identifier):self.calls.append(('reject',identifier))
    def cancel(self,identifier):self.calls.append(('cancel',identifier))


class NativeLanTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-html-lan-')
        self.base=Path(self.temp.name);self.service=FakeLan()
        self.settings=SettingsStore(self.base/'profile')
        self.dialog=LanDialog(self.service,self.settings)
        self.dialog.show();APP.processEvents()

    def tearDown(self):
        self.dialog.close();self.dialog.deleteLater();APP.processEvents();self.temp.cleanup()

    def test_reference_client_geometry_and_native_buttons_remain_modeless(self):
        self.assertEqual(self.dialog.size().toTuple(),(980,704))
        self.assertEqual((self.dialog.card.width(),self.dialog.card.height()),(936,660))
        self.assertEqual(self.dialog.minimumSize().toTuple(),(824,660))
        self.assertEqual(self.dialog.title_bar.height(),48)
        panel=self.dialog.panel
        self.assertAlmostEqual(panel.send_column.width()/panel.activity_column.width(),5/7,delta=.015)
        self.assertFalse(self.dialog.isModal())
        seen=[];self.dialog.overview_requested.connect(lambda:seen.append('overview'))
        self.dialog.back_button.click();self.assertEqual(seen,['overview'])

    def test_real_six_digit_offer_and_service_actions_survive_redesign(self):
        panel=self.dialog.panel
        offer={'id':'incoming','peer':'Mac Studio','count':1,'names':['Ps2025.7z'],
               'total':5*1024**3,'code':'964191'}
        panel.show_offer(offer);APP.processEvents()
        row=panel._rows['incoming'];card=row['extra']
        self.assertEqual(card.findChild(QLabel,'lanOfferCodeValue').text(),'964191')
        panel._accept('incoming')
        self.assertEqual(self.service.calls[-1],('accept','incoming',panel.receive_path.text()))
        self.assertTrue(row['heading'].isVisible());self.assertFalse(card.isVisible())
        row['cancel'].click();self.assertEqual(self.service.calls[-1],('cancel','incoming'))

    def test_macos_control_variant_keeps_modeless_navigation(self):
        with patch('lan_panel.sys.platform','darwin'):
            dialog=LanDialog(self.service,self.settings)
            dialog.apply_theme('dark');dialog.show();APP.processEvents()
            self.assertEqual(len(dialog.traffic_buttons),3)
            self.assertEqual(dialog.traffic_buttons[0].size().width(),12)
            self.assertFalse(dialog.traffic_buttons[2].isEnabled())
            if os.environ.get('FLICKDOCK_CAPTURE_LAN'):
                output=Path(__file__).resolve().parents[2]/'outputs'
                dialog.grab().save(str(output/'html-native-lan-mac-controls.png'))
            dialog.close();dialog.deleteLater();APP.processEvents()

    def test_no_clip_with_offer_active_transfer_and_recent_records(self):
        panel=self.dialog.panel
        paths=[self.base/'Product_Hero_4K_Render.png',self.base/'Promo_Reel_4K_Master.mp4']
        for path in paths:path.write_bytes(b'original')
        panel.queue_paths([str(path) for path in paths])
        panel._history=[{'when':'10-28 14:28','name':name,'peer':'Studio Workstation',
                         'state':'完成','direction':'receive'} for name in
                        ['Booth_M31_Print_Ready.pdf','UI_Icons_Bundle.zip','notes.txt']]
        panel._render_history()
        panel._progress({'id':'progress','direction':'send','peer':'Studio Workstation',
                         'state':'transferring','name':'Promo_Reel_4K_Master.mp4',
                         'received':106*1024**2,'total':148*1024**2})
        panel.show_offer({'id':'incoming','peer':"Roger's MacBook Pro",'count':3,
                         'names':['Design_System_v2.fig'],'total':86*1024**2,'code':'849249'})
        APP.processEvents()
        self.assertGreaterEqual(self.dialog.card.height(),540)
        for widget in (panel.history_body,panel.receive_path,panel.devices,panel.send_button):
            rect=widget.rect(); top=widget.mapTo(panel,rect.topLeft()); bottom=widget.mapTo(panel,rect.bottomRight())
            self.assertGreaterEqual(top.y(),0)
            self.assertLessEqual(bottom.y(),panel.height())
        if os.environ.get('FLICKDOCK_CAPTURE_LAN'):
            output=Path(__file__).resolve().parents[2]/'outputs';output.mkdir(exist_ok=True)
            for theme in ('dark','light'):
                self.dialog.apply_theme(theme);APP.processEvents()
                self.dialog.grab().save(str(output/f'html-native-lan-{theme}.png'))


if __name__=='__main__':unittest.main(verbosity=2)
