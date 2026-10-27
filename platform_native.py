"""Small native boundary; all macOS frameworks load lazily on a Cocoa host."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QCursor, QGuiApplication


def data_directory(app_name='FlickDock', *, platform=None, home=None, environ=None):
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    home = Path(home or Path.home())
    if platform == 'win32':
        return Path(environ.get('APPDATA', str(home))) / app_name
    if platform == 'darwin':
        return home / 'Library' / 'Application Support' / app_name
    return Path(environ.get('XDG_DATA_HOME', str(home / '.local' / 'share'))) / app_name


def app_bundle(executable=None):
    executable = Path(executable or sys.executable).resolve()
    if (executable.parent.name == 'MacOS' and executable.parent.parent.name == 'Contents'
            and executable.parent.parent.parent.suffix.lower() == '.app'):
        return executable.parent.parent.parent
    return None


def native_input_available(*, platform=None, qt_platform=None):
    platform = sys.platform if platform is None else platform
    if qt_platform is None:
        application = QGuiApplication.instance()
        qt_platform = application.platformName() if application else ''
    return ((platform == 'win32' and str(qt_platform).lower() == 'windows')
            or (platform == 'darwin' and str(qt_platform).lower() == 'cocoa'))


def shelf_window_flags(*, platform=None):
    """Keep the shelf in the ordinary app layer, without a Windows taskbar entry.

    Qt.Tool is a floating NSPanel on macOS even without the stay-on-top hint,
    so a normal NSWindow is needed there to let the active app cover the shelf.
    The separate edge trigger remains a topmost, nonactivating tool window.
    """
    platform = sys.platform if platform is None else platform
    kind = Qt.WindowType.Window if platform == 'darwin' else Qt.WindowType.Tool
    return kind | Qt.WindowType.FramelessWindowHint


def keep_tool_visible_when_inactive(widget, *, platform=None):
    """Keep the macOS Qt.Tool edge strip visible when the app deactivates.

    Cocoa-backed Qt.Tool windows are NSPanel instances. Qt hides them when the
    application deactivates unless this attribute is set before first show.
    """
    platform = sys.platform if platform is None else platform
    if platform != 'darwin':
        return False
    widget.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)
    return True


def remove_host_window_shadow(widget):
    """Keep the translucent shelf host from casting a rectangular Cocoa shadow.

    The rounded card paints its own shadow; its larger native host is only a
    transparent animation canvas. Cocoa otherwise shadows that entire canvas.
    """
    if sys.platform != 'darwin' or not native_input_available():
        return False
    import objc
    view = objc.objc_object(c_void_p=ctypes.c_void_p(int(widget.winId())))
    window = view.window()
    if window is None:
        return False
    window.setHasShadow_(False)
    return True


def _qt_mask():
    buttons = QGuiApplication.mouseButtons()
    return sum(bit for bit, flag in ((1, Qt.MouseButton.LeftButton), (2, Qt.MouseButton.RightButton),
                                     (4, Qt.MouseButton.MiddleButton)) if buttons & flag)


_mac_input = None


class MacInput:
    """Quartz coordinates and Qt cursor coordinates are logical screen points.

    Only geometry is read; no screenshots, accessibility prompts or event taps.
    A normal-level window whose bounds change during the held press is treated
    as a window move until release; the independent MIME gate remains required.
    """
    def __init__(self, quartz=None, cursor=None):
        if quartz is None:
            import Quartz
            quartz = Quartz
        self.quartz, self.cursor = quartz, cursor or QCursor.pos
        self._held = False
        self._window, self._bounds, self._moving = None, None, False

    def buttons(self):
        quartz = self.quartz
        return sum(bit for bit, button in ((1, 0), (2, 1), (4, 2))
                   if quartz.CGEventSourceButtonState(quartz.kCGEventSourceStateCombinedSessionState, button))

    def window_moving(self):
        if not self.buttons() & 1:
            self._held = False
            self._window, self._bounds, self._moving = None, None, False
            return False
        quartz = self.quartz
        if not self._held:
            self._held = True
            point = self.cursor()
            windows = quartz.CGWindowListCopyWindowInfo(
                quartz.kCGWindowListOptionOnScreenOnly | quartz.kCGWindowListExcludeDesktopElements,
                quartz.kCGNullWindowID) or []
            for window in windows:
                bounds = window.get(quartz.kCGWindowBounds, {})
                if (window.get(quartz.kCGWindowLayer, 0) == 0 and window.get(quartz.kCGWindowAlpha, 1) > 0
                        and bounds.get('X', 0) <= point.x() < bounds.get('X', 0) + bounds.get('Width', 0)
                        and bounds.get('Y', 0) <= point.y() < bounds.get('Y', 0) + bounds.get('Height', 0)):
                    self._window = window.get(quartz.kCGWindowNumber)
                    self._bounds = dict(bounds)
                    break
        elif self._window is not None:
            windows = quartz.CGWindowListCopyWindowInfo(quartz.kCGWindowListOptionIncludingWindow,
                                                       self._window) or []
            for window in windows:
                if window.get(quartz.kCGWindowNumber) == self._window:
                    self._moving = self._moving or dict(window.get(quartz.kCGWindowBounds, {})) != self._bounds
                    break
        return self._moving


def _mac():
    global _mac_input
    if _mac_input is None:
        _mac_input = MacInput()
    return _mac_input


def mouse_buttons_mask():
    if not native_input_available():
        return _qt_mask()
    if sys.platform == 'win32':
        return sum(bit for bit, key in ((1, 1), (2, 2), (4, 4))
                   if ctypes.windll.user32.GetAsyncKeyState(key) & 0x8000)
    try:
        return _mac().buttons()
    except (ImportError, OSError, RuntimeError):
        return _qt_mask()


def window_move_active():
    if not native_input_available():
        return False
    if sys.platform == 'win32':
        from ctypes import wintypes
        class GUIThreadInfo(ctypes.Structure):
            _fields_ = [('cbSize', wintypes.DWORD), ('flags', wintypes.DWORD),
                        ('hwndActive', wintypes.HWND), ('hwndFocus', wintypes.HWND),
                        ('hwndCapture', wintypes.HWND), ('hwndMenuOwner', wintypes.HWND),
                        ('hwndMoveSize', wintypes.HWND), ('hwndCaret', wintypes.HWND),
                        ('rcCaret', wintypes.RECT)]
        info = GUIThreadInfo()
        info.cbSize = ctypes.sizeof(info)
        try:
            api = ctypes.WinDLL('user32', use_last_error=True)
            api.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUIThreadInfo)]
            api.GetGUIThreadInfo.restype = wintypes.BOOL
            return bool(api.GetGUIThreadInfo(0, ctypes.byref(info)) and (info.flags & 2 or info.hwndMoveSize))
        except OSError:
            return False
    try:
        return _mac().window_moving()
    except (ImportError, OSError, RuntimeError, TypeError):
        return False


def configure_media_backend(*, platform=None):
    platform = sys.platform if platform is None else platform
    if platform == 'win32':
        os.environ['QT_MEDIA_BACKEND'] = 'windows'
    elif platform == 'darwin':
        os.environ['QT_MEDIA_BACKEND'] = 'darwin'


class _EventType(ctypes.Structure):
    _fields_ = [('eventClass', ctypes.c_uint32), ('eventKind', ctypes.c_uint32)]


class _HotkeyID(ctypes.Structure):
    _fields_ = [('signature', ctypes.c_uint32), ('id', ctypes.c_uint32)]


class _HotkeyDispatch(QObject):
    activated = Signal(int)


# Existing HotkeyManager uses Win VK identities, independent of native keycodes.
_MAC_KEYS = {ord(letter): code for letter, code in zip('ABCDEFGHIJKLMNOPQRSTUVWXYZ',
             (0, 11, 8, 2, 14, 3, 5, 4, 34, 38, 40, 37, 46, 45, 31, 35, 12, 15, 1, 17, 32, 9, 13, 7, 16, 6))}
_MAC_KEYS.update({ord(digit): code for digit, code in zip('0123456789', (29, 18, 19, 20, 21, 23, 22, 26, 28, 25))})
_MAC_KEYS.update({0x70 + index: code for index, code in enumerate((122, 120, 99, 118, 96, 97, 98, 100, 101, 109,
                                                                103, 111, 105, 107, 113, 106, 64, 79, 80, 90))})
_MAC_KEYS.update({0x20: 49, 0x09: 48, 0x08: 51, 0x0D: 36, 0x1B: 53, 0x2E: 117, 0x24: 115,
                  0x23: 119, 0x21: 116, 0x22: 121, 0x25: 123, 0x26: 126, 0x27: 124, 0x28: 125})


class MacHotkeyBackend:
    """Carbon app event hotkeys, without keyboard event monitoring permissions.

    Supports register/unregister expected by global_hotkeys.HotkeyManager.
    Qt Control means Command on macOS; Qt Meta means the physical Control key.
    The manager must call close() after unregistering its hotkeys.
    """
    def __init__(self, on_activated, *, carbon=None):
        if carbon is None:
            if sys.platform != 'darwin' or not native_input_available():
                raise RuntimeError('macOS 全局快捷键仅在 Cocoa 桌面启用。')
            carbon = ctypes.CDLL('/System/Library/Frameworks/Carbon.framework/Carbon')
        self.api, self.last_error = carbon, 0
        self.registrations, self._held = {}, set()
        self._closed = False
        self._on_activated = on_activated
        self._dispatch = _HotkeyDispatch()
        self._dispatch.activated.connect(self._activate_if_open, Qt.ConnectionType.QueuedConnection)
        self._signature = int.from_bytes(b'FlDk', 'big')
        callback_type = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
        self._callback = callback_type(self._native_event)
        declarations = {
            'GetApplicationEventTarget': ([], ctypes.c_void_p),
            'InstallEventHandler': ([ctypes.c_void_p, callback_type, ctypes.c_uint32,
                                     ctypes.POINTER(_EventType), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], ctypes.c_int32),
            'RegisterEventHotKey': ([ctypes.c_uint32, ctypes.c_uint32, _HotkeyID, ctypes.c_void_p,
                                     ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)], ctypes.c_int32),
            'UnregisterEventHotKey': ([ctypes.c_void_p], ctypes.c_int32),
            'RemoveEventHandler': ([ctypes.c_void_p], ctypes.c_int32),
            'GetEventKind': ([ctypes.c_void_p], ctypes.c_uint32),
            'GetEventParameter': ([ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                   ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int32)}
        for name, (arguments, result) in declarations.items():
            function = getattr(carbon, name)
            if isinstance(function, ctypes._CFuncPtr):
                function.argtypes, function.restype = arguments, result
        self._target = carbon.GetApplicationEventTarget()
        self._handler = ctypes.c_void_p()
        types = (_EventType * 2)(_EventType(int.from_bytes(b'keyb', 'big'), 6),
                                _EventType(int.from_bytes(b'keyb', 'big'), 7))
        result = carbon.InstallEventHandler(self._target, self._callback, 2, types, None, ctypes.byref(self._handler))
        if result:
            raise OSError(f'无法注册 macOS 快捷键处理器 ({result})。')

    def _activate_if_open(self, identifier):
        if not self._closed and identifier in self.registrations:
            self._on_activated(identifier)

    def register(self, identifier, modifiers, virtual_key):
        if self._closed or virtual_key not in _MAC_KEYS:
            self.last_error = 87
            return False
        native_modifiers = sum(native for flag, native in ((2, 1 << 8), (1, 1 << 11), (4, 1 << 9), (8, 1 << 12))
                               if modifiers & flag)
        reference = ctypes.c_void_p()
        result = self.api.RegisterEventHotKey(_MAC_KEYS[virtual_key], native_modifiers,
                  _HotkeyID(self._signature, identifier), self._target, 0, ctypes.byref(reference))
        self.last_error = 1409 if result == -9878 else result
        if result:
            return False
        self.registrations[identifier] = reference
        return True

    def unregister(self, identifier):
        reference = self.registrations.pop(identifier, None)
        if reference is not None:
            self.api.UnregisterEventHotKey(reference)
        self._held.discard(identifier)

    def _pressed(self, identifier, pressed):
        if self._closed or identifier not in self.registrations:
            return
        if pressed:
            if identifier not in self._held:
                self._held.add(identifier)
                self._dispatch.activated.emit(identifier)
        else:
            self._held.discard(identifier)

    def _native_event(self, handler, event, context):
        try:
            identifier = _HotkeyID()
            result = self.api.GetEventParameter(event, int.from_bytes(b'----', 'big'),
                      int.from_bytes(b'hkid', 'big'), None, ctypes.sizeof(identifier), None, ctypes.byref(identifier))
            if result == 0 and identifier.signature == self._signature:
                self._pressed(identifier.id, self.api.GetEventKind(event) == 6)
                return 0
        except (OSError, ValueError, RuntimeError):
            pass
        return -9874  # eventNotHandledErr

    def close(self):
        if self._closed:
            return
        self._closed = True
        for identifier in tuple(self.registrations):
            self.unregister(identifier)
        self.api.RemoveEventHandler(self._handler)
