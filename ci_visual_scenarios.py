"""Measure changed pixels in the final display during real FlickDock actions."""
import argparse
import faulthandler
from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

if os.environ.get('GITHUB_ACTIONS') != 'true':
    raise SystemExit('Requires a disposable CI desktop')
os.environ['QT_QPA_PLATFORM'] = 'windows' if sys.platform == 'win32' else 'cocoa'

from PySide6.QtCore import QEventLoop, QPoint, QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QCursor, QWheelEvent
from PySide6.QtWidgets import QApplication, QWidget


def pump(milliseconds):
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


def wait_for(predicate, message, seconds=5):
    end = time.perf_counter() + seconds
    while not predicate() and time.perf_counter() < end:
        pump(4)
    if not predicate():
        raise RuntimeError(message)


def measure(binary, target, screen, action, duration, *, action_slice=None):
    bounds = screen.geometry()
    target = bounds.intersected(target)
    if target.width() < 2 or target.height() < 2:
        raise RuntimeError(f'The captured content left the screen: {target}')
    if sys.platform == 'win32':
        target.translate(-bounds.topLeft())
        ratio = screen.devicePixelRatio()
        coordinates = [round(value * ratio) for value in target.getRect()]
    else:
        coordinates = list(target.getRect())
    measurement = subprocess.Popen([str(binary), *map(str, coordinates), str(duration + 1.25)],
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ready = threading.Event()
    frames = []

    def receive():
        for line in measurement.stdout:
            if line.strip() == 'READY':
                ready.set()
            else:
                timestamp, digest, accumulated = line.split()
                frames.append((float(timestamp), digest, int(accumulated)))

    reader = threading.Thread(target=receive, name='desktop-samples', daemon=True)
    reader.start()
    try:
        wait_for(lambda:ready.is_set() or measurement.poll() is not None, 'Desktop capture never started')
        if not ready.is_set():
            raise RuntimeError(measurement.stderr.read())
        # Keep a pre-action sample in the trace to identify the first visibly
        # changed desktop image, even if the action blocks the Qt event loop.
        pump(240)
        started = time.perf_counter()
        action()
        action_return = time.perf_counter()
        remaining = max(0, round((started + duration - action_return) * 1000))
        pump(remaining)
        wait_for(lambda:measurement.poll() is not None, 'Desktop capture did not exit')
        reader.join(timeout=2)
        if measurement.returncode != 0:
            raise RuntimeError(measurement.stderr.read())
    finally:
        if measurement.poll() is None:
            measurement.kill()
            measurement.wait(timeout=2)
    seen = [frame for frame in frames if frame[0] <= started + duration]
    before = [frame for frame in seen if frame[0] < started]
    if not before:
        raise RuntimeError(f'No pre-action display sample; Python clock {started}; sample clock {frames[:2]}')
    previous = before[-1][1]
    changes = []
    for stamp, digest, _ in seen:
        if stamp < started:
            continue
        if digest != previous:
            changes.append(round((stamp-started)*1000, 3))
        previous = digest
    if not changes:
        raise RuntimeError('The final display did not change in the captured area')
    window = action_slice or (0., duration)
    active = [value for value in changes if window[0]*1000 <= value <= window[1]*1000]
    intervals = sorted(b-a for a, b in zip(active, active[1:]))
    period = 1000/screen.refreshRate()
    return {
        'capture_region': coordinates,
        'input_to_visible_ms': changes[0],
        'action_call_ms': (action_return-started)*1000,
        'active_window_ms': [part*1000 for part in window],
        'unique_frames_active': len(active),
        'region_changes_ms': changes,
        'interval_median_ms': statistics.median(intervals) if intervals else None,
        'interval_p95_ms': intervals[math.ceil(len(intervals)*.95)-1] if intervals else None,
        'intervals_over_1_5_display_periods': sum(dt>period*1.5 for dt in intervals),
        'backlogged_capture_frames': sum(max(0, count-1) for _, _, count in seen),
        'all_region_samples': [(round((stamp-started)*1000, 3), digest, n) for stamp,digest,n in seen],
    }


def observe_scroll(area, duration, *, interval=85):
    at = QPointF(area.viewport().rect().center())
    at_global = QPointF(area.viewport().mapToGlobal(at.toPoint()))
    timer = QTimer(area)
    timer.setTimerType(Qt.PreciseTimer)
    def wheel():
        event = QWheelEvent(at, at_global, QPoint(), QPoint(0,-120),
                            Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
        QApplication.sendEvent(area.viewport(), event)
    timer.timeout.connect(wheel)
    wheel()
    timer.start(interval)
    QTimer.singleShot(duration, timer.deleteLater)


def band(widget, x, y, width=88, height=160):
    rect = widget.rect().intersected(QRect(x,y,width,height))
    rect.moveTopLeft(widget.mapToGlobal(rect.topLeft()))
    return rect


def main():
    faulthandler.dump_traceback_later(45, exit=True)
    parser = argparse.ArgumentParser()
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parent)
    options = parser.parse_args()
    sys.path.insert(0, str(options.source.resolve()))
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    screen = app.primaryScreen()
    from shelf_ui_common import load_font
    load_font(app)
    import flickdock
    from app_settings import SettingsStore
    from settings_navigation import install_smooth_scrolling
    from translator_panel import TranslatorDialog
    from translator_store import TranslatorStore
    from test_translator_v1844 import FakeApi
    from app_version import APP_VERSION
    install_smooth_scrolling(app)
    report = dict(version=APP_VERSION, platform=app.platformName(), refresh_hz=screen.refreshRate(),
                  display=screen.geometry().getRect(), dpr=screen.devicePixelRatio(), physical_panel=False,
                  capture='DXGI Desktop Duplication' if sys.platform=='win32' else 'ScreenCaptureKit displayTime')
    with tempfile.TemporaryDirectory(prefix='flickdock-desktop-motion-') as folder, ExitStack() as cleanup:
        base = Path(folder)
        flickdock.BASE = base / 'profile'
        flickdock.STATE_FILE = flickdock.BASE / 'shelves.json'
        flickdock.OLD_STATE_FILE = flickdock.BASE / 'old-shelves.json'
        manager = flickdock.ShelfManager(app, cocoa_smoke=True)
        cleanup.callback(manager.quit)
        print('Opened isolated desktop session',flush=True)
        shelf = manager.shelf
        manager.timer.stop()
        manager.motion = shelf.presentation.motion = shelf.geometry_morph.motion = True
        manager.settings.update(theme='light', capture_mode='reference', view_mode='list',
                                clipboard_sync_enabled=False, sync_upload_enabled=False, sync_receive_enabled=False)
        refs = []
        for index in range(151):
            path = base / f'Document {index:03}.txt'
            text = ('%04x fresh disposable document for capture. ' % (index*77)) * (1 + index % 12)
            path.write_text('\n'.join(f'{line} {text}' for line in range(180)), encoding='utf-8')
            refs.append(str(path))
        shelf.add_paths('permanent', refs)
        wait_for(lambda:not shelf.copy_jobs, 'File intake did not settle')
        shelf.hide_immediately()
        # The device pointer belongs to this disposable runner. Its position
        # gives the real shortcut a deterministic overview destination.
        QCursor.setPos(screen.availableGeometry().center())
        pump(100)
        area = screen.availableGeometry()
        origin = QPoint(area.left()+60, area.top()+115)
        cold_patch = QRect(origin, QRect(0,0,130,170).size())
        print('Measuring first overview appearance',flush=True)
        report['cold_overview'] = measure(options.capture, cold_patch, screen, manager.show_overview, .70,
                                          action_slice=(.035,.29))
        popup = shelf.popup
        wait_for(lambda:popup.isVisible() and not shelf.content_transition_active()
                 and not shelf.presentation._timer.isActive(), 'Overview did not settle')
        print('Measuring scrolling the overview',flush=True)
        report['overview_scroll'] = measure(options.capture, band(popup.scroll.viewport(),68,76), screen,
            lambda:observe_scroll(popup.scroll,1100), 1.3, action_slice=(.12,1.12))
        popup.scroll.verticalScrollBar().setValue(0)
        visible = popup.visible_refs()
        popup.select_item(visible[0])
        if popup._narrow_layout:
            popup.toggle_narrow_preview()
        pump(280)
        def next_file():
            popup.select_item(visible[13])
        print('Measuring file preview switch',flush=True)
        report['file_preview'] = measure(options.capture, band(popup.preview,14,12,196,222),
                                         screen, next_file, .62, action_slice=(0,.32))
        if popup.preview.current_path != visible[13]:
            raise RuntimeError('The preview did not preserve the newest selection')
        shelf.hide_immediately()
        settings = SettingsStore(base / 'translator')
        store = TranslatorStore(base / 'translator', autoload=False)
        store.data['live'] = False
        shim = SimpleNamespace(settings=settings, shelf=QWidget())
        translator = TranslatorDialog(shim,api=FakeApi(),store=store,detector=lambda text:'英语')
        cleanup.callback(translator.shutdown)
        translator.show()
        editor = translator.source_editor
        paragraphs = [' '.join(f'{n ^ (k*13):03x}-fixture' for k in range(n%15+23)) for n in range(300)]
        editor.setPlainText('\n'.join(paragraphs))
        editor.verticalScrollBar().setValue(0)
        pump(420)
        print('Measuring a long document in the translator',flush=True)
        report['long_text_scroll'] = measure(options.capture,band(editor.viewport(),28,68),screen,
            lambda:observe_scroll(editor,1100),1.3,action_slice=(.12,1.12))
        options.out.write_text(json.dumps(report,indent=2), encoding='utf-8')
        print(json.dumps({k:{field:case[field] for field in ('input_to_visible_ms','unique_frames_active',
               'interval_p95_ms','backlogged_capture_frames')} for k,case in report.items() if isinstance(case,dict)}))
    faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    main()
