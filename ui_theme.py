"""Shared light/dark semantic colors for painted controls and Qt styles."""
from __future__ import annotations
import re
from PySide6.QtGui import QColor, QPalette

_theme = 'light'
_PALETTES = {
    'light': {'surface':'#ffffff','canvas':'#f5f7fa','text':'#263344','secondary':'#637186',
              'border':'#dce3ed','hover':'#e6f0fd','icon':'#344b65','button':'#eaf0f7',
              'selected':'#dcecff','tab':'#ffffff','accent':'#1876db'},
    'dark': {'surface':'#252b35','canvas':'#1d232d','text':'#edf2fa','secondary':'#acb9cc',
             'border':'#465266','hover':'#334966','icon':'#e0ebfb','button':'#394555',
             'selected':'#314e70','tab':'#435166','accent':'#7cb6ff'}
}

def set_theme(theme):
    global _theme
    _theme = theme if theme in _PALETTES else 'light'

def current_theme():
    return _theme

def color(key):
    return _PALETTES[_theme].get(key, _PALETTES[_theme]['text'])

_DARK = {
    '#fff':'#252b35', '#ffffff':'#252b35', '#fefeff':'#29303b',
    '#f8f9fb':'#2b3340', '#f5f7fa':'#1d232d', '#f5f7fb':'#1d232d',
    '#eef1f6':'#1c232d', '#e7ebf1':'#3e4b5d', '#e6eaf0':'#465266',
    '#e1e5ec':'#465266', '#e5e8ed':'#465266', '#e1e7ef':'#425167',
    '#dce3ed':'#465266', '#e5e5e9':'#465266', '#ededf0':'#465266',
    '#f4f8fd':'#263b54', '#bfdbfa':'#5b85b8', '#edf6ff':'#283f5d',
    '#75b8ff':'#84baff', '#d9dfe8':'#526780', '#f5f7fa':'#1d232d',
    '#e9f3ff':'#334966', '#d9eaff':'#355676', '#c3ccd9':'#687a93',
    '#c2cbd8':'#687a93', '#72aff3':'#83b8fa',
    '#1d1d1f':'#edf2fa', '#25272c':'#edf2fa', '#28313d':'#edf2fa',
    '#303a48':'#edf2fa', '#354252':'#e4edf9', '#282d35':'#edf2fa',
    '#313741':'#edf2fa', '#213247':'#f2f6ff', '#8a95a4':'#b5c3d8',
    '#7b838f':'#acb9cc', '#626c79':'#b5c3d8', '#738091':'#acb9cc',
    '#7d8796':'#acb9cc', '#7c8796':'#acb9cc', '#8490a0':'#acb9cc'
}

def qss(source, font_family):
    result = source.replace("'PingFang SC'", f"'{font_family}'").replace("'HarmonyOS Sans SC'", f"'{font_family}'")
    if _theme == 'dark':
        result = re.sub(r'#[0-9a-fA-F]{3,8}\b', lambda m: _DARK.get(m.group(0).lower(), m.group(0)), result)
        result = re.sub(r'(?<=background:)white\b', color('surface'), result)
    return result

def style_widget(widget, source, font_family):
    widget._flickdock_style = source
    widget.setStyleSheet(qss(source, font_family))

def apply_application(app, font_family):
    palette = QPalette()
    roles = {'Window':'surface','Base':'surface','AlternateBase':'canvas',
             'WindowText':'text','Text':'text','ButtonText':'text','Button':'button',
             'ToolTipBase':'surface','ToolTipText':'text','Highlight':'selected','HighlightedText':'text',
             'PlaceholderText':'secondary'}
    for role, key in roles.items():
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(color(key)))
    app.setPalette(palette)
    for widget in app.allWidgets():
        source = getattr(widget, '_flickdock_style', None)
        if source is not None:
            widget.setStyleSheet(qss(source, font_family))
        widget.update()
