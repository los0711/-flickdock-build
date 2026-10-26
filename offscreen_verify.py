"""Frozen release smoke check. Explicit offscreen mode is mandatory."""
import ctypes
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
import sys
import zipfile


def run_update(argv, load_font):
    """Exercise the shipped public update channel without executing an installer."""
    if os.environ.get('QT_QPA_PLATFORM') != 'offscreen':
        raise RuntimeError('Update verification requires QT_QPA_PLATFORM=offscreen')
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from app_version import APP_VERSION
    from update_client import UpdateClient
    output = Path(argv[argv.index('--verify-output') + 1]).resolve()
    current = (argv[argv.index('--verify-current') + 1]
               if '--verify-current' in argv else APP_VERSION)
    report = {'ok': False, 'frozen': bool(getattr(sys, 'frozen', False)),
              'version': APP_VERSION, 'simulated_current': current, 'installer_executed': False}
    app = QApplication([argv[0]])
    app.setQuitOnLastWindowClosed(False)
    load_font(app)
    with tempfile.TemporaryDirectory(prefix='flickdock-channel-') as profile:
        updater = UpdateClient(current, Path(profile) / 'updates', timeout_ms=60000)
        done = False

        def finish(error=''):
            nonlocal done
            if done: return
            done = True
            report.update(ok=not error, error=error)
            updater.close()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
            app.exit(1 if error else 0)

        def available(manifest, manual):
            report['available_version'] = manifest['version']
            report['installer_url'] = manifest['installer']['url']
            updater.download(manifest, manual=True)

        def verified(path, manifest, manual):
            file = Path(path)
            report.update(download_bytes=file.stat().st_size,
                          download_sha256=hashlib.sha256(file.read_bytes()).hexdigest(),
                          latest_version=manifest['version'], verified_download=True)
            finish()

        updater.update_available.connect(available)
        updater.verified_file.connect(verified)
        updater.up_to_date.connect(lambda manual: (report.update(up_to_date=True), finish()))
        updater.failed.connect(lambda error, manual: finish(error))
        QTimer.singleShot(0, lambda: updater.check(manual=True))
        QTimer.singleShot(180000, lambda: finish('Public channel verification timed out'))
        return app.exec()


def run(argv, load_font):
    if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
        raise RuntimeError("Release verification requires QT_QPA_PLATFORM=offscreen")
    from PySide6.QtCore import QTimer
    from PySide6.QtNetwork import QSslSocket
    from PySide6.QtWidgets import QApplication
    from flickdock_icons import glyph_pixmap
    from media_preview import MediaPreview, QMediaPlayer
    from async_images import ImageCache
    from selection_archive import ZipArchiveJob
    from capture_files import CopyFilesJob

    original = Path(argv[argv.index("--verify-media") + 1]).resolve()
    profile = tempfile.TemporaryDirectory(prefix='flickdock-frozen-media-',ignore_cleanup_errors=True)
    fixture=Path(profile.name)/original.name
    shutil.copyfile(original,fixture)
    output = Path(argv[argv.index("--verify-output") + 1]).resolve()
    app = QApplication([argv[0]])
    app.setQuitOnLastWindowClosed(False)
    load_font(app)
    viewer = MediaPreview(motion=False)
    viewer.resize(440, 320)
    viewer.show()
    viewer.toggle_mute()
    report = {"ok": False, "platform": app.platformName(), "muted": True,
              "frozen": bool(getattr(sys, "frozen", False)), "phase": "cover"}
    finished = False
    stage = "cover"
    frame = None
    image_cache=ImageCache(app)
    archive_job=None
    copy_job=None

    def module_path(name):
        kernel = ctypes.windll.kernel32
        kernel.GetModuleHandleW.restype = ctypes.c_void_p
        handle = kernel.GetModuleHandleW(name)
        if not handle:
            return ""
        buf = ctypes.create_unicode_buffer(32768)
        kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint]
        kernel.GetModuleFileNameW(handle, buf, len(buf))
        return buf.value

    def finish(error=""):
        nonlocal finished
        if finished:
            return
        finished = True
        timer.stop()
        report["error"] = error
        report["ok"] = not error
        image_cache.close()
        viewer.stop()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
        viewer.hide()
        app.exit(1 if error else 0)

    def got_frame(path, image):
        nonlocal frame
        frame = image.copy()

    def tick():
        nonlocal stage,archive_job,copy_job
        try:
            assert viewer.audio_output is None or viewer.audio_output.isMuted(), "Verification audio must stay muted"
            if viewer._failed:
                raise RuntimeError(viewer.status_label.text())
            if stage == "cover" and frame is not None:
                if viewer.player.playbackState() != QMediaPlayer.PlaybackState.PausedState:
                    return
                assert frame.width() > 32 and frame.height() > 32
                report.update(frame=[frame.width(), frame.height()], duration_ms=viewer.player.duration())
                report["media_plugin"] = module_path("windowsmediaplugin.dll")
                assert report["media_plugin"], "Native Windows media plugin did not load"
                if report["frozen"]:
                    assert Path(report["media_plugin"]).is_relative_to(Path(sys._MEIPASS)), "Plugin loaded outside bundle"
                assert QSslSocket.supportsSsl(), "Packaged Qt TLS backend is unavailable"
                report["tls_backend"] = QSslSocket.activeBackend()
                assert hashlib.sha256(b"abc").hexdigest() == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
                icon = glyph_pixmap("pin", 24, "#182635", dpr=1.25)
                assert not icon.isNull() and icon.width() == 30
                report["svg_dpr"] = icon.devicePixelRatio()
                frame.save(str(output.with_suffix(".png")))
                viewer.seek_slider.setValue(min(1000, viewer.seek_slider.maximum()))
                viewer.toggle_playback()
                stage = report["phase"] = "play"
            elif stage == "play" and viewer.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                viewer.toggle_playback()
                stage = report["phase"] = "pause"
            elif stage == "pause" and viewer.player.playbackState() == QMediaPlayer.PlaybackState.PausedState:
                viewer.hide()
                assert viewer.player is None or viewer.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState
                stage = report['phase'] = 'release'
            elif stage == 'release':
                try:fixture.unlink()
                except PermissionError:return
                report['source_file_released']=True
                viewer.toggle_playback()
                assert viewer._failed and viewer.player is None
                assert '删除' in viewer.status_label.text()
                report['missing_source_replay_guard']=True
                viewer.release_source()
                payload=Path(profile.name)/'selected.txt'
                payload.write_text('FlickDock ZIP export',encoding='utf8')
                archive_job=ZipArchiveJob([str(payload)],Path(profile.name)/'selected.zip',
                                          texts=[('Native text','直接打包文字')],parent=app)
                archive_job.failed.connect(finish)
                archive_job.start()
                stage=report['phase']='archive'
            elif stage=='archive' and archive_job.result_path:
                with zipfile.ZipFile(archive_job.result_path) as archive:
                    assert archive.testzip() is None
                    assert archive.read('selected.txt')==b'FlickDock ZIP export'
                    assert archive.read('Native text.txt').decode('utf8')=='直接打包文字'
                report['selected_zip_export']=True
                copy_job=CopyFilesJob([str(Path(profile.name)/'selected.txt')],Path(profile.name)/'saved',parent=app)
                copy_job.failed.connect(finish);copy_job.start()
                stage=report['phase']='copy'
            elif stage=='copy' and copy_job.result_paths:
                copied=Path(copy_job.result_paths[0])
                assert copied.read_text(encoding='utf8')=='FlickDock ZIP export'
                assert copied.is_relative_to(Path(profile.name)/'saved'/'imports')
                report['background_file_copy']=True
                image_path=Path(profile.name)/'thumbnail.png'
                assert frame.save(str(image_path),'PNG')
                image_cache.request(str(image_path),128)
                stage=report['phase']='thumbnail'
            elif stage=='thumbnail':
                image=image_cache.image(str(Path(profile.name)/'thumbnail.png'),128)
                if image is None:return
                assert not image.isNull() and max(image.width(),image.height())<=128
                report['async_thumbnail']=[image.width(),image.height()]
                report['phase']='complete';finish()
        except Exception as exc:
            finish(str(exc))

    viewer.frame_ready.connect(got_frame)
    timer = QTimer()
    timer.setInterval(30)
    timer.timeout.connect(tick)
    timer.start()
    QTimer.singleShot(15000, lambda: finish("Native media verification timed out"))
    viewer.set_source(str(fixture))
    code = app.exec()
    profile.cleanup()
    raise SystemExit(code)
