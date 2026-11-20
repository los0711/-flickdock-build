"""Shared shelf geometry, fonts, menus and motion preference."""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve,Qt,QRectF
from PySide6.QtGui import QColor,QPainter
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QMenu
import ctypes
import sys
import ui_theme


IDLE_SECONDS = 3.0


FILE_DETAIL_FONT_SIZE_PX = 10


FILE_ROW_HEIGHT_PX=64


OVERVIEW_FULL_TOOLBAR_MIN_WIDTH_PX=484




OVERVIEW_TRANSITION_MAX_ITEMS = 24


SHAKE_DISMISS_SECONDS = 3.0


FONT_FAMILY = "sans-serif"


CARD_W, CARD_H = 332, 318


EXPANDED_W, EXPANDED_H = 1124, 624


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


class RoundedMenu(QMenu):
    """Retain native menu actions and hit testing on a translucent AA host."""
    def __init__(self,parent=None):
        super().__init__(parent)
        self.surface_theme=None
        self.selection_inset=(6,1)
        from dropdown_motion import StretchHighlight
        self._highlight=StretchHighlight(self)
        self.hovered.connect(self._hover_action)
        self.aboutToHide.connect(self._reset_highlight)

    def _hover_action(self, action):
        if not action or action.isSeparator() or not action.isEnabled():
            self._reset_highlight()
            return
        dx,dy=self.selection_inset
        self._highlight.move(QRectF(self.actionGeometry(action)).adjusted(dx,dy,-dx,-dy))

    def _reset_highlight(self):
        self._highlight.move(QRectF())

    def paintEvent(self,event):
        from native_ui_common import colors,paint_rounded_surface,qss_color,RADIUS_CARD_PX,RADIUS_SUBCONTROL_PX
        c=colors(self.surface_theme or ui_theme.current_theme())
        painter=QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        painter.fillRect(self.rect(),Qt.transparent)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        paint_rounded_surface(painter,self.rect(),RADIUS_CARD_PX,qss_color(c['popup_surface']),qss_color(c['border']))
        active=self.activeAction()
        highlighted=self._highlight.paint(painter,qss_color(c['hover']),RADIUS_SUBCONTROL_PX)
        if not highlighted and active is not None and active.isEnabled() and not active.isSeparator():
            dx,dy=self.selection_inset
            rect=QRectF(self.actionGeometry(active)).adjusted(dx,dy,-dx,-dy)
            paint_rounded_surface(painter,rect,RADIUS_SUBCONTROL_PX,qss_color(c['hover']))
        painter.end();super().paintEvent(event)


class AnimatedMenu(RoundedMenu):
    def __init__(self, parent=None):
        super().__init__(parent)
        from display_frames import DisplayAnimation
        self.fade = DisplayAnimation(self)
        self.fade.valueChanged.connect(self.setWindowOpacity)
        self.fade.setDuration(120)
        self.fade.setEasingCurve(QEasingCurve.Type.OutCubic)

    def showEvent(self, event):
        if hasattr(self,'_dropdown_motion'):
            super().showEvent(event)
            return
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
        self.fade.stop()
        if not hasattr(self,'_dropdown_motion'):self.setWindowOpacity(1)
        super().hideEvent(event)


def style_menu(menu: RoundedMenu) -> None:
    from native_ui_common import rounded_popup
    rounded_popup(menu)
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
        sources.append(f"QMenu{{background:transparent;color:{c['text']};border:0;padding:7px 1px;font:12px 'FlickDock UI';}}"
                       f"QMenu::item{{background:transparent;color:{c['text']};padding:7px 12px;margin:1px 6px;min-width:178px;border:0;}}"
                       f"QMenu::item:selected{{background:transparent;color:{c['text']};}}"
                       f"QMenu::item:disabled{{color:{c['muted']};}} QMenu::separator{{height:1px;background:{c['divider']};margin:5px 10px;}}")
    css(menu,*sources)


def ui_style(text):
    return text


def set_ui_style(widget, source):
    ui_theme.style_widget(widget, source, FONT_FAMILY)
