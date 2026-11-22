"""Shared light/dark semantic colors for painted controls and Qt styles."""
from __future__ import annotations
import re
import weakref
from PySide6.QtCore import QEvent,QObject,QPoint,QTimer,Qt
from PySide6.QtGui import QColor,QPainter,QPalette
from PySide6.QtWidgets import QAbstractButton,QApplication,QMessageBox,QToolButton,QWidget
from shiboken6 import isValid

_theme = 'light'
TOOLTIP_DELAY_MS = 500
TOOLTIP_GAP_PX = 6
TOOLTIP_RADIUS_PX = 8
# Legacy painter aliases resolve through the one semantic palette in
# native_ui_common.colors; keep the Qt color strings valid for QColor.
_PALETTES = ('light', 'dark')


def allows_hover_hint(widget):
    if not isinstance(widget,QAbstractButton):return False
    if widget.property('flickdockHoverHelp'):return bool(widget.toolTip().strip())
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


class _PaintedTooltip(QWidget):
    def __init__(self,owner):
        super().__init__(owner,Qt.ToolTip|Qt.FramelessWindowHint|Qt.NoDropShadowWindowHint|
                         Qt.WindowDoesNotAcceptFocus|Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._text=''

    def paintEvent(self,event):
        from native_ui_common import colors,paint_rounded_surface,qss_color
        tokens=colors(_theme)
        painter=QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        painter.fillRect(self.rect(),Qt.transparent)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        paint_rounded_surface(painter,self.rect(),TOOLTIP_RADIUS_PX,qss_color(tokens['popup_surface']),qss_color(tokens['input_border']))
        painter.setPen(qss_color(tokens['text']))
        painter.drawText(self.rect().adjusted(9,5,-9,-5),Qt.AlignCenter|Qt.TextWordWrap|Qt.TextDontClip,self._text)
        painter.end()


class _TooltipCorners(QObject):
    """Use one directly timed tooltip for every single-icon button."""
    def __init__(self,parent):
        super().__init__(parent)
        self._button=None
        self._popup=None
        self._dismiss=QTimer(self);self._dismiss.setSingleShot(True)
        self._dismiss.timeout.connect(self._clear)
        self._timer=QTimer(self);self._timer.setSingleShot(True)
        self._timer.setInterval(TOOLTIP_DELAY_MS)
        self._timer.setTimerType(Qt.PreciseTimer);self._timer.timeout.connect(self._show_button_tip)

    def _current_button(self):
        button=self._button() if self._button is not None else None
        return button if button is not None and isValid(button) else None

    @staticmethod
    def _text(button):
        if not allows_hover_hint(button):return ''
        return (button.toolTip() or button.accessibleName()).strip()

    def _start(self,button):
        if self._current_button() is button:return
        self._clear()
        self._button=weakref.ref(button)
        self._timer.start()

    def _clear(self):
        self._timer.stop()
        self._dismiss.stop()
        self._button=None
        if self._popup is not None and isValid(self._popup):self._popup.hide()

    def _show_button_tip(self):
        button=self._current_button()
        if button is None or not button.isVisible() or not button.underMouse():return
        text=self._text(button)
        if not text:return
        self.show_tip(button,text)

    def show_tip(self,button,text,*,duration=0):
        from native_ui_common import screen_for_widget
        owner=button.window()
        popup=self._popup
        if popup is None or not isValid(popup) or popup.parentWidget() is not owner:
            if popup is not None and isValid(popup):popup.hide();popup.deleteLater()
            popup=self._popup=_PaintedTooltip(owner)
        popup.setFont(button.font())
        popup._text=text
        top=button.mapToGlobal(QPoint())
        metrics=popup.fontMetrics()
        from PySide6.QtGui import QGuiApplication
        screen=QGuiApplication.screenAt(top) or screen_for_widget(button)
        area=screen.availableGeometry() if screen is not None else None
        width=min(metrics.horizontalAdvance(text)+20,area.width() if area is not None else 560,560)
        height=metrics.boundingRect(0,0,max(1,width-18),0,Qt.TextWordWrap,text).height()+12
        x=top.x()+(button.width()-width)//2
        below=top.y()+button.height()+TOOLTIP_GAP_PX
        above=top.y()-height-TOOLTIP_GAP_PX
        y=below if area is None or below+height<=area.bottom()+1 else max(area.top(),above)
        if area is not None:x=max(area.left(),min(x,area.right()-width+1))
        popup.resize(width,height);popup.move(QPoint(x,y));popup.show();popup.update()
        if duration:self._dismiss.start(duration)

    def eventFilter(self,watched,event):
        if getattr(self,'_updating',False):return False
        kind=event.type()
        if kind==QEvent.ToolTip and isinstance(watched,QWidget):
            if self._text(watched):self._start(watched)
            event.accept();return True
        if isinstance(watched,QAbstractButton):
            if kind in (QEvent.Enter,QEvent.HoverEnter) and self._text(watched):
                self._start(watched)
            elif kind in (QEvent.Leave,QEvent.Hide,QEvent.MouseButtonPress,QEvent.WindowDeactivate):
                if watched is self._current_button():self._clear()
        if (kind in (QEvent.Hide,QEvent.WindowDeactivate) and self._popup is not None
                and isValid(self._popup) and self._popup.parentWidget() is watched):
            self._clear()
        if kind in (QEvent.Polish,QEvent.Show,QEvent.Resize) and watched.metaObject().className()=='QTipLabel':
            self._updating=True
            try:
                if not watched.testAttribute(Qt.WA_TranslucentBackground):
                    watched.setAttribute(Qt.WA_TranslucentBackground,True)
                if not watched.mask().isEmpty():watched.clearMask()
                if kind in (QEvent.Polish,QEvent.Show):
                    _style_tooltip(watched)
                    if watched.margin()!=0:watched.setMargin(0);watched.adjustSize()
                    if not watched.windowFlags() & Qt.NoDropShadowWindowHint:
                        watched.setWindowFlag(Qt.NoDropShadowWindowHint,True)
            finally:self._updating=False
        return False


def show_tooltip_feedback(anchor,text,duration=1600):
    app=QApplication.instance()
    if app is None:return
    controller=getattr(app,'_flickdock_tooltip_corners',None)
    if controller is None:
        controller=app._flickdock_tooltip_corners=_TooltipCorners(app)
        app.installEventFilter(controller)
    controller._clear()
    controller.show_tip(anchor,text,duration=duration)


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
