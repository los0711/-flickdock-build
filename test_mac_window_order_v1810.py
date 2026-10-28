"""Isolated Cocoa ordering contract; never opens a real desktop window."""
import sys
import types
import unittest
from unittest.mock import patch

import platform_native


class _Window:
    def __init__(self, behavior=4, reject_conflict=False):
        self.orders = 0
        self._level = 0
        self._behavior = behavior
        self.reject_conflict = reject_conflict

    def orderFrontRegardless(self):
        self.orders += 1

    def level(self):
        return self._level

    def collectionBehavior(self):
        return self._behavior

    def setLevel_(self, level):
        self._level = level

    def setCollectionBehavior_(self, behavior):
        if self.reject_conflict and behavior & 1 and behavior & 2:
            raise RuntimeError('window behavior cannot be both CanJoinAllSpaces and MoveToActiveSpace')
        self._behavior = behavior


class _View:
    def __init__(self, window):
        self._window = window

    def window(self):
        return self._window


class MacWindowOrderTests(unittest.TestCase):
    def test_edge_replaces_move_to_active_space_when_joining_all_spaces(self):
        window = _Window(behavior=2 | 4, reject_conflict=True)
        fake_objc = types.SimpleNamespace(objc_object=lambda **_kwargs: _View(window))
        fake_appkit = types.SimpleNamespace(NSFloatingWindowLevel=3,
            NSWindowCollectionBehaviorCanJoinAllSpaces=1 << 0,
            NSWindowCollectionBehaviorMoveToActiveSpace=1 << 1,
            NSWindowCollectionBehaviorFullScreenAuxiliary=1 << 8,
            NSWindowCollectionBehaviorCanJoinAllApplications=1 << 12)
        widget = types.SimpleNamespace(winId=lambda: 123)
        with patch.dict(sys.modules, {'objc': fake_objc, 'AppKit': fake_appkit}), \
             patch.object(platform_native, 'native_input_available', return_value=True):
            self.assertTrue(platform_native.raise_edge_target(widget, platform='darwin'))
            first_behavior = window.collectionBehavior()
            self.assertTrue(platform_native.raise_edge_target(widget, platform='darwin'))
            self.assertEqual(window.orders, 2)
            self.assertEqual(window.collectionBehavior(), first_behavior)
            self.assertEqual(window.collectionBehavior() & 2, 0)
            self.assertEqual(window.collectionBehavior() & (1 << 12), 0)
            self.assertEqual(window.collectionBehavior() & (1 | 4 | (1 << 8)), 1 | 4 | (1 << 8))

    def test_reveal_replaces_move_then_restores_original_behavior(self):
        original = 2 | 4
        window = _Window(behavior=original, reject_conflict=True)
        fake_objc = types.SimpleNamespace(objc_object=lambda **_kwargs: _View(window))
        fake_appkit = types.SimpleNamespace(NSFloatingWindowLevel=3,
            NSWindowCollectionBehaviorCanJoinAllSpaces=1 << 0,
            NSWindowCollectionBehaviorMoveToActiveSpace=1 << 1,
            NSWindowCollectionBehaviorFullScreenAuxiliary=1 << 8,
            NSWindowCollectionBehaviorCanJoinAllApplications=1 << 12)
        widget = types.SimpleNamespace(winId=lambda: 123)
        with patch.dict(sys.modules, {'objc': fake_objc, 'AppKit': fake_appkit}), \
             patch.object(platform_native, 'native_input_available', return_value=True):
            self.assertTrue(platform_native.raise_shelf_for_reveal(widget, platform='darwin'))
            self.assertEqual(window.collectionBehavior() & 2, 0)
            self.assertEqual(window.collectionBehavior() & (1 << 12), 0)
            self.assertTrue(platform_native.raise_shelf_for_reveal(widget, platform='darwin'))
            self.assertEqual(widget._flickdock_native_order, (0, original))
            self.assertTrue(platform_native.lower_shelf_after_reveal(widget, platform='darwin'))
            self.assertEqual(window.collectionBehavior(), original)
            self.assertEqual(window.level(), 0)
            self.assertIsNone(widget._flickdock_native_order)

    def test_restore_cocoa_error_keeps_order_for_a_retry(self):
        class CocoaError(Exception):
            pass

        class RestoreWindow(_Window):
            fail_restore = True

            def setCollectionBehavior_(self, behavior):
                if behavior == 2 | 4 and self.fail_restore:
                    raise CocoaError('Cocoa temporarily refused to restore')
                super().setCollectionBehavior_(behavior)

        window = RestoreWindow(behavior=1 | 4 | (1 << 8))
        fake_objc = types.SimpleNamespace(error=CocoaError,
            objc_object=lambda **_kwargs: _View(window))
        widget = types.SimpleNamespace(winId=lambda: 123, _flickdock_native_order=(0, 2 | 4))
        with patch.dict(sys.modules, {'objc': fake_objc}), \
             patch.object(platform_native, 'native_input_available', return_value=True):
            self.assertFalse(platform_native.lower_shelf_after_reveal(widget, platform='darwin'))
            self.assertEqual(widget._flickdock_native_order, (0, 2 | 4))
            window.fail_restore = False
            self.assertTrue(platform_native.lower_shelf_after_reveal(widget, platform='darwin'))
            self.assertEqual(window.collectionBehavior(), 2 | 4)
            self.assertIsNone(widget._flickdock_native_order)

    def test_unexpected_error_is_not_silenced_by_native_fallback(self):
        class FailingWindow(_Window):
            def orderFrontRegardless(self):
                raise KeyError('implementation error')

        fake_appkit = types.SimpleNamespace(NSFloatingWindowLevel=3,
            NSWindowCollectionBehaviorCanJoinAllSpaces=1 << 0,
            NSWindowCollectionBehaviorMoveToActiveSpace=1 << 1,
            NSWindowCollectionBehaviorFullScreenAuxiliary=1 << 8)
        for raise_window in (platform_native.raise_edge_target, platform_native.raise_shelf_for_reveal):
            with self.subTest(raise_window=raise_window.__name__):
                window = FailingWindow(behavior=2 | 4)
                fake_objc = types.SimpleNamespace(objc_object=lambda **_kwargs: _View(window))
                widget = types.SimpleNamespace(winId=lambda: 123)
                with patch.dict(sys.modules, {'objc': fake_objc, 'AppKit': fake_appkit}), \
                     patch.object(platform_native, 'native_input_available', return_value=True):
                    with self.assertRaises(KeyError):
                        raise_window(widget, platform='darwin')
                    self.assertEqual((window.level(), window.collectionBehavior()), (0, 2 | 4))

    def test_cocoa_error_safely_restores_partial_window_changes(self):
        class CocoaError(Exception):
            pass

        class FailingWindow(_Window):
            def orderFrontRegardless(self):
                raise CocoaError('Cocoa refused to order the window')

        fake_appkit = types.SimpleNamespace(NSFloatingWindowLevel=3,
            NSWindowCollectionBehaviorCanJoinAllSpaces=1 << 0,
            NSWindowCollectionBehaviorMoveToActiveSpace=1 << 1,
            NSWindowCollectionBehaviorFullScreenAuxiliary=1 << 8)
        for raise_window in (platform_native.raise_edge_target, platform_native.raise_shelf_for_reveal):
            with self.subTest(raise_window=raise_window.__name__):
                original = 2 | 4
                window = FailingWindow(behavior=original)
                fake_objc = types.SimpleNamespace(error=CocoaError,
                    objc_object=lambda **_kwargs: _View(window))
                widget = types.SimpleNamespace(winId=lambda: 123)
                with patch.dict(sys.modules, {'objc': fake_objc, 'AppKit': fake_appkit}), \
                     patch.object(platform_native, 'native_input_available', return_value=True):
                    self.assertFalse(raise_window(widget, platform='darwin'))
                    self.assertEqual(window.level(), 0)
                    self.assertEqual(window.collectionBehavior(), original)
                    if raise_window is platform_native.raise_shelf_for_reveal:
                        self.assertIsNone(widget._flickdock_native_order)

    def test_reveal_joins_fullscreen_space_then_restores_original_native_level(self):
        window = _Window()
        fake_objc = types.SimpleNamespace(objc_object=lambda **_kwargs: _View(window))
        fake_appkit = types.SimpleNamespace(NSFloatingWindowLevel=3,
            NSWindowCollectionBehaviorCanJoinAllSpaces=1 << 0,
            NSWindowCollectionBehaviorFullScreenAuxiliary=1 << 8)
        widget = types.SimpleNamespace(winId=lambda: 123)
        with patch.dict(sys.modules, {'objc': fake_objc, 'AppKit': fake_appkit}), \
             patch.object(platform_native, 'native_input_available', return_value=True):
            self.assertTrue(platform_native.raise_shelf_for_reveal(widget, platform='darwin'))
            self.assertEqual(window.orders, 1)
            self.assertEqual(window.level(), 3)
            self.assertTrue(window.collectionBehavior() & (1 << 8))
            self.assertTrue(platform_native.lower_shelf_after_reveal(widget, platform='darwin'))
            self.assertEqual((window.level(),window.collectionBehavior()),(0,4))
            self.assertTrue(platform_native.raise_edge_target(widget, platform='darwin'))
            self.assertEqual(window.orders, 2)
            self.assertEqual(window.level(), 3)

    def test_unavailable_cocoa_window_has_no_desktop_side_effect(self):
        widget = types.SimpleNamespace(winId=lambda: 123)
        with patch.object(platform_native, 'native_input_available', return_value=False):
            self.assertFalse(platform_native.raise_shelf_for_reveal(widget, platform='darwin'))
        fake_objc = types.SimpleNamespace(objc_object=lambda **_kwargs: _View(None))
        with patch.dict(sys.modules, {'objc': fake_objc}), \
             patch.object(platform_native, 'native_input_available', return_value=True):
            self.assertFalse(platform_native.raise_shelf_for_reveal(widget, platform='darwin'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
