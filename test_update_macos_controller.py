"""Real Mac staging behind the headless controller; fake Popen only."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

import copy
import hashlib
from pathlib import Path
import plistlib
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import zipfile

from PySide6.QtCore import QEventLoop, QObject, QTimer, Signal, QThreadPool
from PySide6.QtWidgets import QApplication
import mac_updates
from update_controller import UpdateController

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


def spin(predicate, timeout=3):
    deadline = time.perf_counter() + timeout
    while not predicate() and time.perf_counter() < deadline:
        loop = QEventLoop()
        QTimer.singleShot(5, loop.quit)
        loop.exec()
    return predicate()


class Client(QObject):
    checking=Signal(bool); update_available=Signal(dict,bool); up_to_date=Signal(bool)
    failed=Signal(str,bool); progress=Signal(int,int); verified_file=Signal(str,dict,bool); cancelled=Signal(bool)
    allowed_hosts={'gitee.com'}
    def __init__(self): super().__init__(); self.busy=False; self.calls=[]
    def check(self, manual=False): self.calls.append('check'); self.busy=True; self.checking.emit(manual); return True
    def download(self, manifest, manual=True): self.calls.append('download'); self.busy=True; self.progress.emit(0, manifest['installer']['size']); return True
    def offer(self, manifest, manual=False): self.busy=False; self.update_available.emit(copy.deepcopy(manifest),manual)
    def complete(self, path, manifest): self.busy=False; self.verified_file.emit(str(path),copy.deepcopy(manifest),True)
    def cancel(self): self.busy=False; self.cancelled.emit(True)
    def close(self): self.busy=False


class MacControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='flickdock-mac-controller-')
        self.base = Path(self.temp.name).resolve()
        self.profile = self.base / 'profile'
        self.bundle = self.base / 'Applications/FlickDock.app'
        self.exe = self.bundle / 'Contents/MacOS/FlickDock'
        self.exe.parent.mkdir(parents=True)
        info = dict(CFBundleIdentifier=mac_updates.BUNDLE_ID, CFBundlePackageType='APPL',
                    CFBundleExecutable='FlickDock', CFBundleShortVersionString='1.7.0')
        (self.bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
        self.exe.write_bytes(b'\xcf\xfa\xed\xfeOLD')
        self.zip = self.profile / 'updates/1.7.1/update.zip'
        self.zip.parent.mkdir(parents=True)
        info['CFBundleShortVersionString'] = '1.7.1'
        with zipfile.ZipFile(self.zip, 'w') as archive:
            for name, value, mode in [('FlickDock.app/Contents/Info.plist', plistlib.dumps(info), 0o644),
                    ('FlickDock.app/Contents/MacOS/FlickDock', b'\xcf\xfa\xed\xfeNEW', 0o755)]:
                member = zipfile.ZipInfo(name)
                member.external_attr = (stat.S_IFREG | mode) << 16
                archive.writestr(member, value)
        self.manifest = dict(schema_version=1, version='1.7.1', published_at='2026-10-26T00:00:00Z', notes='fixture',
            platform='darwin', architecture='arm64', installer=dict(url='https://gitee.com/releases/mac.zip',
                filename='FlickDock-macOS-arm64.zip', size=self.zip.stat().st_size, sha256=hashlib.sha256(self.zip.read_bytes()).hexdigest()))
        self.client, self.events = Client(), []
        self.controller = UpdateController('1.7.0', self.profile, client=self.client, platform='darwin', machine='arm64',
             installed_exe=self.exe, popen=self.popen, quit_callback=lambda:self.events.append(('quit',)), defer_ms=20)

    def tearDown(self):
        self.controller.close()
        self.assertTrue(spin(lambda:not self.controller._jobs))
        QThreadPool.globalInstance().waitForDone(2000)
        APP.processEvents()
        self.temp.cleanup()

    def popen(self, arguments, **options):
        self.events.append(('popen', arguments, options))
        receipt=Path(arguments[2]);prepared=mac_updates._read_receipt(receipt)
        mac_updates._ack_helper_startup(prepared,receipt,pid=123)
        return SimpleNamespace(pid=123,poll=lambda:None,terminate=lambda:None)
    def complete_and_install(self,path,manifest):
        self.client.complete(path,manifest)
        if self.controller.state=='ready':
            self.assertFalse(self.controller._jobs)
            self.controller.activate()

    def authorize(self):
        self.client.offer(self.manifest)
        self.assertTrue(self.controller.start_update())
        self.client.offer(self.manifest)

    def test_auto_result_does_not_launch_and_explicit_update_uses_mac_helper(self):
        self.controller.check_automatically()
        self.client.offer(self.manifest)
        self.complete_and_install(self.zip, self.manifest)
        self.assertEqual(self.events, [])
        self.assertEqual(self.controller.state, 'available')
        self.assertTrue(self.controller.start_update())
        self.client.offer(self.manifest)
        self.complete_and_install(self.zip, self.manifest)
        self.assertTrue(spin(lambda:len(self.events)==2))
        self.assertEqual([value[0] for value in self.events], ['popen','quit'])
        self.assertEqual(self.events[0][1][1], '--apply-mac-update')
        self.assertIn('updater-runtime',self.events[0][1][0])
        self.assertFalse(self.events[0][2]['shell'])
        self.assertEqual(mac_updates.bundle_metadata(self.bundle)['CFBundleShortVersionString'], '1.7.0')
        self.assertFalse(hasattr(self.controller, 'dialog'))

    def test_cancel_download_ignores_late_callback_until_another_click(self):
        self.authorize()
        self.controller.cancel()
        self.complete_and_install(self.zip,self.manifest)
        APP.processEvents()
        self.assertEqual(self.events,[])
        self.assertEqual(self.controller.state,'cancelled')
        self.controller.start_update()
        self.client.offer(self.manifest)
        self.complete_and_install(self.zip,self.manifest)
        self.assertTrue(spin(lambda:len(self.events)==2))

    def test_cancel_preparation_and_close_reject_late_success_without_ui_block(self):
        self.authorize()
        entered, release = threading.Event(), threading.Event()
        real = mac_updates.prepare_update
        def delayed(*args, **kwargs):
            entered.set(); release.wait(2)
            kwargs.pop('cancel_event',None)
            return real(*args,**kwargs)
        beats=[]; timer=QTimer();timer.setInterval(20);timer.timeout.connect(lambda:beats.append(time.perf_counter()));timer.start()
        with patch.object(mac_updates,'prepare_update',delayed):
            self.complete_and_install(self.zip,self.manifest)
            self.assertTrue(spin(entered.is_set))
            self.assertTrue(spin(lambda:len(beats)>=4))
            started=time.perf_counter();self.controller.close()
            self.assertLess(time.perf_counter()-started,.05)
            release.set();self.assertTrue(spin(lambda:not self.controller._jobs))
        timer.stop()
        self.assertEqual(self.events,[])
        self.assertFalse(list(self.bundle.parent.glob('.flickdock-update-*')))
        self.assertLess(max(b-a for a,b in zip(beats,beats[1:])),.1)

    def test_cancel_at_installing_state_revokes_helper_and_can_retry(self):
        self.authorize()
        def cancel(state,payload):
            if state=='installing':self.controller.cancel()
        self.controller.state_changed.connect(cancel)
        self.complete_and_install(self.zip,self.manifest)
        self.assertTrue(spin(lambda:not self.controller._jobs))
        self.assertEqual(self.events,[])
        self.assertEqual(self.controller.state,'cancelled')
        self.controller.state_changed.disconnect(cancel)
        self.assertTrue(self.controller.start_update())
        self.client.offer(self.manifest)
        self.complete_and_install(self.zip,self.manifest)
        self.assertTrue(spin(lambda:len(self.events)==2))
        self.assertEqual(self.client.calls.count('download'),2)

    def test_outside_zip_and_wrong_architecture_never_spawn(self):
        self.authorize()
        outside=self.base/'outside.zip';outside.write_bytes(self.zip.read_bytes())
        self.complete_and_install(outside,self.manifest)
        self.assertEqual(self.controller.state,'error')
        self.assertEqual(self.events,[])
        wrong=copy.deepcopy(self.manifest);wrong['architecture']='x86_64'
        self.client.offer(wrong)
        self.controller.start_update();self.client.offer(wrong);self.complete_and_install(self.zip,wrong)
        self.assertEqual(self.controller.state,'error')
        self.assertEqual(self.events,[])

    def test_failed_helper_spawn_does_not_quit_and_explicit_retry_works(self):
        def denied(*args,**kwargs):raise OSError('helper spawn denied')
        self.controller._popen=denied
        self.authorize();self.complete_and_install(self.zip,self.manifest)
        self.assertTrue(spin(lambda:self.controller.state=='error'))
        self.assertEqual(self.events,[])
        self.assertIn('helper spawn denied',self.controller.state_payload['error'])
        self.controller._popen=self.popen
        self.assertTrue(self.controller.start_update())
        self.client.offer(self.manifest)
        self.complete_and_install(self.zip,self.manifest)
        self.assertTrue(spin(lambda:len(self.events)==2))

    def test_spawned_but_exited_helper_reports_error_and_allows_retry_without_quitting(self):
        self.controller._popen=lambda *args,**kwargs:SimpleNamespace(pid=123,poll=lambda:1,terminate=lambda:None)
        failures=[];self.controller.installation_failed.connect(failures.append)
        self.authorize();self.complete_and_install(self.zip,self.manifest)
        self.assertTrue(spin(lambda:self.controller.state=='error'))
        self.assertTrue(failures);self.assertIn('退出码 1',failures[0]);self.assertEqual(self.events,[])
        self.assertEqual(mac_updates.bundle_metadata(self.bundle)['CFBundleShortVersionString'],'1.7.0')
        self.controller._popen=self.popen;self.assertTrue(self.controller.start_update())
        self.client.offer(self.manifest);self.complete_and_install(self.zip,self.manifest)
        self.assertTrue(spin(lambda:len(self.events)==2))

    def test_cancel_cannot_start_another_helper_until_owned_process_is_reaped(self):
        import subprocess
        entered,release=threading.Event(),threading.Event();alive=[True]
        def wait(timeout):
            entered.set();release.wait(2);alive[0]=False;return 0
        child=SimpleNamespace(pid=123,poll=lambda:None if alive[0] else 0,
            terminate=lambda:None,kill=lambda:None,wait=wait)
        self.controller._popen=lambda *args,**kwargs:child
        self.authorize();self.complete_and_install(self.zip,self.manifest)
        self.assertTrue(spin(lambda:hasattr(self.controller,'_mac_helper_startup')))
        try:
            self.controller.cancel();self.assertTrue(spin(entered.is_set))
            self.assertFalse(self.controller.can_start_update)
            self.assertFalse(self.controller.start_update());self.assertEqual(self.events,[])
        finally:release.set()
        self.assertTrue(spin(lambda:not self.controller.busy))
        self.assertTrue(self.controller.can_start_update)

    def test_cancel_keeps_retry_locked_when_child_exit_cannot_be_confirmed(self):
        import subprocess
        def wait(timeout):raise subprocess.TimeoutExpired('owned fixture',timeout)
        child=SimpleNamespace(pid=123,poll=lambda:None,terminate=lambda:None,kill=lambda:None,wait=wait)
        self.controller._popen=lambda *args,**kwargs:child
        self.authorize();self.complete_and_install(self.zip,self.manifest)
        self.assertTrue(spin(lambda:hasattr(self.controller,'_mac_helper_startup')))
        self.controller.cancel();monitor=child._flickdock_startup_monitor
        self.assertTrue(spin(lambda:self.controller.state=='error'))
        self.assertTrue(self.controller.busy);self.assertFalse(self.controller.can_start_update)
        self.assertTrue(monitor.prepared.stage.exists());self.assertEqual(self.events,[])


if __name__=='__main__':unittest.main(verbosity=2)
