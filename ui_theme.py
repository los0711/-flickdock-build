"""Shared light/dark semantic colors for painted controls and Qt styles."""
from __future__ import annotations
import re
from PySide6.QtGui import QColor, QPalette

_theme = 'light'
_PALETTES = {
    'light': {'surface':'#ffffff','canvas':'#f0f2f6','text':'#1d1d1f','secondary':'#515154',
              'border':'#dfe2e8','hover':'#e8f2ff','icon':'#3a3a3c','button':'#f1f2f5',
              'selected':'#d6eaff','tab':'#ffffff','accent':'#007aff'},
    'dark': {'surface':'#1c1c1e','canvas':'#161616','text':'#f5f5f7','secondary':'#a1a1aa',
             'border':'#3a3a3c','hover':'#303034','icon':'#e4e4e7','button':'#2c2c2e',
             'selected':'#1c3854','tab':'#2c2c2e','accent':'#0a84ff'}
}

def set_theme(theme):
    global _theme
    _theme = theme if theme in _PALETTES else 'light'

def current_theme():
    return _theme

def color(key):
    return _PALETTES[_theme].get(key, _PALETTES[_theme]['text'])

_DARK = {
    '#fff':'#1c1c1e', '#ffffff':'#1c1c1e', '#fefeff':'#242426',
    '#f8f9fb':'#242426', '#f5f7fa':'#161616', '#f5f7fb':'#161616',
    '#eef1f6':'#242426', '#e7ebf1':'#2c2c2e', '#e6eaf0':'#3a3a3c',
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

def qss(source, font_family):
    from native_ui_common import readable_qss
    result = source.replace("'PingFang SC'", f"'{font_family}'").replace("'HarmonyOS Sans SC'", f"'{font_family}'")
    if _theme == 'dark':
        result = re.sub(r'#[0-9a-fA-F]{3,8}\b', lambda m: _DARK.get(m.group(0).lower(), m.group(0)), result)
        result = re.sub(r'(?<=background:)white\b', color('surface'), result)
    return readable_qss(result)

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
