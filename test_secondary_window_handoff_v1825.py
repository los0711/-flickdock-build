"""Actual nonmodal secondary windows, offscreen and without system access."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication,QWidget
from flickdock import ShelfManager
from update_dialog import UpdateDialog
from mac_permissions_ui import MacPermissionsDialog
APP=QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)

class SecondaryHandoff(unittest.TestCase):
    def setUp(self):
        self.owner=QWidget();self.owner.show();self.settings=QWidget();self.settings.show()
        self.events=[];self.windows=[]
        self.owner._release_reveal_level=lambda:self.events.append('release')
        self.owner._dismiss_clipboard_preview=lambda:self.events.append('dismiss-reader')
        self.manager=SimpleNamespace(_quitting=False,shelf=self.owner,shelves=[self.owner],
            settings=SimpleNamespace(theme='dark'),settings_dialog=self.settings,
            settings_open=lambda:True,_center_dialog=lambda _:None)
    def tearDown(self):
        for window in self.windows:window.hide();window.deleteLater()
        self.owner.hide();self.settings.hide();self.owner.deleteLater();self.settings.deleteLater();APP.processEvents()
    def test_update_releases_native_shelf_lease_before_show_without_becoming_topmost(self):
        dialog=UpdateDialog('1.8.24');self.windows.append(dialog);self.manager.update_dialog=dialog
        shown=[]
        with patch.object(dialog,'show',side_effect=lambda:shown.append(tuple(self.events))):
            ShelfManager.show_update_dialog(self.manager)
        self.assertEqual(shown,[('dismiss-reader','release')])
    def test_permission_dialog_rebinds_to_current_settings_entry(self):
        backend=SimpleNamespace(inspect=lambda **_:dict(clipboard='allowed',accessibility='allowed',
            input='allowed',network='unknown',files='on_demand'))
        dialog=MacPermissionsDialog(self.manager,self.owner,backend=backend)
        self.windows.append(dialog);self.manager.mac_permissions_dialog=dialog
        with patch('flickdock.sys.platform','darwin'),patch('flickdock.native_input_available',return_value=True):
            ShelfManager.show_mac_permissions(self.manager)
        APP.processEvents()
        self.assertIs(dialog.parentWidget(),self.settings)
        self.assertIs(dialog.windowHandle().transientParent(),self.settings.windowHandle())

if __name__=='__main__':unittest.main()
