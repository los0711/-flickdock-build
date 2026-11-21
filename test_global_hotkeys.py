"""Hotkey registration and message dispatch with no OS key registration."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import ctypes
from PySide6.QtCore import QByteArray
from PySide6.QtWidgets import QApplication
from global_hotkeys import (HotkeyManager, parse_shortcut, _NoopBackend,
                            _Message, WM_HOTKEY, MOD_NOREPEAT)

app = QApplication([])


class FakeBackend(_NoopBackend):
    def __init__(self):
        super().__init__()
        self.blocked = set()
        self.register_calls = []
        self.removed = []
        self.race_pair = None
        self.race_attempts = 0

    def register(self, identifier, modifiers, key):
        self.register_calls.append((identifier, modifiers, key))
        if (modifiers & ~MOD_NOREPEAT, key) in self.blocked:
            self.last_error = 1409
            return False
        if (modifiers & ~MOD_NOREPEAT, key) == self.race_pair:
            self.race_attempts += 1
            if self.race_attempts == 2:
                self.last_error = 1409
                return False
        return super().register(identifier, modifiers, key)

    def unregister(self, identifier):
        self.removed.append(identifier)
        super().unregister(identifier)


events = []
backend = FakeBackend()
manager = HotkeyManager(app, lambda: events.append('shelf'), lambda: events.append('overview'), backend)
assert not manager._filter_installed
assert manager.validate('Ctrl+Alt+Space', 'Ctrl+Alt+Shift+Space') is None
assert not backend.registrations
assert manager.apply('Ctrl+Alt+Space', 'Ctrl+Alt+Shift+Space') is None
assert len(backend.registrations) == 2
assert all(modifiers & MOD_NOREPEAT for _, modifiers, _ in backend.register_calls)
print('PASS defaults use MOD_NOREPEAT; offscreen installs no native filter')

for invalid in ('', 'Meta+A', 'Ctrl+X, Ctrl+C', 'InvalidKey', 'Ctrl+F25'):
    assert manager.validate(invalid, 'Ctrl+Alt+Shift+Space')
assert manager.validate('Ctrl+Enter', 'Ctrl+Return')
assert parse_shortcut('Ctrl+Alt+F24').virtual_key == 0x87
assert parse_shortcut('Ctrl+Alt+9').virtual_key == ord('9')
assert parse_shortcut('Ctrl+Alt+Left').virtual_key == 0x25
print('PASS syntax, Meta rejection, duplicate aliases and VK mapping')

old = dict(backend.registrations)
calls = len(backend.register_calls)
assert manager.apply('Ctrl+Alt+Shift+Space', 'Ctrl+Alt+Space') is None
assert len(backend.register_calls) == calls and backend.registrations == old
for identifier, (_, role) in manager._registrations.items():
    manager._dispatch(identifier)
assert set(events) == {'shelf', 'overview'}
print('PASS unchanged combinations and role swaps reuse registrations')

old_map = dict(manager._registrations)
backend.blocked.add(parse_shortcut('Ctrl+Alt+B').identity)
error = manager.validate('Ctrl+Alt+A', 'Ctrl+Alt+B')
assert error and '占用' in error
assert backend.registrations == old
assert manager._registrations == old_map
backend.blocked.clear()
backend.race_pair = parse_shortcut('Ctrl+Alt+B').identity
error = manager.apply('Ctrl+Alt+A', 'Ctrl+Alt+B')
assert error and '占用' in error
assert backend.registrations == old and manager._registrations == old_map
print('PASS availability validation and post-validation conflict preserve old shortcuts')

identifier = next(iter(manager._registrations))
message = _Message()
message.message = WM_HOTKEY
message.wParam = identifier
before = len(events)
assert manager.event_filter.nativeEventFilter(QByteArray(b'windows_generic_MSG'), ctypes.addressof(message)) == (True, 0)
assert len(events) == before + 1
message.wParam = 0x7777
assert manager.event_filter.nativeEventFilter(QByteArray(b'windows_dispatcher_MSG'), ctypes.addressof(message)) == (False, 0)
assert manager.event_filter.nativeEventFilter(QByteArray(b'other'), ctypes.addressof(message)) == (False, 0)
print('PASS WM_HOTKEY pointer decoding and callback routing')

assert manager.apply('Ctrl+Alt+F5', 'Ctrl+Alt+F6') is None
assert len(backend.registrations) == 2 and not set(old).intersection(backend.registrations)
manager.close()
manager.close()
assert not backend.registrations and not manager._registrations
assert not manager._dispatch(identifier)
native_default = HotkeyManager(app, lambda: None, lambda: None)
assert isinstance(native_default.backend, _NoopBackend)
assert native_default.apply('Ctrl+Alt+Space', 'Ctrl+Alt+Shift+Space') is None
native_default.close()
print('PASS replace/close cleanup and mandatory offscreen no-op backend')
print('GLOBAL_HOTKEYS_OK')
