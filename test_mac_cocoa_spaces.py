"""CI-only Cocoa probe for the conflicting macOS Spaces collection behaviors.

Run with a native macOS Python/PySide6/PyObjC environment, QT_QPA_PLATFORM=cocoa,
and a disposable HOME. It opens empty ordering fixtures and one actual shelf
with generated text; no manager, global hooks, login item or network is used.
"""
from __future__ import annotations

import shelf_ui_common
import shelf_window
import argparse
import ctypes
import json
import os
from pathlib import Path
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    arguments = parser.parse_args()
    if (sys.platform != 'darwin' or os.environ.get('QT_QPA_PLATFORM') != 'cocoa'
            or os.environ.get('FLICKDOCK_CI_COCOA_SMOKE') != '1'):
        raise RuntimeError('Run this probe on a macOS Cocoa CI runner.')

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QWidget
    import AppKit
    import objc

    # Import the checkout under test rather than an installed FlickDock copy.
    sys.path.insert(0, str(Path.cwd()))
    from platform_native import (configure_media_backend, lower_shelf_after_reveal, raise_edge_target,
                                 raise_shelf_for_reveal, shelf_window_flags)
    configure_media_backend(platform='darwin')

    workspace=AppKit.NSWorkspace.sharedWorkspace()
    external=workspace.frontmostApplication()
    if external is None or int(external.processIdentifier())==os.getpid():
        candidates=AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_('com.apple.finder')
        external=next((candidate for candidate in candidates if int(candidate.processIdentifier())!=os.getpid()),None)
    if external is None:raise AssertionError('Cocoa probe requires an existing external foreground application')
    external_pid=int(external.processIdentifier())
    notifications=[];timeline=[]
    def front_pid():
        front=workspace.frontmostApplication()
        return int(front.processIdentifier()) if front is not None else -1
    center=AppKit.NSNotificationCenter.defaultCenter()
    observers=[]
    for notification,event in ((AppKit.NSApplicationDidFinishLaunchingNotification,'did_finish_launch'),
                               (AppKit.NSApplicationDidBecomeActiveNotification,'did_become_active')):
        observers.append(center.addObserverForName_object_queue_usingBlock_(notification,None,None,
            lambda _notice,event=event:notifications.append(dict(event=event,time=time.monotonic(),front_pid=front_pid()))))
    application = QApplication(['FlickDock-Cocoa-Spaces-probe'])
    application.setQuitOnLastWindowClosed(False)
    def snapshot(label):
        value=dict(phase=label,active=bool(AppKit.NSApplication.sharedApplication().isActive()),
                   front_pid=front_pid(),activation_notifications=sum(n['event']=='did_become_active' for n in notifications))
        timeline.append(value);print(json.dumps(value),flush=True);return value
    def pump_until(predicate):
        deadline=time.monotonic()+8
        while not predicate() and time.monotonic()<deadline:
            application.processEvents();time.sleep(.005)
        if not predicate():raise AssertionError('Cocoa readiness deadline: '+json.dumps(snapshot('deadline')))
    def control_pump(duration=.3):
        deadline=time.monotonic()+duration
        while time.monotonic()<deadline:application.processEvents();time.sleep(.005)
    # Qt's didFinishLaunching can activate the process independently of reveal.
    # Observe its completion, then measure from a real external-app baseline.
    pump_until(lambda:any(n['event']=='did_finish_launch' for n in notifications))
    snapshot('launch_notification')
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
    results = [];widgets=[]
    for name, flags, raise_window in cases:
        widget = QWidget()
        widget.setWindowFlags(flags)
        widget.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        if name == 'edge':
            widget.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)
            widget.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        widget.resize(80, 60)
        widgets.append((name,widget,raise_window))
        # Construct the native window while still hidden, as at app startup.
        widget.winId()
    # A bare Python process need not become active until it has shown a
    # regular window. Exercise one explicit opening as setup, then hide it;
    # the measured operations below still begin hidden and never reactivate.
    warmup=widgets[-1][1]
    warmup.show();warmup.activateWindow()
    AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    pump_until(lambda:bool(AppKit.NSApplication.sharedApplication().isActive()))
    snapshot('explicit_opening_completed')
    warmup.hide();control_pump(.1)
    external.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
    pump_until(lambda:front_pid()==external_pid and not AppKit.NSApplication.sharedApplication().isActive())
    baseline=snapshot('external_baseline')
    control_pump()
    control=snapshot('control_without_reveal')
    if control!=dict(baseline,phase='control_without_reveal'):
        raise AssertionError('External foreground was not stable before reveal')
    for name, widget, raise_window in widgets:
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
            before=snapshot(name+'_hidden')
            if before['active'] or before['front_pid']!=external_pid:
                raise AssertionError(f'{name}: inactive external foreground baseline was lost')
            widget.show()
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
            control_pump()
            after=snapshot(name+'_shown_and_raised')
            if (after['active'] or after['front_pid']!=external_pid
                    or after['activation_notifications']!=baseline['activation_notifications']):
                raise AssertionError(f'{name}: passive show/reveal changed foreground or emitted activation: {after}')
            if name == 'shelf':
                if not lower_shelf_after_reveal(widget, platform='darwin'):
                    raise AssertionError('shelf: native level did not restore')
                restored = int(window.collectionBehavior())
                if restored != staged:
                    raise AssertionError(f'shelf: original behavior not restored: {restored:#x}')
            results.append({'window': name, 'staged_behavior': staged,
                            'reveal_completed': True, 'final_behavior': active,
                            'active_before':before['active'],'active_after':after['active'],
                            'external_front_pid':external_pid,'front_pid_after':after['front_pid'],
                            'activation_notifications':after['activation_notifications']-baseline['activation_notifications'],
                            'hidden_show_verified':True,
                            'qt_raise_process':os.environ.get('QT_MAC_SET_RAISE_PROCESS')})
        finally:
            widget.close()
    # Prove that the notification/PID assertions detect a real activation.
    negative=widgets[-1][1]
    negative.show();negative.activateWindow()
    AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    pump_until(lambda:bool(AppKit.NSApplication.sharedApplication().isActive())
               and sum(n['event']=='did_become_active' for n in notifications)>baseline['activation_notifications'])
    snapshot('deliberate_activation_detected')
    negative.close()
    external.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
    for observer in observers:center.removeObserver_(observer)
    # Exercise the product's real tree and native input map. Empty QWidget
    # ordering probes cannot detect a duplicated shelf or a stale Cocoa mask.
    import tempfile
    from types import SimpleNamespace
    from unittest.mock import patch
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest
    import flickdock
    from app_settings import SettingsStore
    shelf_ui_common.load_font(application)
    with tempfile.TemporaryDirectory(prefix='flickdock-cocoa-shelf-') as directory:
        manager=SimpleNamespace(motion=True,settings=SettingsStore(Path(directory)),save=lambda:None,
            update_handles=lambda:None,activate_shelf=lambda _:None,_quitting=False,outgoing_drag=False)
        shelf=shelf_window.ShelfWindow(manager,1);manager.shelf=shelf;shelf.pinned=True
        ref=shelf.content.add_text('Cocoa native click target');shelf.files['permanent']=[ref];shelf.refresh()
        area=application.primaryScreen().availableGeometry()
        def shelf_window():return objc.objc_object(c_void_p=ctypes.c_void_p(int(shelf.winId()))).window()
        def settle_shelf():pump_until(lambda:not shelf.geometry_morph.active and not shelf.presentation._timer.isActive())
        def hit_target(button):
            point=button.mapToGlobal(button.rect().center());hit=application.widgetAt(point)
            if hit is not button and not (hit is not None and button.isAncestorOf(hit)):
                raise AssertionError('Cocoa input mismatch for '+button.accessibleName()+': '+str(type(hit)))
        try:
            with patch('native_ui_common.reduced_motion',return_value=False):
                shelf._show_at(QPoint(area.right()-359,area.top()+4),'manual',QPoint());settle_shelf()
                number=shelf_window().windowNumber();canvas=shelf.geometry()
                hit_target(shelf.expand_button)
                shelf.show_contents();settle_shelf();application.processEvents()
                if shelf_window().windowNumber()!=number or shelf.geometry()!=canvas:
                    raise AssertionError('Cocoa expansion replaced or rebased the native shelf')
                visible=[window for window in AppKit.NSApplication.sharedApplication().windows()
                         if window.isVisible() and str(window.title())==shelf.windowTitle()]
                if len(visible)!=1:raise AssertionError('Multiple native shelf windows are visible: '+str(len(visible)))
                if shelf.popup.preview.current_path is not None:raise AssertionError('Cocoa overview opened an unselected file')
                hit_target(shelf.popup.select_all_button);hit_target(shelf.popup.search_edit)
                QTest.mouseClick(shelf.popup.select_all_button,Qt.LeftButton);application.processEvents()
                if ref not in shelf.popup.selected:raise AssertionError('Cocoa overview selection button ignored the click')
                if shelf.popup.preview.current_path!=ref:raise AssertionError('Cocoa selected file did not load its preview')
                shelf.collapse_contents();settle_shelf();hit_target(shelf.lan_button);hit_target(shelf.expand_button)
                if shelf_window().windowNumber()!=number:raise AssertionError('Cocoa collapse replaced the shelf window')
                shelf.show_contents();settle_shelf()
                if shelf.popup.preview.current_path is not None:raise AssertionError('Cocoa overview restored stale selected content')
                shelf_interactions=dict(unique_native_shelf=True,stable_native_canvas=True,overview_input=True,compact_input=True)
        finally:
            shelf.presentation.stop();shelf.geometry_morph.stop();shelf.hide_immediately()
            shelf.resolver.close();shelf.image_cache.close();shelf.file_metadata.close();shelf.deleteLater();application.processEvents()
    application.quit()
    result = json.dumps({'ok': True, 'qt_platform': application.platformName(),
                         'cases': results,'startup_timeline':timeline,'notifications':notifications,
                         'forced_activation_detected':True,'shelf_interactions':shelf_interactions}, ensure_ascii=False)
    if arguments.output is not None:
        arguments.output.write_text(result, encoding='utf-8')
    print(result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
