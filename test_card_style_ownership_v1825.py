"""The shelf card has one authoritative per-platform theme definition."""
from types import SimpleNamespace
import unittest
from PySide6.QtCore import QPoint,QRect
import test_html_native_workspace as ws
import ui_theme


class CardStyleOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.fixture=ws.WorkspaceTests('runTest');self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.shelf=self.fixture.shelf;self.manager=self.fixture.manager
        self.shelf.files['permanent']=[];self.shelf.refresh();ws.flush()
        self.manager._applied_settings=dict(self.fixture.settings._data)
        self.manager.updates=SimpleNamespace(apply_theme=lambda _:None)
        for name in ('settings_dialog','lan_dialog','wan_dialog','update_notice','update_dialog'):setattr(self.manager,name,None)

    def apply_theme(self,theme):
        self.fixture.settings.update(theme=theme)
        ws.subject.ShelfManager.apply_settings(self.manager);ws.flush()

    def test_same_theme_refresh_keeps_complete_pixels_and_current_style_in_both_modes(self):
        for theme in ('dark','light'):
            self.apply_theme(theme)
            for expanded in (False,True):
                with self.subTest(theme=theme,expanded=expanded):
                    if expanded:self.shelf.show_contents(animate=False)
                    elif self.shelf.expanded:self.shelf.collapse_contents();self.shelf.geometry_morph.complete()
                    ws.flush();style=self.shelf.card.styleSheet();before=self.shelf.grab().toImage()
                    native=QRect(self.shelf.geometry());visual=self.shelf.visual_geometry()
                    ui_theme.apply_application(ws.APP,ws.subject.FONT_FAMILY);self.shelf.refresh();ws.flush()
                    self.assertEqual(self.shelf.card.styleSheet(),style,'A retired global style overwrote the current card theme')
                    self.assertEqual(self.shelf.grab().toImage(),before,'Same-theme refresh changed the actual window pixels')
                    self.assertEqual(self.shelf.geometry(),native);self.assertEqual(self.shelf.visual_geometry(),visual)
                    self.assertFalse(hasattr(self.shelf.card,'_flickdock_style'))

    def test_real_theme_settings_route_preserves_mode_and_header_geometry(self):
        self.shelf.show_contents(animate=False);ws.flush()
        controls=(self.shelf.network_status,self.shelf.cloud_button,self.shelf.pin_button,self.shelf.expand_button,self.shelf.menu_button,self.shelf.close_button)
        header=[QRect(button.mapToGlobal(QPoint()),button.size()) for button in controls]
        native=QRect(self.shelf.geometry());visual=self.shelf.visual_geometry();popup=self.shelf.popup
        for theme in ('light','dark','light'):
            with self.subTest(theme=theme):
                self.apply_theme(theme)
                self.assertEqual(self.shelf.card.styleSheet(),self.shelf.card._reference_style[theme=='light'])
                self.assertEqual(self.shelf.geometry(),native);self.assertEqual(self.shelf.visual_geometry(),visual)
                self.assertEqual([QRect(button.mapToGlobal(QPoint()),button.size()) for button in controls],header)
                self.assertTrue(self.shelf.expanded);self.assertIs(self.shelf.popup,popup)
                self.assertTrue(popup.view_list.isChecked());self.assertFalse(popup.view_grid.isChecked())


if __name__=='__main__':unittest.main()
