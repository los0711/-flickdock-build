"""CI-only Cocoa probe for the conflicting macOS Spaces collection behaviors.

Run with a native macOS Python/PySide6/PyObjC environment, QT_QPA_PLATFORM=cocoa,
and a disposable HOME. It opens only two empty test windows; no app instance,
global hotkeys, login item, network service, or user data is touched.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    arguments = parser.parse_args()
    if sys.platform != 'darwin' or os.environ.get('QT_QPA_PLATFORM') != 'cocoa':
        raise RuntimeError('Run this probe on a macOS Cocoa CI runner.')

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QWidget
    import AppKit
    import objc

    # Import the checkout under test rather than an installed FlickDock copy.
    sys.path.insert(0, str(Path.cwd()))
    from platform_native import (lower_shelf_after_reveal, raise_edge_target,
                                 raise_shelf_for_reveal, shelf_window_flags)

    application = QApplication(['FlickDock-Cocoa-Spaces-probe'])
    can_join = int(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces)
    move = int(AppKit.NSWindowCollectionBehaviorMoveToActiveSpace)
    ignores_cycle = int(AppKit.NSWindowCollectionBehaviorIgnoresCycle)
    staged = move | ignores_cycle
    cases = (
        ('edge', Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
         Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus,
         raise_edge_target),
        ('shelf', shelf_window_flags(platform='darwin'), raise_shelf_for_reveal),
    )
    results = []
    for name, flags, raise_window in cases:
        widget = QWidget()
        widget.setWindowFlags(flags)
        if name == 'edge':
            widget.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)
            widget.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        widget.resize(80, 60)
        widget.show()
        application.processEvents()
        try:
            view = objc.objc_object(c_void_p=ctypes.c_void_p(int(widget.winId())))
            window = view.window()
            if window is None:
                raise AssertionError(f'{name}: Qt did not create an NSWindow')
            # Force the behavior found on the affected Mac. CI's default
            # NSWindow does not necessarily set this bit, so simply launching
            # on macOS 15 cannot reproduce the user's macOS 27 exception.
            window.setCollectionBehavior_(staged)
            if not int(window.collectionBehavior()) & move:
                raise AssertionError(f'{name}: failed to stage MoveToActiveSpace')
            if not raise_window(widget, platform='darwin'):
                raise AssertionError(f'{name}: native reveal did not complete')
            active = int(window.collectionBehavior())
            if not active & can_join or active & move or not active & ignores_cycle:
                raise AssertionError(f'{name}: invalid Spaces behavior: {active:#x}')
            if not raise_window(widget, platform='darwin'):
                raise AssertionError(f'{name}: repeated native reveal did not complete')
            repeated = int(window.collectionBehavior())
            if repeated != active:
                raise AssertionError(f'{name}: repeated reveal changed behavior: {repeated:#x}')
            if name == 'shelf':
                if not lower_shelf_after_reveal(widget, platform='darwin'):
                    raise AssertionError('shelf: native level did not restore')
                restored = int(window.collectionBehavior())
                if restored != staged:
                    raise AssertionError(f'shelf: original behavior not restored: {restored:#x}')
            results.append({'window': name, 'staged_behavior': staged,
                            'reveal_completed': True, 'final_behavior': active})
        finally:
            widget.close()
    application.quit()
    result = json.dumps({'ok': True, 'qt_platform': application.platformName(),
                         'cases': results}, ensure_ascii=False)
    if arguments.output is not None:
        arguments.output.write_text(result, encoding='utf-8')
    print(result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
