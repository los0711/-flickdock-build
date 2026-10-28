"""Confirmation is a sibling layer excluded from card effect/snapshot caches.

Use --baseline before the fix to save/read the old cache-containment evidence.
All native input, tray, registry and resolver operations remain mocked.
"""
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['QT_SCALE_FACTOR'] = '1.25'

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QObject, QPoint, QRect, QTimer, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFrame, QLineEdit, QSystemTrayIcon, QWidget
from shiboken6 import isValid

from confirm_clear import ClearConfirmOverlay, confirmation_active
from surface_morph import SurfaceSnapshot


class Screen:
    def availableGeometry(self): return QRect(0, 0, 1920, 1080)
    def devicePixelRatio(self): return 1.25


class LocalResolver(QObject):
    def __init__(self, *args, parent=None, **kwargs): super().__init__(parent)
    @property
    def pending_refs(self): return frozenset()
    def cancel(self, ref): pass
    def cancel_all(self): pass
    def close(self): pass


def run_for(milliseconds):
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


def until(condition, seconds=8):
    deadline = time.perf_counter()+seconds
    while not condition() and time.perf_counter()<deadline: run_for(10)
    assert condition(), 'Offscreen lifecycle did not settle'


def red_pixels(pixmap):
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    pixels = bytes(image.constBits())
    return sum(r>170 and g<100 and b<125 and a>230
               for r,g,b,a in zip(pixels[0::4], pixels[1::4], pixels[2::4], pixels[3::4]))


baseline = '--baseline' in sys.argv
evidence_dir = os.getenv('FLICKDOCK_CLEAR_LIFECYCLE_RENDER_DIR')
if evidence_dir: Path(evidence_dir).mkdir(parents=True, exist_ok=True)

with tempfile.TemporaryDirectory(prefix='flickdock-clear-lifecycle-v163-') as temporary:
    os.environ['APPDATA'] = str(Path(temporary)/'profile')
    import flickdock as subject
    import app_settings
    app = QApplication([])
    subject.load_font(app)
    with contextlib.ExitStack() as patches:
        for owner, name, value in [
            (subject, 'motion_enabled', lambda: True),
            (subject, 'ContentResolver', LocalResolver),
            (subject.QCursor, 'pos', lambda *args: QPoint(1400, 350)),
            (subject.QGuiApplication, 'screenAt', lambda *args: Screen()),
            (subject.QGuiApplication, 'primaryScreen', lambda *args: Screen()),
            (subject.ctypes.windll.user32, 'GetAsyncKeyState', lambda key: 0),
            (subject, 'native_window_move_active', lambda: False),
            (app_settings, '_read_startup_value', lambda: None),
            (app_settings, '_write_startup_value', lambda *args: None),
            (QSystemTrayIcon, 'show', lambda self: None),
            (QSystemTrayIcon, 'hide', lambda self: None),
        ]: patches.enter_context(patch.object(owner, name, value))
        manager = subject.ShelfManager(app)
        manager.timer.stop()
        shelf = manager.shelf
        shelf.pinned = True
        originals = []
        for index in range(250):
            path = Path(temporary)/f'local-{index:03}.txt'
            path.write_text(f'Local original {index}', encoding='utf8')
            originals.append(str(path))
        evidence = []
        for name, count in [('rest', 125), ('appearing_spring', 125), ('bulk', 250), ('immediate_resize', 250)]:
            shelf.add_paths('permanent', originals[:count])
            manager.show_overview()
            popup = shelf.popup
            until(lambda: popup._build_cursor == len(popup.visible_refs()) and popup.retirement.is_idle and not shelf.geometry_morph.active)
            run_for(80)
            shelf.presentation.jump({'sx': .94 if name=='appearing_spring' else 1,
                'sy': .90 if name=='appearing_spring' else 1, 'alpha': 1, 'dx': 0, 'dy': 0})
            if name=='appearing_spring':
                shelf.presentation.animate({'sx': 1, 'sy': 1}, response=.4, damping=.7)
            # The reference UI has a red clear action in the underlying card.
            # Confirmation must add no pixels to the card snapshot; compare
            # against that real baseline instead of assuming a red-free UI.
            probe=SurfaceSnapshot(shelf)
            baseline_red=red_pixels(probe.capture_live(shelf.card,(shelf.pin_button,shelf.expand_button,shelf.menu_button,shelf.close_button)))
            probe.deleteLater()
            result = {'case': name, 'count': count, 'baseline_card_red_pixels':baseline_red}
            errors = []
            overlay_seen = []
            outer = QEventLoop()
            def interact():
                overlay = next((item for item in shelf.findChildren(ClearConfirmOverlay) if item.active), None)
                try:
                    assert overlay is not None and not overlay.isWindow()
                    overlay_seen.append(overlay)
                    result['overlay_in_card_subtree'] = shelf.card.isAncestorOf(overlay)
                    result['effect_moving'] = abs(shelf.surface_effect.sx-1)>.0001 or abs(shelf.surface_effect.sy-1)>.0001
                    active = shelf.grab()
                    result['active_owner_red_pixels'] = red_pixels(active)
                    assert result['active_owner_red_pixels']>500
                    snapshot = SurfaceSnapshot(shelf)
                    image = snapshot.capture_live(shelf.card, (shelf.pin_button, shelf.expand_button, shelf.menu_button, shelf.close_button))
                    result['active_card_snapshot_red_pixels'] = red_pixels(image)
                    if evidence_dir:
                        active.save(str(Path(evidence_dir)/f'{"before" if baseline else "after"}-{name}-active.png'))
                        image.save(str(Path(evidence_dir)/f'{"before" if baseline else "after"}-{name}-card-snapshot.png'))
                    snapshot.deleteLater()
                    if not baseline:
                        assert overlay.parentWidget() is shelf
                        assert not result['overlay_in_card_subtree']
                        assert result['active_card_snapshot_red_pixels'] == result['baseline_card_red_pixels'], result
                    overlay.delete_button.click()
                    assert not overlay.active and not overlay.isVisible()
                    if not baseline:
                        assert not any(timer.isActive() for timer in overlay.findChildren(QTimer))
                except BaseException as error:
                    errors.append(error)
                    if overlay is not None: overlay.cancel()
            def entry():
                QTimer.singleShot(20, interact)
                popup.clear_button.click()
                if errors: outer.quit(); return
                assert not shelf.files['permanent'] and not confirmation_active(shelf)
                result['after_clear_red_pixels'] = red_pixels(shelf.grab())
                if name=='immediate_resize':
                    shelf.collapse_contents()
                    run_for(45)
                    result['during_resize_red_pixels'] = red_pixels(shelf.grab())
                    shelf.show_contents()
                QTimer.singleShot(300, outer.quit)
            QTimer.singleShot(0, entry)
            QTimer.singleShot(5000, outer.quit)
            outer.exec()
            if errors: raise errors[0]
            until(lambda: popup.retirement.is_idle and not shelf.geometry_morph.active)
            result['settled_red_pixels'] = red_pixels(shelf.grab())
            result['overlay_objects_remaining'] = len(shelf.findChildren(ClearConfirmOverlay))
            if evidence_dir: shelf.grab().save(str(Path(evidence_dir)/f'{"before" if baseline else "after"}-{name}-cleared.png'))
            assert result['after_clear_red_pixels'] == 0 and result['settled_red_pixels'] == 0, result
            assert result.get('during_resize_red_pixels', 0) == 0, result
            assert result['overlay_objects_remaining'] == 0, result
            assert all(Path(path).is_file() for path in originals)
            evidence.append(result)
            print('PASS real clear lifecycle', json.dumps(result), flush=True)
        manager.quit()
        run_for(30)

        if not baseline:
            # Small child host separately exercises geometry/focus/lifetime
            # API contracts without mixing automatic manager visibility rules.
            owner = QWidget(); owner.resize(850, 620)
            card = QFrame(owner); card.setGeometry(24, 31, 788, 550)
            field = QLineEdit('previous focus', card); field.setGeometry(20, 20, 180, 30)
            owner.show(); run_for(20); field.setFocus(); run_for(1)
            overlay = ClearConfirmOverlay(card, 2)
            results = []
            overlay.finished.connect(results.append)
            assert overlay.open()
            assert confirmation_active(owner) and confirmation_active(card)
            card.move(35, 42); card.resize(720, 510); owner.resize(900, 660)
            run_for(15)
            assert overlay.geometry() == QRect(card.mapTo(owner, QPoint()), card.size())
            assert overlay.cancel_button.hasFocus()
            overlay.cancel(); overlay.cancel()
            assert results == [False] and field.hasFocus()
            assert not confirmation_active(owner) and not confirmation_active(card)
            overlay.deleteLater(); run_for(10)
            # Removing just the card must also end an asynchronous overlay.
            disappearing = ClearConfirmOverlay(card, 2)
            assert disappearing.open()
            card.deleteLater(); run_for(15)
            assert not disappearing.active and not disappearing.isVisible() and not confirmation_active(owner)
            disappearing.deleteLater(); run_for(10)
            # Destroying an owner inside exec must safely quit its child loop.
            card2 = QFrame(owner); card2.setGeometry(10, 10, 700, 500); card2.show()
            synchronous = ClearConfirmOverlay(card2, 2)
            QTimer.singleShot(10, owner.deleteLater)
            assert synchronous.exec() is False
            assert not isValid(owner) and not isValid(synchronous)
            print('PASS sibling geometry, focus, cancellation, card deletion and owner deletion inside exec')

        if evidence_dir:
            (Path(evidence_dir)/f'clear-lifecycle-{"before" if baseline else "after"}.json').write_text(json.dumps(evidence, indent=2), encoding='utf8')

print('CLEAR_LIFECYCLE_V163_' + ('BASELINE_OK' if baseline else 'OK'))
