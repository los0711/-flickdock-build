"""Per-user desktop hotkeys with transactional RegisterHotKey ownership.

The offscreen/minimal Qt backends never call Windows input-registration APIs.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import sys
from typing import Callable

from PySide6.QtCore import QAbstractNativeEventFilter, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication


WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_META = 0x0008
MOD_NOREPEAT = 0x4000


@dataclass(frozen=True)
class Shortcut:
    text: str
    modifiers: int
    virtual_key: int

    @property
    def identity(self) -> tuple[int, int]:
        return self.modifiers, self.virtual_key


def parse_shortcut(value: str) -> Shortcut:
    """Parse one portable Qt shortcut into a Win32 modifier/VK pair."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("快捷键不能为空")
    sequence = QKeySequence.fromString(value.strip(), QKeySequence.SequenceFormat.PortableText)
    if sequence.isEmpty() or sequence.count() != 1:
        raise ValueError("请使用单组快捷键，例如 Ctrl+Alt+Space")
    combination = sequence[0]
    flags = combination.keyboardModifiers()
    if flags & Qt.KeyboardModifier.MetaModifier and sys.platform != 'darwin':
        raise ValueError("请使用 Ctrl、Alt、Shift 组合，不能使用 Windows 键")
    permitted = (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier |
                 Qt.KeyboardModifier.ShiftModifier)
    if sys.platform == 'darwin':
        permitted |= Qt.KeyboardModifier.MetaModifier
    if flags & ~permitted:
        raise ValueError("该快捷键包含不支持的修饰键")
    key = int(combination.key())
    modifiers = 0
    if flags & Qt.KeyboardModifier.ControlModifier:
        modifiers |= MOD_CONTROL
    if flags & Qt.KeyboardModifier.AltModifier:
        modifiers |= MOD_ALT
    if flags & Qt.KeyboardModifier.ShiftModifier:
        modifiers |= MOD_SHIFT
    if flags & Qt.KeyboardModifier.MetaModifier:
        modifiers |= MOD_META
    if int(Qt.Key.Key_A) <= key <= int(Qt.Key.Key_Z) or int(Qt.Key.Key_0) <= key <= int(Qt.Key.Key_9):
        virtual_key = key
    elif int(Qt.Key.Key_F1) <= key <= int(Qt.Key.Key_F24):
        virtual_key = 0x70 + key - int(Qt.Key.Key_F1)
    else:
        named = {
            int(Qt.Key.Key_Space): 0x20, int(Qt.Key.Key_Tab): 0x09,
            int(Qt.Key.Key_Backspace): 0x08, int(Qt.Key.Key_Return): 0x0D,
            int(Qt.Key.Key_Enter): 0x0D, int(Qt.Key.Key_Escape): 0x1B,
            int(Qt.Key.Key_Delete): 0x2E, int(Qt.Key.Key_Insert): 0x2D,
            int(Qt.Key.Key_Home): 0x24, int(Qt.Key.Key_End): 0x23,
            int(Qt.Key.Key_PageUp): 0x21, int(Qt.Key.Key_PageDown): 0x22,
            int(Qt.Key.Key_Left): 0x25, int(Qt.Key.Key_Up): 0x26,
            int(Qt.Key.Key_Right): 0x27, int(Qt.Key.Key_Down): 0x28,
            int(Qt.Key.Key_Pause): 0x13, int(Qt.Key.Key_Print): 0x2C,
        }
        virtual_key = named.get(key)
        if virtual_key is None:
            raise ValueError("支持字母、数字、F1–F24、空格、回车和方向键等快捷键")
    if key == 0 or virtual_key is None:
        raise ValueError("无法识别该快捷键")
    text = sequence.toString(QKeySequence.SequenceFormat.PortableText)
    return Shortcut(text, modifiers, virtual_key)


def normalize_shortcut(value: str) -> str:
    return parse_shortcut(value).text


class _NoopBackend:
    """Local-only owner table for tests; never touches the operating system."""
    def __init__(self):
        self.registrations: dict[int, tuple[int, int]] = {}
        self.last_error = 0

    def register(self, identifier: int, modifiers: int, virtual_key: int) -> bool:
        pair = (modifiers & ~MOD_NOREPEAT, virtual_key)
        if pair in self.registrations.values():
            self.last_error = 1409
            return False
        self.registrations[identifier] = pair
        self.last_error = 0
        return True

    def unregister(self, identifier: int) -> None:
        self.registrations.pop(identifier, None)


class _Win32Backend:
    def __init__(self):
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                              wintypes.UINT, wintypes.UINT]
        self.user32.RegisterHotKey.restype = wintypes.BOOL
        self.user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.UnregisterHotKey.restype = wintypes.BOOL
        self.last_error = 0

    def register(self, identifier: int, modifiers: int, virtual_key: int) -> bool:
        ctypes.set_last_error(0)
        result = bool(self.user32.RegisterHotKey(None, identifier, modifiers, virtual_key))
        self.last_error = ctypes.get_last_error() if not result else 0
        return result

    def unregister(self, identifier: int) -> None:
        self.user32.UnregisterHotKey(None, identifier)


class _Message(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD), ("pt", wintypes.POINT),
                ("lPrivate", wintypes.DWORD)]


class _NativeFilter(QAbstractNativeEventFilter):
    def __init__(self, owner: "HotkeyManager"):
        super().__init__()
        self.owner = owner

    def nativeEventFilter(self, event_type, pointer):
        if bytes(event_type) not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            return False, 0
        try:
            address = int(pointer)
            if not address:
                return False, 0
            message = _Message.from_address(address)
            if message.message == WM_HOTKEY and self.owner._dispatch(int(message.wParam)):
                return True, 0
        except (TypeError, ValueError, OSError):
            pass
        return False, 0


class HotkeyManager:
    """Global shelf/overview shortcuts. apply() returns None or a Chinese error.

    validate() checks syntax, duplication and availability using temporary IDs,
    skipping combinations already owned by this manager. apply() registers all
    new combinations with fresh IDs. Existing
    combinations are reused, including when their shelf/overview roles swap.
    If any new combination conflicts, only new candidates are removed, leaving
    every prior registration and callback intact.
    """
    _next_identifier = 0x5A10

    def __init__(self, app: QApplication, on_shelf: Callable[[], None],
                 on_overview: Callable[[], None], backend=None):
        self.app = app
        self.callbacks = {"shelf": on_shelf, "overview": on_overview}
        self._registrations: dict[int, tuple[Shortcut, str]] = {}
        self._offscreen = QApplication.platformName().lower() in ("offscreen", "minimal")
        if backend is not None:
            self.backend = backend
        elif sys.platform == 'darwin' and not self._offscreen:
            from platform_native import MacHotkeyBackend
            self.backend = MacHotkeyBackend(self._dispatch)
        else:
            self.backend = _Win32Backend() if sys.platform == 'win32' and not self._offscreen else _NoopBackend()
        self.event_filter = _NativeFilter(self)
        self._filter_installed = sys.platform == "win32" and not self._offscreen
        if self._filter_installed:
            self.app.installNativeEventFilter(self.event_filter)
        self.shelf_shortcut = ""
        self.overview_shortcut = ""

    def validate(self, shelf_shortcut: str, overview_shortcut: str) -> str | None:
        temporary = []
        try:
            shelf = parse_shortcut(shelf_shortcut)
            overview = parse_shortcut(overview_shortcut)
            if shelf.identity == overview.identity:
                return "显示搁架和展开总览不能使用相同快捷键"
            owned = {item[0].identity for item in self._registrations.values()}
            for shortcut in (shelf, overview):
                if shortcut.identity in owned:
                    continue
                identifier = self._allocate_identifier()
                if not self.backend.register(identifier, shortcut.modifiers | MOD_NOREPEAT,
                                             shortcut.virtual_key):
                    error = getattr(self.backend, "last_error", 0)
                    reason = "已被其他程序占用" if error == 1409 else "无法注册，请换一组按键"
                    return f"快捷键 {shortcut.text} {reason}"
                temporary.append(identifier)
        except (ValueError, TypeError, RuntimeError, OSError) as error:
            return str(error)
        finally:
            for identifier in temporary:
                self.backend.unregister(identifier)
        return None

    @classmethod
    def _allocate_identifier(cls) -> int:
        identifier = cls._next_identifier
        if identifier > 0xBFFF:
            raise RuntimeError("快捷键标识已耗尽，请重新启动 FlickDock")
        cls._next_identifier += 1
        return identifier

    def apply(self, shelf_shortcut: str, overview_shortcut: str) -> str | None:
        problem = self.validate(shelf_shortcut, overview_shortcut)
        if problem:
            return problem
        desired = [(parse_shortcut(shelf_shortcut), "shelf"),
                   (parse_shortcut(overview_shortcut), "overview")]
        prior = {shortcut.identity: (identifier, shortcut, role)
                 for identifier, (shortcut, role) in self._registrations.items()}
        candidate: dict[int, tuple[Shortcut, str]] = {}
        newly_registered = []
        try:
            for shortcut, role in desired:
                previous = prior.get(shortcut.identity)
                if previous:
                    identifier = previous[0]
                else:
                    identifier = self._allocate_identifier()
                    if not self.backend.register(identifier, shortcut.modifiers | MOD_NOREPEAT,
                                                 shortcut.virtual_key):
                        error = getattr(self.backend, "last_error", 0)
                        reason = "已被其他程序占用" if error == 1409 else "无法注册，请换一组按键"
                        raise RuntimeError(f"快捷键 {shortcut.text} {reason}")
                    newly_registered.append(identifier)
                candidate[identifier] = (shortcut, role)
        except (RuntimeError, OSError) as error:
            for identifier in newly_registered:
                self.backend.unregister(identifier)
            return str(error)
        for identifier in self._registrations:
            if identifier not in candidate:
                self.backend.unregister(identifier)
        self._registrations = candidate
        self.shelf_shortcut, self.overview_shortcut = desired[0][0].text, desired[1][0].text
        return None

    def _dispatch(self, identifier: int) -> bool:
        item = self._registrations.get(identifier)
        if item is None:
            return False
        self.callbacks[item[1]]()
        return True

    def close(self):
        for identifier in list(self._registrations):
            self.backend.unregister(identifier)
        self._registrations.clear()
        if self._filter_installed:
            self.app.removeNativeEventFilter(self.event_filter)
            self._filter_installed = False
        close = getattr(self.backend, 'close', None)
        if close is not None:
            close()
