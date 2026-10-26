"""Settings startup reconciliation against injected backends only."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
import app_settings as subject
from platform_startup import CallbackBackend, LaunchAgentBackend, StartupManager

APP=QApplication.instance() or QApplication([])


class StartupSettings(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='flickdock-startup-settings-')
        self.base=Path(self.temporary.name)
        self.env=patch.dict(os.environ,APPDATA=str(self.base/'isolated'));self.env.start()
        self.installed=self.base/'installed'/'FlickDock.exe'
        self.installed.parent.mkdir();self.installed.write_bytes(b'MZ-fixture')
        self.store=subject.SettingsStore(self.base/'profile')
        self.store.update(dock_length=90)
        self.value=None;self.approval=None;self.writes=[]

    def tearDown(self):
        self.env.stop();self.temporary.cleanup()

    def read(self):return self.value
    def write(self,value):self.value=value;self.writes.append(('run',value))
    def read_approval(self):return self.approval
    def write_approval(self,value):self.approval=value;self.writes.append(('approval',value))

    def manager(self,**kwargs):
        return StartupManager(self.installed,platform='win32',frozen=True,runtime_allowed=True,
            backend=CallbackBackend(self.read,self.write,self.read_approval,self.write_approval),**kwargs)

    def test_saved_true_initialized_missing_repairs_and_same_value_save_repairs(self):
        self.store._data['startup_initialized']=True
        with patch.object(subject.sys,'frozen',True,create=True), \
             patch.object(subject.sys,'executable',str(self.installed)), \
             patch.object(subject,'_startup_runtime_allowed',return_value=True), \
             patch.object(subject,'_read_startup_value',side_effect=self.read), \
             patch.object(subject,'_write_startup_value',side_effect=self.write):
            self.assertTrue(self.store.reconcile_startup())
            self.assertEqual(self.value,(f'"{self.installed}"',1))
            self.assertTrue(self.store.startup_status().enabled)
            self.value=None
            self.assertTrue(self.store.update(autostart=True))
            self.assertTrue(self.store.startup_status().enabled)
            self.assertEqual(json.loads(self.store.path.read_text())['autostart'],True)
            self.store.update(autostart=False)
            self.assertIsNone(self.value)
            self.writes.clear()
            self.assertFalse(self.store.reconcile_startup());self.assertEqual(self.writes,[])

    def test_os_block_is_respected_on_launch_and_explicit_save_restores(self):
        self.approval=(b'\x03'+b'\0'*11,3)
        with patch.object(subject,'_startup_runtime_allowed',return_value=True), \
             patch.object(subject,'_startup_manager',side_effect=self.manager):
            self.assertFalse(self.store.reconcile_startup())
            self.assertEqual(self.writes,[])
            self.assertTrue(self.store.update(autostart=True))
            self.assertIsNone(self.approval)
            self.assertTrue(self.store.startup_status().enabled)

    def test_no_op_backend_does_not_report_success_or_close_form(self):
        manager=StartupManager(self.installed,platform='win32',frozen=True,runtime_allowed=True,
            backend=CallbackBackend(lambda:None,lambda value:None))
        before=self.store.path.read_bytes()
        with patch.object(subject,'_startup_runtime_allowed',return_value=True), \
             patch.object(subject,'_startup_manager',return_value=manager):
            dialog=subject.SettingsDialog(self.store);dialog.show();APP.processEvents()
            dialog.save();APP.processEvents()
            self.assertTrue(dialog.isVisible());self.assertTrue(dialog.error_label.isVisible())
            self.assertEqual(self.store.path.read_bytes(),before)
            self.assertFalse(self.store.startup_status().enabled)
            dialog.reject();dialog.deleteLater();APP.processEvents()

    def test_json_failure_rolls_back_run_and_approval(self):
        self.approval=(b'\x03'+b'\0'*11,3);original=self.approval
        before=self.store.path.read_bytes()
        with patch.object(subject,'_startup_runtime_allowed',return_value=True), \
             patch.object(subject,'_startup_manager',side_effect=self.manager), \
             patch.object(subject.os,'replace',side_effect=OSError('disk failure')):
            with self.assertRaisesRegex(OSError,'disk failure'):
                self.store.update(autostart=True)
        self.assertIsNone(self.value);self.assertEqual(self.approval,original)
        self.assertEqual(self.store.path.read_bytes(),before)
        self.assertFalse(list(self.store.base.glob('.settings-*.tmp')))

    def test_foreign_value_never_overwritten(self):
        self.value=(str(self.base/'Other.exe'),1);before=self.value
        with patch.object(subject,'_startup_runtime_allowed',return_value=True), \
             patch.object(subject,'_startup_manager',side_effect=self.manager):
            with self.assertRaises(OSError):self.store.update(autostart=True)
        self.assertEqual(self.value,before);self.assertEqual(self.writes,[])

    def test_macos_same_settings_store_uses_temporary_launch_agent(self):
        installed=self.base/'FlickDock.app'/'Contents'/'MacOS'/'FlickDock'
        installed.parent.mkdir(parents=True);installed.write_bytes(b'fixture')
        backend=LaunchAgentBackend(self.base/'mock-home')
        manager=StartupManager(installed,platform='darwin',frozen=True,runtime_allowed=True,backend=backend)
        with patch.object(subject,'_startup_runtime_allowed',return_value=True), \
             patch.object(subject,'_startup_manager',return_value=manager):
            self.assertTrue(self.store.update(autostart=True))
            self.assertTrue(backend.path.is_file());self.assertTrue(self.store.startup_status().enabled)
            self.store.update(autostart=False)
            self.assertFalse(backend.path.exists())

    def test_source_offscreen_load_and_save_never_touch_startup_callbacks(self):
        with patch.object(subject,'_read_startup_value') as read,patch.object(subject,'_write_startup_value') as write:
            for frozen in (False,True):
                with patch.object(subject.sys,'frozen',frozen,create=True):
                    self.store.reconcile_startup();self.store.update(autostart=False);self.store.update(autostart=True)
                    self.assertFalse(self.store.startup_status().supported)
            read.assert_not_called();write.assert_not_called()


if __name__=='__main__':unittest.main(verbosity=2)
