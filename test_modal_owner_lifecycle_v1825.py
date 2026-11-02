"""Owned modal lifetime and permission rebind; offscreen, no native grants."""
import os,unittest
os.environ['QT_QPA_PLATFORM']='offscreen'
from types import SimpleNamespace
from unittest.mock import patch
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication,QWidget,QLabel,QDialog
from app_settings import ReferenceModalDialog
from mac_permissions_ui import MacPermissionsDialog
APP=QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
def flush():APP.processEvents()

class ModalOwnerTests(unittest.TestCase):
    def setUp(self):
        self.owner=QWidget();self.owner.show();self.other=QWidget();self.other.show();self.windows=[];flush()
    def tearDown(self):
        for d in self.windows:d.hide();d.deleteLater()
        self.owner.hide();self.other.hide();self.owner.deleteLater();self.other.deleteLater();flush()
    def dialog(self,modal=True):
        d=ReferenceModalDialog(self.owner,modal=modal);d.body_layout.addWidget(QLabel('Synthetic',d.body));self.windows.append(d);return d
    def test_owner_hide_rejects_modal_and_reopen_preserves_true_modality(self):
        d=self.dialog();done=[];d.finished.connect(done.append);d.show();flush();self.assertIs(APP.activeModalWidget(),d)
        self.owner.hide();flush();self.assertFalse(d.isVisible());self.assertIsNone(APP.activeModalWidget());self.assertEqual(done,[QDialog.Rejected])
        self.owner.show();d.show();flush();self.assertIs(APP.activeModalWidget(),d);self.assertTrue(d.isModal());d.reject();flush();self.assertIsNone(APP.activeModalWidget())
    def test_owner_hide_during_exit_discards_fade_and_no_late_reopen_close(self):
        d=self.dialog();d.show();flush()
        with patch.object(d._window_fade,'enabled',return_value=True):
            d.reject();self.assertIsNotNone(d._window_fade.pending);self.owner.hide();flush()
            self.assertFalse(d.isVisible());self.assertIsNone(APP.activeModalWidget());self.assertIsNone(d._window_fade.pending);self.assertFalse(d._window_fade.animation.isActive() if hasattr(d._window_fade.animation,'isActive') else d._window_fade.animation.state()==d._window_fade.animation.State.Running)
            self.owner.show();d.show();flush();self.assertTrue(d.isVisible());self.assertIs(APP.activeModalWidget(),d)
            d._window_fade.animation.setCurrentTime(d._window_fade.animation.duration());flush();self.assertTrue(d.isVisible())
        d.reject();flush();self.assertIsNone(APP.activeModalWidget())
    def test_hidden_owner_cannot_show_modal_but_nonmodal_behavior_is_unchanged(self):
        self.owner.hide();d=self.dialog();d.show();flush();self.assertFalse(d.isVisible());self.assertIsNone(APP.activeModalWidget())
        reader=self.dialog(modal=False);reader.show();flush();self.assertTrue(reader.isVisible());self.assertIsNone(APP.activeModalWidget())
    def test_ignored_owner_close_does_not_cancel_live_modal(self):
        class IgnoreClose(QWidget):
            def closeEvent(self,e):e.ignore()
        owner=IgnoreClose();owner.show();d=ReferenceModalDialog(owner,modal=True);self.windows.extend([d,owner]);d.show();flush()
        owner.close();flush();self.assertTrue(owner.isVisible());self.assertTrue(d.isVisible());self.assertIs(APP.activeModalWidget(),d);d.reject();flush()
    def test_permission_rebind_changes_qt_and_transient_owner_without_topmost(self):
        backend=SimpleNamespace(inspect=lambda **_:dict(clipboard='allowed',accessibility='allowed',input='allowed',network='observed',files='on_demand'))
        manager=SimpleNamespace(shelf=self.owner,settings=SimpleNamespace(theme='dark'))
        d=MacPermissionsDialog(manager,self.owner,backend=backend);self.windows.append(d);d.show();flush();self.assertTrue(d.timer.isActive())
        d.bind_owner(self.other);self.assertFalse(d.isVisible());self.assertFalse(d.timer.isActive());d.show();flush()
        self.assertIs(d.parentWidget(),self.other);self.assertIs(d.windowHandle().transientParent(),self.other.windowHandle());self.assertFalse(bool(d.windowFlags()&Qt.WindowStaysOnTopHint));self.assertFalse(d.isModal())
        d.close();flush();self.assertFalse(d.timer.isActive());self.assertIsNone(APP.activeModalWidget())

if __name__=='__main__':unittest.main(verbosity=2)
