"""Isolated Cocoa ordering contract; never opens a real desktop window."""
import sys
import types
import unittest
from unittest.mock import patch

import platform_native


class _Window:
    def __init__(self):
        self.orders = 0

    def orderFrontRegardless(self):
        self.orders += 1

    def setLevel_(self, _level):
        raise AssertionError('The revealed shelf must remain at normal window level')


class _View:
    def __init__(self, window):
        self._window = window

    def window(self):
        return self._window


class MacWindowOrderTests(unittest.TestCase):
    def test_reveal_orders_above_inactive_app_without_activation_or_floating_level(self):
        window = _Window()
        fake_objc = types.SimpleNamespace(objc_object=lambda **_kwargs: _View(window))
        widget = types.SimpleNamespace(winId=lambda: 123)
        with patch.dict(sys.modules, {'objc': fake_objc}), \
             patch.object(platform_native, 'native_input_available', return_value=True):
            self.assertTrue(platform_native.raise_shelf_for_reveal(widget, platform='darwin'))
            self.assertEqual(window.orders, 1)

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
