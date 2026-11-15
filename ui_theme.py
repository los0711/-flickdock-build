"""Shared light/dark semantic colors for painted controls and Qt styles."""
from __future__ import annotations
import math
import re
import time
import weakref
from PySide6.QtCore import QEvent,QObject,QTimer,Qt
from PySide6.QtGui import QColor,QPalette
from PySide6.QtWidgets import QAbstractButton,QApplication,QToolTip
from shiboken6 import isValid

_theme = 'light'
# Legacy painter aliases resolve through the one semantic palette in
# native_ui_common.colors; keep the Qt color strings valid for QColor.
_PALETTES = ('light', 'dark')


class _TooltipCorners(QObject):
    """Keep tooltip surfaces rounded and delay button hints until deliberate hover."""
    def __init__(self,parent):
        super().__init__(parent)
        self._button=None;self._entered_at=0.;self._position=None
        self._timer=QTimer(self);self._timer.setSingleShot(True)
        self._timer.setTimerType(Qt.PreciseTimer);self._timer.timeout.connect(self._show_button_tip)

    def _current_button(self):
        button=self._button() if self._button is not None else None
        return button if button is not None and isValid(button) else None

    def _show_button_tip(self):
        button=self._current_button()
        if button is not None and button.isVisible() and button.underMouse() and button.toolTip():
            QToolTip.showText(self._position,button.toolTip(),button)

    def eventFilter(self,watched,event):
        if getattr(self,'_updating',False):return False
        if isinstance(watched,QAbstractButton):
            if event.type()==QEvent.Enter:
                self._timer.stop();self._button=weakref.ref(watched);self._entered_at=time.monotonic()
            elif event.type() in (QEvent.Leave,QEvent.Hide,QEvent.MouseButtonPress):
                if watched is self._current_button():self._timer.stop()
            elif event.type()==QEvent.ToolTip and watched.toolTip():
                if watched is not self._current_button():
                    self._button=weakref.ref(watched);self._entered_at=time.monotonic()
                self._position=event.globalPos()
                remaining=max(0.,1.-(time.monotonic()-self._entered_at))
                self._timer.start(math.ceil(remaining*1000))
                event.accept();return True
        if event.type() in (QEvent.Polish,QEvent.Show,QEvent.Resize) and watched.metaObject().className()=='QTipLabel':
            self._updating=True
            try:
                if not watched.testAttribute(Qt.WA_TranslucentBackground):
                    watched.setAttribute(Qt.WA_TranslucentBackground,True)
                if not watched.mask().isEmpty():watched.clearMask()
                # Set geometry-affecting properties before the tooltip appears;
                # resizing at Show made the popup visibly jump.
                if event.type() in (QEvent.Polish,QEvent.Show):
                    if watched.margin()!=0:watched.setMargin(0);watched.adjustSize()
                    if not watched.windowFlags() & Qt.NoDropShadowWindowHint:
                        watched.setWindowFlag(Qt.NoDropShadowWindowHint,True)
            finally:self._updating=False
        return False

def set_theme(theme):
    global _theme
    _theme = theme if theme in _PALETTES else 'light'

def current_theme():
    return _theme

def color(key):
    from native_ui_common import colors
    palette = colors(_theme)
    role = {'canvas':'background','border':'qt_border','button':'control',
            'tab':'card','accent':'action'}.get(key,key)
    return palette.get(role, palette['text'])

_DARK = {
    '#fff':'#111111', '#ffffff':'#111111', '#fefeff':'#272727',
    '#f8f9fb':'#272727', '#f5f7fa':'#111111', '#f5f7fb':'#111111',
    '#eef1f6':'#272727', '#e7ebf1':'#272727', '#e6eaf0':'#3a3a3c',
    '#e1e5ec':'#3a3a3c', '#e5e8ed':'#3a3a3c', '#e1e7ef':'#303034',
    '#dce3ed':'#3a3a3c', '#e5e5e9':'#3a3a3c', '#ededf0':'#3a3a3c',
    '#f4f8fd':'#1e2b39', '#bfdbfa':'#567daf', '#edf6ff':'#1e354b',
    '#75b8ff':'#0a84ff', '#d9dfe8':'#454549',
    '#e9f3ff':'#1c3854', '#d9eaff':'#1e4264', '#c3ccd9':'#606066',
    '#c2cbd8':'#606066', '#72aff3':'#0a84ff',
    '#1d1d1f':'#f5f5f7', '#25272c':'#f5f5f7', '#28313d':'#f5f5f7',
    '#303a48':'#f5f5f7', '#354252':'#f5f5f7', '#282d35':'#f5f5f7',
    '#313741':'#f5f5f7', '#213247':'#f5f5f7', '#8a95a4':'#a1a1aa',
    '#7b838f':'#a1a1aa', '#626c79':'#a1a1aa', '#738091':'#a1a1aa',
    '#7d8796':'#a1a1aa', '#7c8796':'#a1a1aa', '#8490a0':'#a1a1aa',
    '#1876db':'#0a84ff', '#176bc2':'#0a84ff', '#3989ef':'#0a84ff'
}
_DARK.update({'#fff':'#0a0a0a','#ffffff':'#0a0a0a','#fefeff':'#171717',
              '#f8f9fb':'#171717','#f5f7fa':'#0a0a0a','#f5f7fb':'#0a0a0a',
              '#1d1d1f':'#fafafa','#282d35':'#fafafa','#313741':'#fafafa',
              '#7c8796':'#a3a3a3','#8490a0':'#a3a3a3','#a3a3a3':'#737373'})

def qss(source, font_family):
    from native_ui_common import readable_qss,font_css_stack
    from PySide6.QtWidgets import QApplication
    app=QApplication.instance()
    stack=font_css_stack() if app is not None and getattr(app,'_flickdock_fonts_loaded',False) else f"'{font_family}'"
    result = source.replace("'PingFang SC'", stack).replace("'FlickDock UI'", stack)
    if _theme == 'dark':
        result = re.sub(r'#[0-9a-fA-F]{3,8}\b', lambda m: _DARK.get(m.group(0).lower(), m.group(0)), result)
        result = re.sub(r'(?<=background:)white\b', color('surface'), result)
    return readable_qss(result)

def style_widget(widget, source, font_family):
    widget._flickdock_style = source
    widget.setStyleSheet(qss(source, font_family))

def apply_application(app, font_family):
    from native_ui_common import colors,font_css_stack,RADIUS_SUBCONTROL_PX,UiButton
    palette = QPalette()
    roles = {'Window':'surface','Base':'surface','AlternateBase':'canvas',
             'WindowText':'text','Text':'text','ButtonText':'text','Button':'button',
             'ToolTipBase':'surface','ToolTipText':'text','Highlight':'selected','HighlightedText':'text',
             'PlaceholderText':'secondary'}
    for role, key in roles.items():
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(color(key)))
    app.setPalette(palette)
    # One application-level tooltip style also covers icon buttons created by
    # individual dialogs and QToolTip.showText().  Keep any existing app QSS.
    c=colors(_theme)
    if not hasattr(app,'_flickdock_tooltip_base'):app._flickdock_tooltip_base=app.styleSheet()
    tooltip=(f"QToolTip{{background:{c['card']};color:{c['text']};border:1px solid {c['input_border']};"
             f"border-radius:{RADIUS_SUBCONTROL_PX}px;padding:4px 8px;font:400 12px {font_css_stack()};}}")
    stylesheet=(app._flickdock_tooltip_base+'\n'+tooltip).strip()
    if app.styleSheet()!=stylesheet:app.setStyleSheet(stylesheet)
    if not getattr(app,'_flickdock_tooltip_corners',None):
        QApplication.setEffectEnabled(Qt.UI_AnimateTooltip,False)
        QApplication.setEffectEnabled(Qt.UI_FadeTooltip,False)
        app._flickdock_tooltip_corners=_TooltipCorners(app);app.installEventFilter(app._flickdock_tooltip_corners)
    for widget in app.allWidgets():
        if isinstance(widget,UiButton):widget.refresh_style()
        source = getattr(widget, '_flickdock_style', None)
        if source is not None:
            widget.setStyleSheet(qss(source, font_family))
        widget.update()
