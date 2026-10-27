"""Isolated Cocoa ordering contract; never opens a real desktop window."""
import sys
import types
import unittest
from unittest.mock import patch

import platform_native


class _Window:
    def __init__(self):
        self.orders = 0
        self._level = 0
        self._behavior = 4

    def orderFrontRegardless(self):
        self.orders += 1

    def level(self):
        return self._level

    def collectionBehavior(self):
        return self._behavior

    def setLevel_(self, level):
        self._level = level

    def setCollectionBehavior_(self, behavior):
        self._behavior = behavior


class _View:
    def __init__(self, window):
        self._window = window

    def window(self):
        return self._window


class MacWindowOrderTests(unittest.TestCase):
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
