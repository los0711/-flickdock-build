"""Mock Win32 ordering contract; never changes real desktop windows."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import platform_native


class _SetWindowPos:
    def __init__(self, result=1):
        self.result = result
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


class WindowsWindowOrderTests(unittest.TestCase):
    def test_reveal_is_temporarily_topmost_and_restored_without_activation_or_resize(self):
        set_position = _SetWindowPos()
        api = SimpleNamespace(SetWindowPos=set_position)
        widget = SimpleNamespace(winId=lambda: 12345)
        with patch.object(platform_native, 'native_input_available', return_value=True), \
             patch.object(platform_native.ctypes, 'WinDLL', return_value=api):
            self.assertTrue(platform_native.raise_shelf_for_reveal(widget, platform='win32'))
            self.assertTrue(platform_native.lower_shelf_after_reveal(widget, platform='win32'))
            self.assertTrue(platform_native.raise_edge_target(widget, platform='win32'))
        self.assertEqual(set_position.calls, [(12345, -1, 0, 0, 0, 0, 0x0213),
                                              (12345, -2, 0, 0, 0, 0, 0x0213),
                                              (12345, -1, 0, 0, 0, 0, 0x0213)])

    def test_native_api_unavailable_or_failed_does_not_claim_success(self):
        widget = SimpleNamespace(winId=lambda: 12345)
        with patch.object(platform_native, 'native_input_available', return_value=False), \
             patch.object(platform_native.ctypes, 'WinDLL') as dll:
            self.assertFalse(platform_native.raise_shelf_for_reveal(widget, platform='win32'))
            dll.assert_not_called()
        set_position = _SetWindowPos(result=0)
        with patch.object(platform_native, 'native_input_available', return_value=True), \
             patch.object(platform_native.ctypes, 'WinDLL', return_value=SimpleNamespace(SetWindowPos=set_position)):
            self.assertFalse(platform_native.raise_shelf_for_reveal(widget, platform='win32'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
