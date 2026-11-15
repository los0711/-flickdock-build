"""Shared shelf geometry, fonts, menus and motion preference."""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve
from PySide6.QtGui import QColor
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QMenu
import ctypes
import sys
import ui_theme


IDLE_SECONDS = 3.0


FILE_DETAIL_FONT_SIZE_PX = 10


FILE_ROW_HEIGHT_PX=64


OVERVIEW_FULL_TOOLBAR_MIN_WIDTH_PX=484


OVERVIEW_SECTION_OFFSETS_PX = (('header',(0,-8)),('sidebar',(-18,0)),('middle',(0,14)),('preview',(18,0)))


OVERVIEW_TRANSITION_MAX_ITEMS = 24


SHAKE_DISMISS_SECONDS = 3.0


FONT_FAMILY = "sans-serif"


CARD_W, CARD_H = 332, 318


EXPANDED_W, EXPANDED_H = 1040, 600


MODES = ("permanent",)


def load_font(app):
    global FONT_FAMILY
    from native_ui_common import configure_reference_font
    FONT_FAMILY=configure_reference_font(app).family()


def motion_enabled() -> bool:
    if sys.platform != "win32":
        from native_ui_common import reduced_motion
        return not reduced_motion()
    value = ctypes.c_int(1)
    try:
        ok = ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(value), 0)
        return bool(value.value) if ok else True
    except OSError:
        return True


class AnimatedMenu(QMenu):
    def __init__(self, parent=None):
        super().__init__(parent)
        from display_frames import DisplayAnimation
        self.fade = DisplayAnimation(self)
        self.fade.valueChanged.connect(self.setWindowOpacity)
        self.fade.setDuration(120)
        self.fade.setEasingCurve(QEasingCurve.Type.OutCubic)

    def showEvent(self, event):
        self.fade.stop()
        from native_ui_common import reduced_motion
        if reduced_motion():
            self.setWindowOpacity(1);super().showEvent(event);return
        self.setWindowOpacity(.25)
        super().showEvent(event)
        self.fade.setStartValue(.25)
        self.fade.setEndValue(1.)
        self.fade.start()

    def hideEvent(self,event):
        self.fade.stop();self.setWindowOpacity(1);super().hideEvent(event)


def style_menu(menu: QMenu) -> None:
    from native_ui_common import rounded_popup,RADIUS_CARD_PX,RADIUS_SUBCONTROL_PX
    # A binary QRegion mask makes small rounded corners visibly stair-stepped.
    # Let Qt composite the translucent frameless popup's antialiased edge.
    rounded_popup(menu, RADIUS_CARD_PX, antialiased=True)
    palette = menu.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor(ui_theme.color("surface")))
    palette.setColor(QPalette.ColorRole.Base, QColor(ui_theme.color("surface")))
    palette.setColor(QPalette.ColorRole.Text, QColor(ui_theme.color("text")))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(ui_theme.color("text")))
    menu.setPalette(palette)
    from reference_widgets import css,colors
    sources=[]
    for theme in ('dark','light'):
        c=colors(theme)
        sources.append(f"QMenu{{background:{c['card']};color:{c['text']};border:1px solid {c['border']};border-radius:{RADIUS_CARD_PX}px;padding:6px 0;font:12px 'FlickDock UI';}}"
                       f"QMenu::item{{background:transparent;color:{c['text']};padding:7px 12px;margin:1px 6px;min-width:178px;border:0;border-radius:{RADIUS_SUBCONTROL_PX}px;}}"
                       f"QMenu::item:selected{{background:{c['hover']};color:{c['text']};}}"
                       f"QMenu::item:disabled{{color:{c['muted']};}} QMenu::separator{{height:1px;background:{c['divider']};margin:5px 10px;}}")
    css(menu,*sources)


def ui_style(text):
    return text


def set_ui_style(widget, source):
    ui_theme.style_widget(widget, source, FONT_FAMILY)
