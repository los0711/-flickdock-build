"""Shared light/dark semantic colors for painted controls and Qt styles."""
from __future__ import annotations
import math
import re
import time
import weakref
from PySide6.QtCore import QEasingCurve,QEvent,QObject,QPoint,QPropertyAnimation,QTimer,Qt
from PySide6.QtGui import QColor,QPalette
from PySide6.QtWidgets import QAbstractButton,QApplication,QMessageBox,QToolButton,QToolTip,QWidget
from shiboken6 import isValid

_theme = 'light'
TOOLTIP_DELAY_SECONDS = 0.35
TOOLTIP_GAP_PX = 6
TOOLTIP_FADE_MS = 90
# Legacy painter aliases resolve through the one semantic palette in
# native_ui_common.colors; keep the Qt color strings valid for QColor.
_PALETTES = ('light', 'dark')


def allows_hover_hint(widget):
    if not isinstance(widget,QAbstractButton):return False
    hides_text=isinstance(widget,QToolButton) and widget.toolButtonStyle()==Qt.ToolButtonIconOnly
    if widget.text().strip() and not hides_text:return False
    return not widget.icon().isNull() or bool(getattr(widget,'glyph',None)) or bool(widget.property('flickdockIconOnly'))


def _style_tooltip(widget):
    from native_ui_common import colors
    c=colors(_theme)
    if widget.property('flickdockTooltipTheme')!=_theme:widget.setProperty('flickdockTooltipTheme',_theme)
    tip=f"QTipLabel{{background:{c['popup_surface']};color:{c['text']};border:1px solid {c['input_border']};border-radius:10px;padding:6px 9px;}}"
    if widget.styleSheet()!=tip:widget.setStyleSheet(tip)


def _style_message_box(widget):
    from native_ui_common import colors
    palette=widget.palette();desired=QColor(colors(_theme)['dialog_surface'])
    if palette.color(QPalette.Window)!=desired:
        palette.setColor(QPalette.Window,desired);widget.setPalette(palette)


class _DialogPalette(QObject):
    def eventFilter(self,watched,event):
        if isinstance(watched,QMessageBox) and event.type() in (QEvent.Polish,QEvent.Show):
            _style_message_box(watched)
        return False


class _TooltipCorners(QObject):
    """Style tooltip surfaces and delay button hints until deliberate hover."""
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
        if not allows_hover_hint(button) or not button.isVisible() or not button.underMouse() or not button.toolTip():return
        # One anchor rule for every button: centred below, flipped above only
        # when the tip would leave the screen, so neighbours never disagree.
        top=button.mapToGlobal(QPoint(0,0));width=button.width()
        height=button.palette().fontMetrics().height()+18
        area=button.screen().availableGeometry() if button.screen() is not None else None
        x=top.x()+width//2-60
        below=top.y()+button.height()+TOOLTIP_GAP_PX
        above=top.y()-height-TOOLTIP_GAP_PX
        y=below if area is None or below+height<=area.bottom()+1 else max(area.top(),above)
        if area is not None:x=max(area.left(),min(x,area.right()-120))
        QToolTip.showText(QPoint(x,y),button.toolTip(),button)

    def eventFilter(self,watched,event):
        if getattr(self,'_updating',False):return False
        if event.type()==QEvent.ToolTip and isinstance(watched,QWidget) and not allows_hover_hint(watched):
            event.accept();return True
        if isinstance(watched,QAbstractButton):
            if event.type()==QEvent.Enter:
                self._timer.stop();self._button=weakref.ref(watched);self._entered_at=time.monotonic()
            elif event.type() in (QEvent.Leave,QEvent.Hide,QEvent.MouseButtonPress):
                if watched is self._current_button():self._timer.stop()
            elif event.type()==QEvent.ToolTip and watched.toolTip():
                if watched is not self._current_button():
                    self._button=weakref.ref(watched);self._entered_at=time.monotonic()
                self._position=event.globalPos()
                remaining=max(0.,TOOLTIP_DELAY_SECONDS-(time.monotonic()-self._entered_at))
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
                    # Apply the changing colors only to the tooltip itself.
                    # Replacing QApplication's stylesheet on every theme
                    # change repolishes the entire widget tree synchronously.
                    _style_tooltip(watched)
                    if event.type()==QEvent.Show:
                        watched.setWindowOpacity(0.0)
                        fade=QPropertyAnimation(watched,b'windowOpacity',watched)
                        fade.setDuration(TOOLTIP_FADE_MS);fade.setStartValue(0.0)
                        fade.setEndValue(1.0);fade.setEasingCurve(QEasingCurve.OutCubic)
                        fade.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
                        watched._flickdock_tip_fade=fade
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
    from native_ui_common import colors,font_css_stack,UiButton,icon
    from theme_accent import resolve_qss
    palette = QPalette()
    roles = {'Window':'surface','Base':'surface','AlternateBase':'canvas',
             'WindowText':'text','Text':'text','ButtonText':'text','Button':'button',
             'ToolTipBase':'surface','ToolTipText':'text','Highlight':'accent','HighlightedText':'on_action',
             'PlaceholderText':'secondary'}
    for role, key in roles.items():
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(color(key)))
    app.setPalette(palette)
    if not getattr(app,'_flickdock_dialog_palette',None):
        app._flickdock_dialog_palette=_DialogPalette(app);app.installEventFilter(app._flickdock_dialog_palette)
    # One application-level tooltip style also covers icon buttons created by
    # individual dialogs and QToolTip.showText().  Keep any existing app QSS.
    c=colors(_theme)
    if not hasattr(app,'_flickdock_tooltip_base'):app._flickdock_tooltip_base=app.styleSheet()
    tooltip=(f"QToolTip{{border-radius:10px;"
             f"padding:4px 8px;font:400 12px {font_css_stack()};}}")
    # Keep both themed rules installed once, avoiding an application-wide
    # repolish when the active theme changes.
    for name in ('light','dark'):
        tip_colors=colors(name)
        tooltip+=f"QToolTip[flickdockTooltipTheme=\"{name}\"]{{background:{tip_colors['popup_surface']};color:{tip_colors['text']};border:1px solid {tip_colors['input_border']};}}"
    stylesheet=(app._flickdock_tooltip_base+'\n'+tooltip).strip()
    if app.styleSheet()!=stylesheet:app.setStyleSheet(stylesheet)
    if not getattr(app,'_flickdock_tooltip_corners',None):
        QApplication.setEffectEnabled(Qt.UI_AnimateTooltip,False)
        QApplication.setEffectEnabled(Qt.UI_FadeTooltip,False)
        app._flickdock_tooltip_corners=_TooltipCorners(app);app.installEventFilter(app._flickdock_tooltip_corners)
    for widget in app.allWidgets():
        if widget.metaObject().className()=='QTipLabel':_style_tooltip(widget)
        if isinstance(widget,QMessageBox):_style_message_box(widget)
        if isinstance(widget,UiButton):widget.refresh_style()
        reference=getattr(widget,'_reference_style',None)
        if reference:
            resolved=resolve_qss(reference[_theme=='light'],_theme)
            if widget.styleSheet()!=resolved:widget.setStyleSheet(resolved)
        named_icon=getattr(widget,'_reference_icon',None)
        if named_icon:
            name,tint=named_icon;widget.setIcon(icon(name,c.get(tint,tint)))
        source = getattr(widget, '_flickdock_style', None)
        if source is not None:
            resolved=qss(source, font_family)
            if widget.styleSheet()!=resolved:widget.setStyleSheet(resolved)
        widget.update()
