"""Test only a disposable Cocoa shelf with generated videos, under a supervisor.

No account, cloud backend, global shortcut, real clipboard or installed app is
used. The supervisor samples only its own child on a timeout. There is no
faulthandler.dump_traceback_later timer.
"""
import argparse
import faulthandler
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2), encoding='utf8')
    temporary.replace(path)


def worker(args):
    os.environ['QT_QPA_PLATFORM'] = 'cocoa'
    os.environ['QT_MEDIA_BACKEND'] = 'darwin'
    os.environ['QT_MAC_SET_RAISE_PROCESS'] = '0'
    sys.path.insert(0, str(args.source))
    output = args.output
    stack = (output / 'python-stack.txt').open('w', encoding='utf8', buffering=1)
    faulthandler.enable(stack, all_threads=True)
    events = (output / 'breadcrumbs.jsonl').open('w', encoding='utf8', buffering=1)
    qt_log = (output / 'qt-messages.txt').open('w', encoding='utf8', buffering=1)

    def note(name, **fields):
        row = dict(event=name, at=time.time(), **fields)
        events.write(json.dumps(row) + '\n')
        atomic_json(output / 'heartbeat.json', row)

    def exception(kind, error, tb):
        value = ''.join(traceback.format_exception(kind, error, tb))
        stack.write(value)
        print(value, file=sys.stderr, flush=True)

    sys.excepthook = exception
    from PySide6.QtCore import Qt, qInstallMessageHandler
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QWidget
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    qInstallMessageHandler(lambda kind, context, message: qt_log.write(str(message) + '\n'))
    if args.renderer == 'legacy':
        import importlib.util
        import file_preview
        legacy = args.source / 'ci_media_preview_legacy.py'
        spec = importlib.util.spec_from_file_location('flickdock_legacy_media', legacy)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        file_preview.MediaPreview = module.MediaPreview
    import test_html_native_workspace as workspace
    os.environ['QT_QPA_PLATFORM'] = 'cocoa'
    assert app.platformName() == 'cocoa'
    fixture = workspace.WorkspaceTests()
    fixture.setUp()
    shelf = fixture.shelf
    watchdog = None
    from crash_diagnostics import CrashDiagnostics
    if hasattr(CrashDiagnostics, 'watchdog_heartbeat'):
        from gui_watchdog import GuiWatchdog
        watchdog = GuiWatchdog(app, output / 'diagnostic-profile', timeout=8)
    try:
        note('fixture.generate')
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        mp4, mov = fixture.base / 'generated-h264.mp4', fixture.base / 'generated-h264.mov'
        subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                        'testsrc=size=1280x720:rate=30', '-t', '2', '-c:v',
                        'libx264', '-pix_fmt', 'yuv420p', '-y', str(mp4)],
                       check=True, capture_output=True, timeout=30)
        subprocess.run([ffmpeg, '-v', 'error', '-i', str(mp4), '-c', 'copy', '-y', str(mov)],
                       check=True, capture_output=True, timeout=30)
        image = fixture.base / 'generated.png'
        picture = QImage(96, 96, QImage.Format_RGB32)
        picture.fill(QColor('#4477aa'))
        assert picture.save(str(image))
        shelf.manager.motion = True
        shelf.presentation.motion = True
        shelf.geometry_morph.motion = True
        shelf.add_paths('permanent', [str(mp4), str(mov), str(image)])
        workspace.flush()
        note('overview.open')
        shelf.show_contents()
        workspace.until(lambda: not shelf.geometry_morph.active, seconds=8)
        popup = shelf.popup
        popup.resume_content()
        workspace.until(lambda: popup._build_cursor == len(popup.visible_refs()), seconds=10)
        note('video.first_select')
        popup.select_item(str(mp4))
        workspace.flush()
        media = popup.preview.media
        workspace.until(lambda: media._poster_sent or media._failed, seconds=12)
        if media._failed or not media._poster_sent:
            raise AssertionError('Generated H.264 video did not supply its poster')
        if args.renderer == 'composited':
            if media.surface.native is not None or any(
                    w.metaObject().className() == 'QVideoWidget' for w in media.findChildren(QWidget)):
                raise AssertionError('Composited preview created a native video widget')
        observed = []
        media.video_sink.videoFrameChanged.connect(lambda frame: observed.append(frame.startTime()) if frame.isValid() else None)
        note('video.play')
        media.toggle_playback()
        workspace.until(lambda: len(observed) >= 5 or media._failed, seconds=8)
        if media._failed or len(observed) < 5:
            raise AssertionError('Video did not continue playing')
        first_image = media.surface.grab().toImage()
        count = len(observed)
        workspace.until(lambda: len(observed) >= count + 3 or media._failed, seconds=6)
        second_image = media.surface.grab().toImage()
        if args.renderer == 'composited' and first_image == second_image:
            raise AssertionError('Decoded frames changed but the preview pixels did not')
        playback = dict(frames=len(observed), painted_frames_change=(first_image != second_image)
                        if args.renderer == 'composited' else None)
        media.toggle_playback()
        popup.selected.clear()
        popup.primary_selection = None
        popup.restore_selected_preview()
        workspace.flush()
        workspace.until(lambda: media.player is None, seconds=5)
        modes = {}
        for mode in ('list', 'icons'):
            popup.change_view_mode(mode)
            popup.resume_content()
            workspace.until(lambda: popup._build_cursor == len(popup.visible_refs()), seconds=10)
            refs = [str(mp4), str(mov), str(image), fixture.paths[0]]
            for index in range(args.clicks):
                ref = refs[index % len(refs)]
                item = popup._item_widgets[ref]
                note('file.click', mode=mode, index=index,
                     kind='mp4' if ref == str(mp4) else 'mov' if ref == str(mov) else 'other')
                before = set(popup.selected)
                expected = before - {ref} if ref in before else {ref}
                point = item.mapTo(shelf, item.rect().center())
                QTest.mouseClick(shelf.windowHandle(), Qt.LeftButton, pos=point)
                QTest.qWait(80 if index else 800)
                workspace.flush()
                if not shelf.isVisible() or not shelf.expanded or not popup.isVisible():
                    raise AssertionError('Overview disappeared during file selection')
                if popup.selected != expected:
                    raise AssertionError('File selection click was intercepted')
            popup.selected.clear()
            popup.primary_selection = None
            popup.restore_selected_preview()
            workspace.flush()
            workspace.until(lambda: popup.preview.media.player is None, seconds=5)
            modes[mode] = dict(clicks=args.clicks, player_released=True)
        note('video.select.before_collapse')
        popup.select_item(str(mp4))
        workspace.flush()
        workspace.until(lambda: popup.preview.media._poster_sent or popup.preview.media._failed, seconds=12)
        note('overview.collapse')
        shelf.collapse_contents()
        workspace.until(lambda: not shelf.geometry_morph.active, seconds=8)
        note('overview.reopen')
        shelf.show_contents()
        workspace.until(lambda: not shelf.geometry_morph.active, seconds=8)
        popup.resume_content()
        workspace.until(lambda: popup._build_cursor == len(popup.visible_refs()), seconds=10)
        snapshot = getattr(shelf, '_surface_snapshot', None)
        if snapshot is not None and (snapshot.active or snapshot.isVisible()):
            raise AssertionError('Morph left a visible or active snapshot')
        note('file.click.after_reopen')
        item = popup._item_widgets[str(image)]
        QTest.mouseClick(shelf.windowHandle(), Qt.LeftButton,
                         pos=item.mapTo(shelf, item.rect().center()))
        workspace.flush()
        if popup.selected != {str(image)}:
            raise AssertionError('File row could not be selected after reopening')
        from shelf_window import ShelfWindow
        windows = [w for w in app.topLevelWidgets() if isinstance(w, ShelfWindow) and w.isVisible()]
        if windows != [shelf]:
            raise AssertionError('Multiple visible shelf windows after video selection')
        shelf.grab().save(str(output / 'shelf.png'))
        atomic_json(output / 'worker-result.json', dict(ok=True, qt_platform='cocoa',
                    media_backend='darwin', source=str(args.source), modes=modes, playback=playback,
                    renderer=args.renderer, snapshot_finished=True, selected_after_reopen=True,
                    gil_held_diagnostics=watchdog is not None,
                    video_collapse_reopen='single-window'))
        note('test.complete')
    except BaseException:
        exception(*sys.exc_info())
        atomic_json(output / 'worker-result.json', dict(ok=False, error=traceback.format_exc()))
        raise
    finally:
        fixture.tearDown()
        if watchdog is not None:
            watchdog.close()
            watchdog.diagnostics.wait_closed()
        qInstallMessageHandler(None)
        faulthandler.disable()
        sys.excepthook = sys.__excepthook__
        stack.close()
        events.close()
        qt_log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('mac-video-probe-output'))
    parser.add_argument('--clicks', type=int, default=50)
    parser.add_argument('--timeout', type=int, default=120)
    parser.add_argument('--renderer', choices=('composited', 'legacy'), default='composited')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.source, args.output = args.source.resolve(), args.output.resolve()
    if not (args.source / 'flickdock.py').is_file():
        parser.error('--source must contain flickdock.py')
    if not 1 <= args.clicks <= 200 or not 30 <= args.timeout <= 600:
        parser.error('clicks must be 1..200 and timeout 30..600 seconds')
    if sys.platform != 'darwin':
        parser.error('This test requires macOS; Windows offscreen results cannot replace it')
    args.output.mkdir(parents=True, exist_ok=True)
    if args.worker:
        worker(args)
        return 0
    with tempfile.TemporaryDirectory(prefix='flickdock-supervised-profile-') as profile:
        env = dict(os.environ, HOME=profile, USERPROFILE=profile, APPDATA=profile,
                   LOCALAPPDATA=profile, XDG_CONFIG_HOME=profile, XDG_DATA_HOME=profile,
                   PYTHONUTF8='1')
        command = [sys.executable, str(Path(__file__).resolve()), '--worker', '--source', str(args.source),
                   '--output', str(args.output), '--clicks', str(args.clicks), '--timeout', str(args.timeout),
                   '--renderer', args.renderer]
        with (args.output / 'process.log').open('wb') as log:
            child = subprocess.Popen(command, cwd=args.source, env=env, stdout=log, stderr=log)
            try:
                returncode = child.wait(timeout=args.timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                # Sample only the child created above. Never attach to the real
                # installed FlickDock or use a timer that walks Python frames.
                with (args.output / 'sample-command.log').open('wb') as sample_log:
                    try:
                        subprocess.run(['/usr/bin/sample', str(child.pid), '1', '1', '-file',
                                        str(args.output / 'native-stall-sample.txt')],
                                       stdout=sample_log, stderr=sample_log, timeout=15)
                    except subprocess.TimeoutExpired:
                        sample_log.write(b'Native sample command timed out\n')
                child.terminate()
                try:
                    returncode = child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    returncode = child.wait(timeout=5)
                timed_out = True
    result = dict(returncode=returncode, timeout=timed_out,
                  ok=returncode == 0 and not timed_out,
                  scope='Generated fixtures in a fake shelf; not installed-app acceptance')
    atomic_json(args.output / 'supervisor-result.json', result)
    print(json.dumps(result, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
