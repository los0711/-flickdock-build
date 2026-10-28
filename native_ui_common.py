"""Native Qt primitives transcribed from the supplied 最终版.html.

The source's CSS tokens and SVG paths are retained here; no browser runtime is
used. Windows and macOS keep the separate radius, font and titlebar treatments
specified by that document.
"""
from __future__ import annotations

import sys
import os
from pathlib import Path

from PySide6.QtCore import QByteArray, QEasingCurve, QEvent, QObject, QPointF, QRectF, QSize, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QIconEngine, QPainter, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QAbstractButton, QHBoxLayout, QLabel, QPushButton, QWidget


WINDOWS_FONT_STACK=('HarmonyOS Sans SC','Noto Sans SC','Source Han Sans SC','Segoe UI Variable','Segoe UI')


def reduced_motion():
    """Read the platform preference without starting native hooks or prompts."""
    if sys.platform == 'darwin':
        try:
            from AppKit import NSWorkspace
            return bool(NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())
        except Exception:
            return False
    if sys.platform == 'win32':
        try:
            import ctypes
            value = ctypes.c_int(1)
            ok = ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(value), 0)
            return bool(ok and not value.value)
        except (AttributeError, OSError):
            return False
    return False


def keyboard_focus_visible(widget):
    return bool(widget.hasFocus() and widget.property('keyboardFocus'))


class _KeyboardFocusVisibility(QObject):
    """Expose keyboard focus without adding a blue ring after pointer clicks."""
    def eventFilter(self, watched, event):
        if not isinstance(watched, QAbstractButton):
            return False
        kind = event.type()
        if kind == QEvent.Type.FocusIn:
            visible = event.reason() in (Qt.FocusReason.TabFocusReason,
                Qt.FocusReason.BacktabFocusReason, Qt.FocusReason.ShortcutFocusReason)
        elif kind == QEvent.Type.FocusOut:
            visible = False
        elif kind == QEvent.Type.MouseButtonPress:
            visible = False
        elif kind == QEvent.Type.KeyPress:
            visible = True
        else:
            return False
        if bool(watched.property('keyboardFocus')) != visible:
            watched.setProperty('keyboardFocus', visible)
            watched.style().unpolish(watched)
            watched.style().polish(watched)
            watched.update()
        return False


def reference_font():
    """Use the application font chosen from the HTML's platform font stack."""
    return QFont(QApplication.font()) if QApplication.instance() else QFont()


def configure_reference_font(app):
    """Match installed browser fonts, without forcing the bundled Regular face.

    Qt's offscreen backend has no Windows font catalogue. Read only the
    installed fonts used by the reference so isolated screenshots resolve the
    same families and real weights as Chromium on this computer.
    """
    if sys.platform=='darwin':
        font=QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    else:
        if sys.platform=='win32' and app.platformName() in ('offscreen','minimal') and not getattr(app,'_reference_os_fonts_loaded',False):
            import winreg
            faces={'HarmonyOS Sans SC','HarmonyOS Sans SC Medium','HarmonyOS Sans SC Bold',
                   'Noto Sans SC','Source Han Sans SC',
                   'Segoe UI Variable','Segoe UI','Segoe UI Semibold','Segoe UI Bold'}
            for hive in (winreg.HKEY_CURRENT_USER,winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive,r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts') as key:
                        for index in range(winreg.QueryInfoKey(key)[1]):
                            name,value,_=winreg.EnumValue(key,index)
                            if name.removesuffix(' (TrueType)') not in faces or not isinstance(value,str):continue
                            path=Path(value)
                            if not path.is_absolute():path=Path(os.environ.get('SystemRoot','C:/Windows'))/'Fonts'/path
                            if path.is_file():QFontDatabase.addApplicationFont(str(path))
                except OSError:pass
            app._reference_os_fonts_loaded=True
        available=set(QFontDatabase.families())
        family=next((name for name in WINDOWS_FONT_STACK if name in available),None)
        font=QFont(family) if family else QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    # Preserve fractional horizontal advances, as Chromium does; full glyph
    # hinting rounds every character and makes the same 13px title ~10px wider.
    font.setHintingPreference(QFont.HintingPreference.PreferVerticalHinting)
    font.setPointSize(11);app.setFont(font)
    if not getattr(app, '_flickdock_focus_visibility', None):
        app._flickdock_focus_visibility = _KeyboardFocusVisibility(app)
        app.installEventFilter(app._flickdock_focus_visibility)
    return font


_DARK = {
    'background': '#161616', 'surface': '#1C1C1E',
    'sidebar': 'rgba(18,18,20,115)', 'preview': 'rgba(14,14,16,97)',
    'glass_border': 'rgba(255,255,255,20)', 'divider': 'rgba(255,255,255,18)',
    'highlight': 'rgba(255,255,255,36)', 'card': 'rgba(255,255,255,10)',
    'border': 'rgba(255,255,255,18)', 'card_hover': 'rgba(255,255,255,19)',
    'hover_border': 'rgba(255,255,255,36)', 'selected': 'rgba(10,132,255,46)',
    'selected_border': 'rgba(10,132,255,148)', 'control': 'rgba(255,255,255,15)',
    'hover': 'rgba(255,255,255,28)', 'input': '#242426',
    'input_border': 'rgba(255,255,255,36)', 'text': '#F5F5F7',
    'secondary': '#B2B2BA', 'muted': '#A1A1AA',
    'shadow': 'rgba(0,0,0,191)', 'shadow_blur': 64, 'shadow_y': 28,
}
_LIGHT = {
    'background': '#F0F2F6', 'surface': '#FFFFFF',
    'sidebar': 'rgba(242,244,248,184)', 'preview': 'rgba(245,246,250,173)',
    'glass_border': 'rgba(0,0,0,23)', 'divider': 'rgba(0,0,0,18)',
    'highlight': 'rgba(255,255,255,242)', 'card': 'rgba(255,255,255,184)',
    'border': 'rgba(0,0,0,18)', 'card_hover': 'rgba(255,255,255,245)',
    'hover_border': 'rgba(0,0,0,33)', 'selected': 'rgba(0,122,255,31)',
    'selected_border': 'rgba(0,122,255,140)', 'control': 'rgba(0,0,0,13)',
    'hover': 'rgba(0,0,0,23)', 'input': '#FFFFFF',
    'input_border': 'rgba(0,0,0,36)', 'text': '#1D1D1F',
    'secondary': '#515154', 'muted': '#68686D',
    'shadow': 'rgba(15,23,42,41)', 'shadow_blur': 56, 'shadow_y': 24,
}
_SEMANTIC = {
    'blue': '#0A84FF', 'green': '#30D158', 'orange': '#FF9F0A',
    'red': '#FF453A', 'purple': '#BF5AF2', 'cyan': '#64D2FF', 'steel': '#567DAF',
}


def colors(theme='dark', platform=None):
    result = dict(_LIGHT if str(theme).lower() == 'light' else _DARK)
    result.update(_SEMANTIC)
    result.update(action='#0062CC', on_action='#FFFFFF',
                  link='#0062CC' if str(theme).lower() == 'light' else '#6DAEFF')
    if str(theme).lower() == 'light':
        result.update(green='#167D3D', red='#BD2C25', orange='#9D5800')
    is_mac = str(platform or ('mac' if sys.platform == 'darwin' else 'win')).lower() in ('mac', 'macos', 'darwin')
    result.update(radius=16 if is_mac else 10, card_radius=12 if is_mac else 8)
    result.update(primary=result['text'], accent=result['blue'], danger=result['red'], success=result['green'])
    return result


# Paths copied verbatim from the source document's toolbar and category SVGs.
_SVG_BODY = {
    'cloud': '<path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z"/>',
    'pin': '<path d="M16 3l5 5-4 1-4 4 1 5-8-8 5 1 4-4 1-4z"/><path d="M9 15l-6 6"/>',
    'expand': '<path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/>',
    'collapse': '<path d="M4 14h6v6M20 10h-6V4M14 10l7-7M3 21l7-7"/>',
    'more': '<path d="M5 12h.01M12 12h.01M19 12h.01"/>',
    'dock': '<path d="M13 5l7 7-7 7M5 5l7 7-7 7"/>',
    'nearby': '<path d="M2 8.82a15 15 0 0 1 20 0"/><path d="M5 12.859a10 10 0 0 1 14 0"/><path d="M8.5 16.429a5 5 0 0 1 7 0"/><circle cx="12" cy="19.5" r="1" fill="currentColor" stroke="none"/>',
    'settings': '<circle cx="12" cy="12" r="3"/><path stroke-linecap="round" d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 01-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06a1.65 1.65 0 001.82.33H9a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06a1.65 1.65 0 00-.33 1.82V9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"/>',
    'search': '<path d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/>',
    'list': '<path d="M4 6h16M4 12h16M4 18h16"/>',
    'grid': '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    'chevron': '<path d="M6 9l6 6 6-6"/>',
    'right': '<path d="M9 5l7 7-7 7"/>',
    'all': '<path d="M4 6h16M4 12h16M4 18h16"/>',
    'image': '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/>',
    'video': '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="M10 9l5 3-5 3V9z"/>',
    'audio': '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
    'text': '<path d="M4 7V4h16v3M9 20h6M12 4v16"/>',
    'file': '<path d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/>',
    'web': '<circle cx="12" cy="12" r="9"/><path d="M3.6 9h16.8M3.6 15h16.8M12 3a15 15 0 010 18M12 3a15 15 0 000 18"/>',
    'transferring': '<path d="M7 16V4m0 0L3 8m4-4l4 4m6 0v12m0 0l4-4m-4 4l-4-4"/>',
    'refresh': '<circle opacity="0.25" cx="12" cy="12" r="9" stroke="currentColor" stroke-width="3"/><path opacity="0.90" fill="currentColor" stroke="none" d="M4 12a8 8 0 018-8V1C5.925 1 1 5.925 1 12h3z"/>',
    'play': '<path d="M4 3l9 5-9 5V3z"/>',
    'check': '<path d="m5 12 4 4L19 6"/>',
    'close': '<path d="m6 6 12 12M18 6 6 18"/>',
    'trash': '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>',
    'copy': '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h3"/>',
    'user': '<circle cx="12" cy="8" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/>',
    'pause': '<path d="M8 4v16M16 4v16"/>',
    'desktop': '<rect x="3" y="3" width="18" height="13" rx="2"/><path d="M12 16v5M8 21h8"/>',
    'laptop': '<rect x="5" y="3" width="14" height="13" rx="2"/><path d="m5 16-3 5h20l-3-5M9 18h6"/>',
    'sun': '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5"/>',
    'moon': '<path d="M20.5 13A9 9 0 0 1 11 3.5 9 9 0 1 0 20.5 13Z"/>',
    'plus': '<path d="M12 4v16M4 12h16"/>',
    'shelf': '<path d="m3 13 3-9h12l3 9v7H3v-7Z"/><path d="M3 13h5l2 3h4l2-3h5"/>',
}
_ALIASES = {'folder': 'file', 'upload': 'transferring', 'download': 'transferring',
            'wifi': 'nearby', 'lan': 'nearby', 'chevron-down': 'chevron',
            'chevron-right': 'right', 'cloud-upload': 'cloud', 'cloud-off': 'cloud'}


class _SourceIconEngine(QIconEngine):
    def __init__(self, name, color):
        super().__init__()
        self.name, self.color = name, QColor(color)
        self.svg = None
        key = _ALIASES.get(name, name)
        if key in _SVG_BODY:
            body = _SVG_BODY[key]
            if key == 'play':
                attributes = 'viewBox="0 0 16 16" fill="currentColor" stroke="none"'
            else:
                attributes = ('viewBox="0 0 24 24" fill="' + ('currentColor' if key == 'pin' else 'none')
                              + '" stroke="currentColor" stroke-width="' + ('2.2' if key == 'more' else '1.8')
                              + '" stroke-linecap="round" stroke-linejoin="round"')
            if name == 'left':
                body = '<g transform="translate(24 0) scale(-1 1)">' + body + '</g>'
            data = '<svg xmlns="http://www.w3.org/2000/svg" ' + attributes + '>' + body + '</svg>'
            self.svg = data.replace('currentColor', self.color.name())

    def clone(self):
        return _SourceIconEngine(self.name, self.color)

    def paint(self, painter, rect, mode, state):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if mode == QIcon.Mode.Disabled:
            painter.setOpacity(0.4)
        if self.svg:
            renderer = QSvgRenderer(QByteArray(self.svg.encode('utf-8')))
            renderer.render(painter, QRectF(rect))
        painter.restore()

    def pixmap(self, size, mode, state):
        result = QPixmap(size)
        result.fill(Qt.GlobalColor.transparent)
        painter = QPainter(result)
        self.paint(painter, result.rect(), mode, state)
        painter.end()
        return result


def icon(name, color='#A1A1AA', size=16):
    # size is accepted for convenient call-site parity; this vector engine is
    # rendered at the actual button's iconSize and device pixel ratio.
    if name == 'left':
        _ALIASES['left'] = 'right'
    return QIcon(_SourceIconEngine(str(name), color))


def _context(widget):
    current = widget
    while current is not None:
        theme = current.property('uiTheme')
        if theme:
            return str(theme), str(current.property('uiPlatform') or 'win')
        current = current.parentWidget()
    return 'dark', 'mac' if sys.platform == 'darwin' else 'win'


class UiLabel(QLabel):
    def __init__(self, text='', size=12, weight=400, color='primary', parent=None):
        super().__init__(text, parent)
        self._ui_size, self._ui_weight, self._ui_color = size, weight, color
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.refresh_style()

    def refresh_style(self):
        palette = colors(*_context(self))
        value = palette.get(self._ui_color, self._ui_color)
        font = self.font()
        font.setPixelSize(int(self._ui_size))
        font.setWeight(QFont.Weight(max(100, min(900, int(self._ui_weight)))))
        self.setFont(font)
        self.setStyleSheet(f'color:{value}; background:transparent; border:none;')


class UiButton(QPushButton):
    def __init__(self, text='', icon='', variant='secondary', parent=None):
        super().__init__(text, parent)
        self.variant = str(variant)
        self._ui_icon_name = icon if isinstance(icon, str) else ''
        self.setProperty('uiVariant', self.variant)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setIconSize(QSize(16, 16))
        self.setFixedHeight(32 if variant == 'toolbar' else 28)
        if variant == 'toolbar':
            self.setFixedWidth(32)
        if isinstance(icon, QIcon):
            self.setIcon(icon)
        self.refresh_style()

    def refresh_style(self):
        c = colors(*_context(self))
        primary = self.variant in ('primary', 'accent')
        danger = self.variant in ('danger', 'destructive')
        plain = self.variant in ('toolbar', 'ghost', 'text', 'link')
        foreground = c['on_action'] if primary else c['red'] if danger else c['text']
        background = c['action'] if primary else 'rgba(255,69,58,38)' if danger else 'transparent' if plain else c['control']
        border = 'rgba(255,69,58,77)' if danger else 'transparent' if plain or primary else c['glass_border']
        hover = 'rgba(10,132,255,230)' if primary else 'rgba(255,69,58,64)' if danger else c['hover']
        padding = '0px' if self.variant == 'toolbar' else '0px 12px'
        self.setStyleSheet(f'''QPushButton {{ background:{background}; color:{foreground};
            border:1px solid {border}; border-radius:8px; padding:{padding};
            font-size:12px; font-weight:500; }}
            QPushButton:hover {{ background:{hover}; }}
            QPushButton:pressed {{ background:{c['selected'] if plain else hover}; }}
            QPushButton:checked {{ background:{c['selected']}; color:{c['blue']}; }}
            QPushButton:disabled {{ color:{c['muted']}; background:{c['control']}; border-color:transparent; }}
            QPushButton[keyboardFocus="true"] {{border:1px solid {c['link']};}}''')
        if self._ui_icon_name:
            self.setIcon(icon(self._ui_icon_name, foreground))


class UiSwitch(QAbstractButton):
    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(bool(checked))
        self.setFixedSize(44, 32)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._position = 1.0 if checked else 0.0
        self._motion = QVariantAnimation(self)
        self._motion.setDuration(240)
        curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
        curve.addCubicBezierSegment(QPointF(0.22, 1), QPointF(0.36, 1), QPointF(1, 1))
        self._motion.setEasingCurve(curve)
        self._motion.valueChanged.connect(self._animate)
        self.toggled.connect(self._toggle)

    def _toggle(self, checked):
        self._motion.stop()
        if reduced_motion():
            self._animate(1.0 if checked else 0.0)
            return
        self._motion.setStartValue(self._position)
        self._motion.setEndValue(1.0 if checked else 0.0)
        self._motion.start()

    def _animate(self, value):
        self._position = float(value)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        start, end = QColor(142,142,147,82), QColor('#30D158')
        p = self._position
        color = QColor(*(round(a + (b-a)*p) for a,b in zip(start.getRgb(), end.getRgb())))
        if not self.isEnabled():
            painter.setOpacity(0.4)
        painter.setBrush(color)
        painter.translate(3,5)
        painter.drawRoundedRect(QRectF(0,0,38,22),11,11)
        painter.setBrush(QColor(0,0,0,35))
        painter.drawEllipse(QRectF(2+16*p,3,18,18))
        painter.setBrush(QColor('#FFFFFF'))
        painter.drawEllipse(QRectF(2+16*p,2,18,18))
        if keyboard_focus_visible(self):
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(colors(*_context(self))['link']), 1.5))
            painter.drawRoundedRect(QRectF(-2,-2,42,26),13,13)


class _Titlebar(QWidget):
    def __init__(self, surface, title):
        super().__init__(surface)
        self.surface = surface
        self.setFixedHeight(48)
        self._drag = None
        self.layout_box = QHBoxLayout(self)
        self.layout_box.setContentsMargins(0,0,0,0)
        self.layout_box.setSpacing(12)
        _, platform = _context(surface)
        self.traffic_buttons = []
        if platform in ('mac','macos','darwin'):
            traffic = QWidget(self)
            row = QHBoxLayout(traffic)
            row.setContentsMargins(0,0,0,0)
            row.setSpacing(8)
            close_action = (lambda: surface.navigate('overview')) if title in ('设置','局域网互传') else (lambda: surface.controller.command('dock'))
            callbacks = (close_action, lambda: surface.navigate('mini'),
                         lambda: surface.controller.command('pin'))
            for value, callback in zip(('#FF5F56','#FFBD2E','#27C93F'), callbacks):
                control = QPushButton(traffic)
                control.setFixedSize(12,12)
                control.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                control.setCursor(Qt.CursorShape.PointingHandCursor)
                control.setStyleSheet(f'QPushButton{{background:{value};border:0;border-radius:6px;}} QPushButton:hover{{border:1px solid rgba(255,255,255,64);}}')
                control.clicked.connect(callback)
                row.addWidget(control)
                self.traffic_buttons.append(control)
            if title in ('设置','局域网互传'):
                self.traffic_buttons[-1].setEnabled(False)
            self.layout_box.addWidget(traffic)
        self.title_label = UiLabel(title,15,600,parent=self)
        self.layout_box.addWidget(self.title_label)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            top = self.surface.window()
            handle = top.windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return
            self._drag = event.globalPosition().toPoint() - top.pos()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.surface.window().move(event.globalPosition().toPoint()-self._drag)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag = None
        super().mouseReleaseEvent(event)


def titlebar(surface, title):
    """Return the source header's left section; page owns its right actions."""
    return _Titlebar(surface, title)


def apply_theme(widget, theme='dark', platform=None):
    platform = platform or ('mac' if sys.platform == 'darwin' else 'win')
    c = colors(theme, platform)
    widget.setProperty('uiTheme', str(theme))
    widget.setProperty('uiPlatform', str(platform))
    font = reference_font()
    font.setPixelSize(12)
    widget.setFont(font)
    widget.setStyleSheet(f'''
        QWidget {{ color:{c['text']}; font-size:12px; }}
        QLabel {{ background:transparent; border:0; }}
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ background:{c['input']};
            color:{c['text']}; border:1px solid {c['input_border']}; border-radius:8px;
            padding:5px 10px; min-height:18px; selection-background-color:{c['blue']}; }}
        QLineEdit:focus, QComboBox:focus {{ border-color:{c['blue']}; }}
        QComboBox QAbstractItemView {{ background:{c['input']}; color:{c['text']};
            selection-background-color:{c['blue']}; padding:4px; border:1px solid {c['input_border']}; }}
        QComboBox QAbstractItemView::item {{ min-height:30px; padding:4px 10px; }}
        QScrollArea {{ background:transparent; border:0; }}
        QScrollBar:vertical {{ background:transparent; width:6px; margin:0; }}
        QScrollBar::handle:vertical {{ background:rgba(142,142,147,71); border-radius:3px; min-height:24px; }}
        QScrollBar::handle:vertical:hover {{ background:rgba(142,142,147,115); }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:transparent; }}
        QMenu {{ background:{c['input']}; color:{c['text']}; border:1px solid {c['glass_border']};
            border-radius:8px; padding:4px 0; }}
        QMenu::item {{ padding:6px 14px; }}
        QMenu::item:selected {{ background:{c['blue']}; color:white; }}
    ''')
    for child in [widget] + widget.findChildren(QWidget):
        if isinstance(child, (UiButton, UiLabel)):
            child.refresh_style()
