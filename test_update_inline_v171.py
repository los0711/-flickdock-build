"""Settings inline updates: offscreen, temporary profiles, recorded launches only."""
import os
import sys
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
if len(sys.argv) > 1 and sys.argv[1] in ('1.0', '1.25', '2.0'):
    os.environ['QT_SCALE_FACTOR'] = sys.argv.pop(1)

import copy
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject, QCoreApplication, QEvent, QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QLineEdit, QVBoxLayout

import app_settings as settings
import update_controller as subject
from update_client import UpdateClient, DEFAULT_ALLOWED_HOSTS

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
QFontDatabase.addApplicationFont(str(Path(__file__).with_name('fonts')/'HarmonyOS_Sans_SC_Regular.ttf'))
APP.setFont(QFont('HarmonyOS Sans SC', 11))


def wait(predicate, timeout=3):
    deadline = time.perf_counter()+timeout
    while not predicate() and time.perf_counter()<deadline:
        APP.processEvents()
        time.sleep(.002)
    return bool(predicate())


class Client(QObject):
    checking=Signal(bool); update_available=Signal(dict,bool); up_to_date=Signal(bool)
    failed=Signal(str,bool); progress=Signal(int,int); verified_file=Signal(str,dict,bool)
    cancelled=Signal(bool)
    allowed_hosts=DEFAULT_ALLOWED_HOSTS

    def __init__(self):
        super().__init__()
        self.busy=False
        self.calls=[]

    def check(self, manual=False):
        self.calls.append(('check',manual));self.busy=True;self.checking.emit(manual)
        return True

    def available(self, manifest, manual=True):
        self.busy=False;self.update_available.emit(copy.deepcopy(manifest),manual)

    def download(self, manifest=None, manual=True):
        self.calls.append(('download',manifest['version'],manual));self.busy=True
        self.progress.emit(0,manifest['installer']['size'])
        return True

    def complete(self, path, manifest):
        self.busy=False;self.verified_file.emit(str(path),copy.deepcopy(manifest),True)

    def cancel(self):
        self.calls.append(('cancel',));self.busy=False;self.cancelled.emit(True)
        return True

    def close(self):
        self.busy=False


class InlineUpdates(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='flickdock-inline-update-')
        self.base=Path(self.temporary.name)
        self.env=patch.dict(os.environ,APPDATA=str(self.base/'profile'));self.env.start()
        self.registry_read=patch.object(settings,'_read_startup_value');self.read=self.registry_read.start()
        self.registry_write=patch.object(settings,'_write_startup_value');self.write=self.registry_write.start()
        self.runtime=patch.object(settings,'_startup_runtime_allowed',return_value=False);self.runtime.start()
        self.store=settings.SettingsStore(self.base/'profile');self.store.update(dock_length=90)
        self.dialog=settings.SettingsDialog(self.store)
        self.events=[];self.client=Client()
        self.installed=self.base/'Program Files'/'FlickDock.exe'
        self.installed.parent.mkdir();self.installed.write_bytes(b'MZ-current')
        self.body=b'MZ'+bytes(range(256))*8192
        self.manifest=dict(schema_version=1,version='1.8.0',published_at='2026-10-26T00:00:00Z',notes='fixture',
            installer=dict(url='https://gitee.com/releases/setup.exe',size=len(self.body),sha256=hashlib.sha256(self.body).hexdigest()))
        self.path=self.store.base/'updates'/'1.8.0'/'setup.exe'
        self.path.parent.mkdir(parents=True);self.path.write_bytes(self.body)
        self.controller=subject.UpdateController('1.7.0',self.store.base,settings=self.store,client=self.client,
            installed_exe=self.installed,quit_callback=lambda:self.events.append(('quit',)),
            popen=self.popen,defer_ms=20)
        self.controller.state_changed.connect(lambda state,p:self.dialog.set_update_state(state,**p))
        self.dialog.update_requested.connect(self.controller.activate)
        self.dialog.set_update_state(self.controller.state,**self.controller.state_payload)

    def tearDown(self):
        self.controller.close()
        self.assertTrue(wait(lambda:not self.controller._jobs))
        self.dialog.close();self.dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.read.assert_not_called();self.write.assert_not_called()
        self.runtime.stop();self.registry_read.stop();self.registry_write.stop();self.env.stop()
        self.temporary.cleanup()

    def popen(self,args,**options):
        self.events.append(('popen',args,options));return object()

    def click(self):
        QTest.mouseClick(self.dialog.update_button,Qt.MouseButton.LeftButton)
        APP.processEvents()

    def complete_and_install(self,path,manifest):
        self.client.complete(path,manifest)
        if self.controller.state=='ready':
            self.assertEqual(self.controller._jobs,{})
            self.assertEqual(self.dialog.update_button.text(),'重启并安装')
            self.click()

    def offer(self):
        self.dialog.show();APP.processEvents();self.click()
        self.assertEqual(self.controller.state,'checking')
        self.client.available(self.manifest)
        self.assertEqual(self.dialog.update_button.text(),'立即更新')

    def begin_download(self, manifest=None):
        self.click()
        self.assertEqual(self.controller.state,'checking')
        self.client.available(self.manifest if manifest is None else manifest)
        self.assertEqual(self.controller.state,'downloading')

    def test_settings_download_then_explicit_restart_keeps_one_shared_controller(self):
        self.offer()
        top={id(w) for w in APP.topLevelWidgets()}
        self.assertFalse(hasattr(self.controller,'dialog'))
        self.assertEqual(self.events,[])
        saved=self.store.path.read_bytes()
        self.dialog.dock_position_spin.setValue(61)
        self.begin_download()
        self.assertEqual(self.client.calls,[('check',True),('check',True),('download','1.8.0',True)])
        self.assertFalse(self.dialog.update_button.isEnabled())
        self.client.progress.emit(len(self.body)//4,len(self.body))
        self.assertIn('25%',self.dialog.update_status_label.text())
        self.complete_and_install(self.path,self.manifest)
        self.assertTrue(wait(lambda:len(self.events)==2))
        self.assertEqual([e[0] for e in self.events],['popen','quit'])
        arguments,options=self.events[0][1:]
        self.assertIn('/DIR='+str(self.installed.parent),arguments)
        self.assertIn('/FDParentPid='+str(os.getpid()),arguments)
        self.assertFalse(options['shell'])
        self.assertEqual(top,{id(w) for w in APP.topLevelWidgets()})
        self.assertEqual(self.store.path.read_bytes(),saved)
        self.assertEqual(self.dialog.dock_position_spin.value(),61)

    def test_install_click_refreshes_old_offer_and_never_falls_back_to_it(self):
        self.offer()
        self.click()
        self.assertEqual(self.controller.state,'checking')
        self.assertEqual([call for call in self.client.calls if call[0]=='download'],[])
        current=copy.deepcopy(self.manifest)
        current['version']='1.8.1'
        latest_path=self.store.base/'updates'/'1.8.1'/'setup.exe'
        latest_path.parent.mkdir(parents=True)
        latest_path.write_bytes(self.body)
        self.client.available(current)
        self.assertEqual(self.client.calls[-1],('download','1.8.1',True))
        self.complete_and_install(latest_path,current)
        self.assertTrue(wait(lambda:len(self.events)==2))
        self.assertIn('1.8.1',self.events[0][1][0])

    def test_failed_refresh_does_not_download_stale_offer(self):
        self.offer()
        self.click()
        self.client.busy=False
        self.client.failed.emit('网络暂时不可用',True)
        self.assertEqual(self.controller.state,'error')
        self.assertEqual([call for call in self.client.calls if call[0]=='download'],[])
        self.assertEqual(self.events,[])

    def test_automatic_states_only_never_show_or_execute_and_defer_tray_notice(self):
        notices=[];busy=[True]
        # Exercise notification-only compatibility without an auto-download preference.
        self.controller.settings=None
        self.controller._is_busy=lambda:busy[0]
        self.controller.notification_shown.connect(notices.append)
        self.controller.check_automatically()
        self.assertEqual(self.client.calls,[])
        busy[0]=False
        self.assertTrue(wait(lambda:bool(self.client.calls)))
        busy[0]=True;self.client.available(self.manifest,False)
        self.assertEqual(self.controller.state,'available')
        self.assertFalse(self.dialog.isVisible());self.assertEqual(notices,[])
        busy[0]=False;self.assertTrue(wait(lambda:bool(notices)))
        self.assertEqual(notices,['1.8.0'])
        self.assertFalse(self.dialog.isVisible());self.assertEqual(self.events,[])
        self.controller.open();self.client.busy=False;self.client.up_to_date.emit(False)
        self.assertEqual(self.controller.state,'latest');self.assertFalse(self.dialog.isVisible())
        self.controller.open();self.client.busy=False;self.client.failed.emit('offline',False)
        self.assertEqual(self.controller.state,'error');self.assertFalse(self.dialog.isVisible())

    def test_download_cancel_ignores_late_completion_and_requires_new_click(self):
        self.offer();self.begin_download();self.controller.cancel()
        self.assertEqual(self.controller.state,'cancelled')
        self.client.progress.emit(len(self.body),len(self.body))
        self.complete_and_install(self.path,self.manifest)
        APP.processEvents()
        self.assertEqual(self.controller.state,'cancelled');self.assertEqual(self.events,[])
        self.begin_download();self.complete_and_install(self.path,self.manifest)
        self.assertTrue(wait(lambda:len(self.events)==2))

    def test_settings_close_keeps_authorized_task_and_reopen_keeps_real_state(self):
        self.offer();self.begin_download()
        self.dialog.reject();APP.processEvents()
        self.assertEqual(self.controller.state,'downloading')
        self.client.complete(self.path,self.manifest)
        self.assertEqual(self.controller.state,'ready');self.assertEqual(self.events,[])
        self.dialog.show();APP.processEvents()
        self.assertEqual(self.dialog.update_button.text(),'重启并安装')
        self.click();self.assertTrue(wait(lambda:len(self.events)==2))
        self.assertEqual(len([c for c in self.client.calls if c[0]=='download']),1)

    def test_controller_close_during_hash_suppresses_late_handoff_and_releases_lease(self):
        self.offer();self.begin_download()
        entered,release=threading.Event(),threading.Event();real=subject.verify_installer
        def delayed(*args,**kwargs):
            entered.set();release.wait(3);kwargs.pop('cancelled',None)
            return real(*args,**kwargs)
        with patch.object(subject,'verify_installer',delayed):
            self.complete_and_install(self.path,self.manifest)
            self.assertTrue(wait(entered.is_set))
            self.controller.close();release.set()
            self.assertTrue(wait(lambda:not self.controller._jobs))
        self.assertEqual(self.events,[])
        self.path.unlink()

    def test_download_spawn_and_corrupt_file_failures_retry_in_place(self):
        self.offer();self.begin_download();self.client.busy=False;self.client.failed.emit('download failed',True)
        self.assertEqual(self.dialog.update_button.text(),'重试更新')
        self.begin_download()
        def denied(*args,**kwargs):raise OSError('spawn denied')
        self.controller._popen=denied;self.complete_and_install(self.path,self.manifest)
        self.assertTrue(wait(lambda:self.controller.state=='error'))
        self.assertIn('spawn denied',self.controller.state_payload['error'])
        self.assertEqual(self.events,[])
        self.controller._popen=self.popen;self.begin_download();self.complete_and_install(self.path,self.manifest)
        self.assertTrue(wait(lambda:len(self.events)==2))

    def test_tamper_unrequested_callback_and_path_guards(self):
        self.offer()
        self.complete_and_install(self.path,self.manifest);APP.processEvents()
        self.assertEqual(self.events,[]);self.assertEqual(self.controller.state,'available')
        self.begin_download();self.path.write_bytes(b'MZ'+b'X'*(len(self.body)-2))
        self.complete_and_install(self.path,self.manifest)
        self.assertTrue(wait(lambda:self.controller.state=='error'))
        self.assertEqual(self.events,[])
        outside=self.base/'outside.exe';outside.write_bytes(self.body)
        for p,current in ((outside,'1.7.0'),(self.path,'1.8.0')):
            with self.assertRaises(ValueError):
                subject.verify_installer(p,self.manifest,current_version=current,updates_dir=self.store.base/'updates')
        self.path.write_bytes(self.body)
        lease=subject.verify_installer(self.path,self.manifest,current_version='1.7.0',updates_dir=self.store.base/'updates')
        try:
            if os.name=='nt':
                with self.assertRaises(OSError):self.path.unlink()
                with self.assertRaises(OSError):self.path.open('wb')
            with self.assertRaises(ValueError):
                subject.launch_installer(lease,installed_exe=self.installed,quit_callback=lambda:None)
        finally:lease.close()

    def test_rounded_surface_small_close_account_slot_and_all_inline_states(self):
        saved=self.store.path.read_bytes();self.dialog.show();self.dialog.resize(892,632);APP.processEvents()
        for theme in ('light','dark'):
            self.dialog.apply_theme(theme)
            for state in ('idle','checking','latest','available','downloading','verifying','ready','error','cancelled','installing'):
                self.dialog.set_update_state(state,version='1.8.0',received=25,total=100,error='fixture error')
                APP.processEvents()
                children=(self.dialog.version_label,self.dialog.update_status_label,self.dialog.update_button,self.dialog.close_button)
                positions=[QRectF(child.mapTo(self.dialog.title_bar,QPoint()),child.size()) for child in children]
                for rect in positions:
                    if not rect.isEmpty():self.assertTrue(QRectF(self.dialog.title_bar.rect()).contains(rect))
                for left,right in zip(positions,positions[1:]):self.assertLess(left.right(),right.left())
                self.assertTrue(self.dialog.surface.rect().contains(self.dialog.save_button.geometry()))
                self.assertTrue(self.dialog.surface.rect().contains(self.dialog.cancel_button.geometry()))
            button=self.dialog.close_button
            self.assertEqual(button.size().width(),44 if getattr(button,'role',None)=='close' else 28);self.assertEqual(button.glyph_rect.width(),14)
            self.assertEqual(button.glyph_rect.center(),QRectF(button.rect()).center())
            rendered=self.dialog.grab().toImage()
            dpr=rendered.devicePixelRatio()
            pixel=lambda x,y:rendered.pixelColor(round(x*dpr),round(y*dpr)).alpha()
            middle=self.dialog.width()//2
            self.assertEqual(self.dialog.surface.size().width(),860)
            self.assertEqual(self.dialog.surface.size().height(),600)
            self.assertEqual(pixel(0,0),0)
            self.assertLess(pixel(middle,3),32,'the outer shadow gutter stays transparent')
            self.assertGreater(pixel(12,30),0,'the card casts a visible rounded shadow')
            self.assertLess(pixel(12,30),100,'the shadow does not become a second glass frame')
            self.assertGreaterEqual(pixel(middle,20),214,'the source material retains its 84% surface alpha')
            self.assertGreaterEqual(pixel(20,30),214,'the rounded material is fully painted past its corner')
            render_dir=os.getenv('FLICKDOCK_INLINE_RENDER_DIR')
            if render_dir:
                target=Path(render_dir);target.mkdir(parents=True,exist_ok=True)
                self.dialog.set_update_state('available',version='1.8.0');APP.processEvents()
                self.dialog.grab().save(str(target/f'inline-settings-{theme}-{os.getenv("QT_SCALE_FACTOR","1")}.png'))
        card=QFrame();layout=QVBoxLayout(card);layout.addWidget(QLabel('Account fixture'));editor=QLineEdit(card);layout.addWidget(editor)
        original=self.dialog.body_layout.itemAt(0).widget()
        self.dialog.set_account_panel(card);self.assertIs(self.dialog.account_slot_layout.itemAt(1).widget(),card)
        self.assertIs(self.dialog.body_layout.itemAt(0).widget(),original)
        self.assertIs(self.dialog.set_account_panel(None),card);self.assertFalse(card.isVisible());card.deleteLater()
        self.assertIs(self.dialog.body_layout.itemAt(0).widget(),original)
        self.dialog.dock_position_spin.setValue(71)
        QTest.mouseClick(self.dialog.close_button,Qt.MouseButton.LeftButton,pos=QPoint(1,1));APP.processEvents()
        self.assertFalse(self.dialog.isVisible());self.assertEqual(self.store.path.read_bytes(),saved)
        self.dialog.show();APP.processEvents();self.assertEqual(self.dialog.dock_position_spin.value(),self.store.dock_position)

    def test_real_client_empty_endpoint_reports_error_without_window_or_network(self):
        class NoNetwork:
            def get(self,*args):raise AssertionError('Unexpected network request')
        real=UpdateClient('1.7.0',self.base/'unused',endpoint='',network=NoNetwork())
        controller=subject.UpdateController('1.7.0',self.base/'unused',client=real,quit_callback=lambda:None)
        try:
            before={id(w) for w in APP.topLevelWidgets()}
            controller.open();self.assertEqual(controller.state,'error')
            self.assertEqual(before,{id(w) for w in APP.topLevelWidgets()})
        finally:controller.close()


if __name__=='__main__':
    unittest.main(verbosity=2)
