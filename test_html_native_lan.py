"""Native LAN reference geometry and bindings; no sockets or real profile."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject, Signal, Qt, QPoint
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from app_settings import SettingsStore
from lan_panel import LanDialog

APP=QApplication.instance() or QApplication([])
from native_ui_common import configure_reference_font
configure_reference_font(APP)


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
        area=self.dialog.chrome._available()
        self.assertEqual(self.dialog.size().toTuple(),(min(980,area.width()-16),min(704,area.height()-16)))
        self.assertEqual((self.dialog.card.width(),self.dialog.card.height()),(self.dialog.width()-44,self.dialog.height()-44))
        self.assertEqual(self.dialog.minimumSize().toTuple(),(min(824,area.width()-16),min(660,area.height()-16)))
        self.assertEqual(self.dialog.title_bar.height(),48)
        panel=self.dialog.panel
        self.assertAlmostEqual(panel.left_column.width()/panel.activity_scroll.width(),1,delta=.005)
        self.assertFalse(self.dialog.isModal())
        seen=[];self.dialog.overview_requested.connect(lambda:seen.append('overview'))
        self.dialog.back_button.click();self.assertEqual(seen,['overview'])

    def test_real_six_digit_offer_and_service_actions_survive_redesign(self):
        panel=self.dialog.panel
        offer={'id':'incoming','peer':'Mac Studio','count':1,'names':['Ps2025.7z'],
               'total':5*1024**3,'code':'964191'}
        panel.show_offer(offer);APP.processEvents()
        row=panel._rows['incoming'];card=row['extra']
        self.assertEqual(card.findChild(QPushButton,'lanOfferCodeValue').text(),'964191')
        panel._accept('incoming')
        self.assertEqual(self.service.calls[-1],('accept','incoming',panel.receive_path.text()))
        self.assertTrue(row['heading'].isVisible());self.assertFalse(card.isVisible())
        row['cancel'].click();self.assertEqual(self.service.calls[-1],('cancel','incoming'))

    def test_busy_progress_returns_to_determinate_and_stops_its_frame_pulse(self):
        panel=self.dialog.panel
        with patch('native_ui_common.reduced_motion',return_value=False):
            panel._progress(dict(id='job',peer='Mac',direction='send',state='connecting'))
            APP.processEvents();bar=panel._rows['job']['bar']
            self.assertEqual((bar.minimum(),bar.maximum()),(0,0));self.assertTrue(bar._frames.isActive())
            panel._progress(dict(id='job',peer='Mac',direction='send',state='transferring',received=512,total=1024))
            APP.processEvents();self.assertEqual((bar.minimum(),bar.maximum(),bar.value()),(0,1000,500))
            self.assertFalse(bar._frames.isActive())
            panel._progress(dict(id='job',peer='Mac',direction='send',state='complete'))
            APP.processEvents();self.assertTrue(bar.isHidden());self.assertFalse(bar._frames.isActive())

    def test_device_popup_has_visible_antialiased_rounded_frame_in_both_themes(self):
        combo=self.dialog.panel.devices
        for theme in ('light','dark'):
            self.dialog.apply_theme(theme);APP.processEvents();combo.showPopup();APP.processEvents()
            popup=combo.view().window();image=popup.grab().toImage()
            self.assertTrue(popup.testAttribute(Qt.WA_TranslucentBackground))
            self.assertTrue(popup.windowFlags()&Qt.FramelessWindowHint)
            self.assertTrue(popup.mask().isEmpty())
            for x,y in ((0,0),(image.width()-1,0),(0,image.height()-1),(image.width()-1,image.height()-1)):
                self.assertEqual(image.pixelColor(x,y).alpha(),0,(theme,x,y))
            self.assertGreater(image.pixelColor(image.width()//2,3).alpha(),200)
            self.assertTrue(any(0<image.pixelColor(x,y).alpha()<255
                                for x in range(12) for y in range(12)))
            self.assertGreaterEqual(combo.view().x(),4)
            view=combo.view();rect=view.visualRect(view.model().index(0,0))
            self.assertGreaterEqual(view.viewport().height(),rect.bottom()+1)
            self.assertGreaterEqual(popup.height()-view.geometry().bottom()-1,4)
            combo.hidePopup();APP.processEvents()

    def test_windows_device_popup_fades_on_transparent_host_and_restores_preference(self):
        from PySide6.QtCore import QAbstractAnimation
        combo=self.dialog.panel.devices
        for theme in ('light','dark'):
            self.dialog.apply_theme(theme);APP.processEvents()
            preference=QApplication.isEffectEnabled(Qt.UI_AnimateCombo)
            with patch('lan_panel.sys.platform','win32'),patch('lan_panel.reduced_motion',return_value=False):
                combo.showPopup();APP.processEvents();popup=combo.view().window();fade=popup._device_fade
                self.assertEqual(QApplication.isEffectEnabled(Qt.UI_AnimateCombo),preference)
                self.assertEqual(fade.animation.state(),QAbstractAnimation.Running)
                fade.animation.pause()
                for point in (0,70,140):
                    fade.animation.setCurrentTime(point);image=popup.grab().toImage()
                    self.assertEqual(image.pixelColor(0,0).alpha(),0,(theme,point))
                    self.assertGreater(image.pixelColor(image.width()//2,3).alpha(),200,(theme,point))
                combo.hidePopup();APP.processEvents()
                self.assertEqual(fade.animation.state(),QAbstractAnimation.Stopped)
                self.assertAlmostEqual(popup.windowOpacity(),1.,places=2)

    def test_clear_buttons_share_default_and_red_hover_in_both_themes(self):
        panel=self.dialog.panel
        for theme in ('light','dark'):
            self.dialog.apply_theme(theme);APP.processEvents();pixels=[]
            for control in (panel.clear_queue,panel.clear_history):
                control.setEnabled(True);QTest.mouseMove(control,QPoint(-20,-20));APP.processEvents()
                normal=control.grab().toImage().pixelColor(5,control.height()//2)
                QTest.mouseMove(control,control.rect().center());APP.processEvents()
                hovered=control.grab().toImage().pixelColor(5,control.height()//2)
                self.assertGreater(hovered.red(),hovered.green());self.assertNotEqual(normal,hovered)
                pixels.append((normal,hovered,control.size()))
            self.assertEqual(pixels[0],pixels[1])

    def test_history_delete_has_visible_hover_and_press_in_both_themes(self):
        panel=self.dialog.panel
        panel._history=[{'when':'11-15 14:43','name':'report.zip','peer':'Mac',
                         'state':'已取消','direction':'send'}]
        panel._render_history();APP.processEvents()
        button=panel.history_rows[0].remove_button
        for theme in ('light','dark'):
            self.dialog.apply_theme(theme);APP.processEvents()
            QTest.mouseMove(panel,QPoint(0,0));APP.processEvents()
            normal=button.grab().toImage().pixelColor(5,button.height()//2)
            QTest.mouseMove(button,button.rect().center());APP.processEvents()
            hovered=button.grab().toImage().pixelColor(5,button.height()//2)
            QTest.mousePress(button,Qt.LeftButton);APP.processEvents()
            pressed=button.grab().toImage().pixelColor(5,button.height()//2)
            QTest.mouseRelease(button,Qt.LeftButton,pos=QPoint(-5,-5));APP.processEvents()
            self.assertNotEqual(normal,hovered,theme)
            self.assertNotEqual(hovered,pressed,theme)

    def test_macos_control_variant_keeps_modeless_navigation(self):
        with patch('lan_panel.sys.platform','darwin'):
            dialog=LanDialog(self.service,self.settings)
            dialog.apply_theme('dark');dialog.show();APP.processEvents()
            self.assertEqual(len(dialog.traffic_buttons),3)
            self.assertEqual(dialog.traffic_buttons[0].size().width(),12)
            self.assertTrue(dialog.traffic_buttons[2].isEnabled())
            previous=dialog.geometry();dialog.traffic_buttons[2].click();APP.processEvents()
            self.assertNotEqual(dialog.geometry(),previous)
            dialog.traffic_buttons[2].click();APP.processEvents();self.assertEqual(dialog.geometry(),previous)
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
        QTest.qWait(30)
        self.assertGreaterEqual(self.dialog.card.height(),540)
        self.assertTrue(panel.destination_bar.rect().contains(panel.receive_path.mapTo(panel.destination_bar,panel.receive_path.rect().bottomRight())))
        self.assertTrue(panel.left_column.rect().contains(panel.send_button.mapTo(panel.left_column,panel.send_button.rect().bottomRight())))
        for widget in (panel.history_scroll,panel.devices):
            scroll=next(area for area in (panel.send_scroll,panel.activity_scroll)
                        if area.widget().isAncestorOf(widget))
            viewport=scroll.viewport();bar=scroll.verticalScrollBar()
            top=widget.mapTo(viewport,widget.rect().topLeft()).y()
            bottom=widget.mapTo(viewport,widget.rect().bottomRight()).y()
            if top<0:bar.setValue(bar.value()+top-8)
            elif bottom>=viewport.height():bar.setValue(bar.value()+bottom-viewport.height()+9)
            APP.processEvents()
            bounds=(widget.objectName(),widget.size().toTuple(),viewport.size().toTuple(),
                    widget.mapTo(viewport,widget.rect().topLeft()).toTuple(),
                    widget.mapTo(viewport,widget.rect().bottomRight()).toTuple())
            self.assertTrue(viewport.rect().contains(widget.mapTo(viewport,widget.rect().topLeft())),bounds)
            self.assertTrue(viewport.rect().contains(widget.mapTo(viewport,widget.rect().bottomRight())),bounds)
        # History content can scroll independently; every record must be reachable.
        for row in panel.history_rows:
            scroll=panel.history_scroll;viewport=scroll.viewport();bar=scroll.verticalScrollBar()
            top=row.mapTo(viewport,row.rect().topLeft()).y()
            bottom=row.mapTo(viewport,row.rect().bottomRight()).y()
            if top<0:bar.setValue(bar.value()+top)
            elif bottom>=viewport.height():bar.setValue(bar.value()+bottom-viewport.height()+1)
            APP.processEvents()
            self.assertTrue(viewport.rect().contains(row.mapTo(viewport,row.rect().topLeft())))
            self.assertTrue(viewport.rect().contains(row.mapTo(viewport,row.rect().bottomRight())))
        if os.environ.get('FLICKDOCK_CAPTURE_LAN'):
            output=Path(__file__).resolve().parents[2]/'outputs';output.mkdir(exist_ok=True)
            for theme in ('dark','light'):
                self.dialog.apply_theme(theme);APP.processEvents()
                self.dialog.grab().save(str(output/f'html-native-lan-{theme}.png'))


if __name__=='__main__':unittest.main(verbosity=2)
