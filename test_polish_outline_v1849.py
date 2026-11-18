"""Check the single breathing stroke on all four edges and its lifecycle."""
import unittest
from unittest.mock import patch

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget

from native_ui_common import UiButton, colors, qss_color
from reference_widgets import button, refresh_tree
from test_translator_v1843 import APP
from translator_widgets import PolishPane
import theme_accent
import ui_theme


class PolishOutlineTests(unittest.TestCase):
    def test_all_edges_breathe_together_in_both_themes(self):
        self.addCleanup(theme_accent.set_color,theme_accent.current())
        self.addCleanup(ui_theme.set_theme,ui_theme.current_theme())
        theme_accent.set_color('#4466DD')
        host=QWidget();host.resize(280,180)
        self.addCleanup(host.deleteLater)
        pane=PolishPane(host);pane.setGeometry(20,20,240,140)
        host.show();APP.processEvents()

        def frame(elapsed):
            with patch('translator_widgets.time.monotonic',return_value=pane._started_at+elapsed):
                grabbed=pane.grab().toImage()
                width,height=grabbed.width(),grabbed.height()
                edges=(grabbed.pixelColor(width//2,0),grabbed.pixelColor(width//2,height-1),
                       grabbed.pixelColor(0,height//2),grabbed.pixelColor(width-1,height//2))
                return tuple(color.name() for color in edges),grabbed.pixelColor(width//2,height//2).name()

        with patch('translator_widgets.reduced_motion',return_value=False):
            for theme in ('light','dark'):
                ui_theme.set_theme(theme);pane.set_polish_active(True)
                self.assertTrue(pane._frames.isActive())
                length=pane.BREATH_SECONDS
                resting,center=frame(0)
                mid,_=frame(length/4)
                peak,peak_center=frame(length/2)
                self.assertEqual(peak,('#4466dd',)*4)
                self.assertEqual(len(set(mid)),1)
                self.assertNotIn(mid[0],(peak[0],resting[0]))
                self.assertEqual(frame(length)[0],resting)
                self.assertEqual(peak_center,center)
                pane.set_polish_active(False)
                self.assertFalse(pane._frames.isActive())
                self.assertEqual(qss_color(pane.surface_style()[1]),qss_color(colors(theme)['border']))
            pane.set_polish_active(True);host.hide()
            self.assertFalse(pane._frames.isActive())
        with patch('translator_widgets.reduced_motion',return_value=True):
            host.show();pane.set_polish_active(True)
            self.assertFalse(pane._frames.isActive())
            self.assertNotEqual(pane.surface_style()[1],QColor('#4466DD'))


class AccentInkTests(unittest.TestCase):
    def test_primary_button_ink_tracks_accent_even_when_checked_and_pressed(self):
        from accent_presets import PRESETS
        self.addCleanup(theme_accent.set_color,theme_accent.current())
        self.addCleanup(ui_theme.set_theme,ui_theme.current_theme())
        plain=button('MMMMM',variant='primary')
        illustrated=button('MMMMM','check',variant='primary')
        styled=UiButton('MMMMM',variant='primary')
        controls=(plain,illustrated,styled)
        for control in controls:
            self.addCleanup(control.deleteLater)
            control.setCheckable(True);control.setChecked(True);control.resize(130,32);control.show()
        APP.processEvents()
        for theme in ('light','dark'):
            ui_theme.set_theme(theme)
            styled.setProperty('uiTheme',theme)
            for value in [hex_value for _,hex_value in PRESETS]+['#FFFFFF','#777777']:
                theme_accent.set_color(value)
                palette=colors(theme)
                foreground=QColor(palette['on_action']).rgb()
                for control in controls:refresh_tree(control)
                styled.refresh_style()
                for state in ('action','action_hover','action_pressed'):
                    fill=QColor(palette[state]).rgb()
                    for control in controls:
                        control.setDown(state=='action_pressed')
                        with patch.object(control,'underMouse',return_value=state=='action_hover'):
                            image=control.grab().toImage()
                        dpr=image.devicePixelRatio();x=round(14*dpr);y=image.height()//2
                        if control is not styled or state!='action_hover':
                            self.assertEqual(image.pixel(x,y),fill,(theme,value,state,control))
                        self.assertTrue(any(image.pixel(i,j)==foreground
                                            for i in range(round(42*dpr),round(96*dpr))
                                            for j in range(y-7,y+7)),(theme,value,state,control))
