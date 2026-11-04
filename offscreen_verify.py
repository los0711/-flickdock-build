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


def run_overview(argv,load_font):
    """Exercise the shipped UI with disposable files and no native services."""
    if os.environ.get('QT_QPA_PLATFORM')!='offscreen':
        raise RuntimeError('Overview verification requires QT_QPA_PLATFORM=offscreen')
    import time
    import traceback
    from types import SimpleNamespace
    from PySide6.QtCore import QPoint,QRect,QTimer
    from PySide6.QtGui import QImage,QColor
    from PySide6.QtWidgets import QApplication
    from app_settings import SettingsStore
    from app_version import APP_VERSION
    from flickdock import ShelfWindow
    import ui_theme
    app=QApplication([argv[0]]);app.setQuitOnLastWindowClosed(False);load_font(app)
    output=Path(argv[argv.index('--verify-output')+1]).resolve()
    capture=Path(argv[argv.index('--verify-capture')+1]).resolve() if '--verify-capture' in argv else None
    report=dict(ok=False,frozen=bool(getattr(sys,'frozen',False)),version=APP_VERSION,
                qt_platform=app.platformName(),native_hotkeys=False,startup_mutation=False)
    shelf=None
    try:
        with tempfile.TemporaryDirectory(prefix='flickdock-overview-') as directory:
            base=Path(directory);settings=SettingsStore(base/'profile')
            settings.update(theme='dark',view_mode='list',capture_mode='reference',capture_dir=str(base/'saved'))
            ui_theme.set_theme('dark');ui_theme.apply_application(app,app.font().family());queued=[]
            manager=SimpleNamespace(motion=False,settings=settings,save=lambda:None,update_handles=lambda:None,
                activate_shelf=lambda _:None,_quitting=False,outgoing_drag=False,
                show_transfer_dialog=lambda:None,show_settings=lambda:None,
                queue_transfer_paths=lambda mode,paths:queued.append((mode,paths)))
            shelf=ShelfWindow(manager,1);manager.shelf=shelf;shelf.pinned=True
            shelf._show_at(QPoint(30,30),'manual',QPoint())
            bitmap=QImage(180,120,QImage.Format_RGB32);bitmap.fill(QColor('#3B5268'))
            image=base/'Group 1.png';assert bitmap.save(str(image))
            archive=base/'flickdock_redesign_source.zip'
            with zipfile.ZipFile(archive,'w') as stream:stream.writestr('sample.txt','Original sample bytes')
            text=shelf.content.add_text('用于验证文字、复制和文件预览。')
            shelf.content.metadata[text]=dict(title='粘贴的文字.txt')
            shelf.add_paths('permanent',[str(image),str(archive),text])
            shelf.show_contents(target=QRect(30,30,1068,627),animate=False);popup=shelf.popup;popup.resume_content()
            deadline=time.monotonic()+10
            while (popup._build_cursor<len(popup.visible_refs()) or not popup.retirement.is_idle) and time.monotonic()<deadline:
                app.processEvents();time.sleep(.002)
            assert popup._build_cursor==len(popup.visible_refs())
            popup.select_item(str(archive));app.processEvents()
            preview=popup.preview
            assert len(preview.action_buttons)==6
            assert all(button.isVisible() for button in preview.action_buttons)
            assert preview.action_buttons[0].y()==preview.action_buttons[1].y()
            report['preview_actions']=[button.text() for button in preview.action_buttons]
            def capture_card(path):
                shot=shelf.grab();ratio=shot.devicePixelRatioF();rect=shelf.card.geometry()
                pixels=QRect(round(rect.x()*ratio),round(rect.y()*ratio),round(rect.width()*ratio),round(rect.height()*ratio))
                return shot.copy(pixels).save(str(path))
            if capture:
                capture.parent.mkdir(parents=True,exist_ok=True)
                assert capture_card(capture);report['capture']=str(capture)
            preview.copy_path_button.click();assert app.clipboard().text()==str(archive)
            preview.send_button.click();assert queued==[('lan',[str(archive)])]
            before=list(shelf.files['permanent'])
            def cancel_delete():
                dialog=shelf._clear_confirmation
                report['delete_choices']=[dialog.selected_button.text(),dialog.delete_button.text(),dialog.cancel_button.text()]
                if capture:
                    capture_card(capture.with_name(capture.stem+'-delete.png'))
                dialog.cancel_button.click()
            QTimer.singleShot(0,cancel_delete);popup.clear_button.click()
            assert shelf.files['permanent']==before
            preview.remove_button.click();assert str(archive) not in shelf.files['permanent'] and archive.is_file()
            report.update(ok=True,copy_path=True,send_queue=True,remove_preserves_original=True,delete_cancel=True)
            shelf.resolver.close();shelf.image_cache.close();shelf.image_cache._pool.waitForDone(5000)
            shelf.presentation.stop();shelf.geometry_morph.stop();shelf.hide();shelf.deleteLater();app.processEvents();shelf=None
    except Exception as error:
        report.update(error=str(error),traceback=traceback.format_exc())
    finally:
        if shelf is not None:
            shelf.resolver.close();shelf.image_cache.close();shelf.hide()
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return 0 if report['ok'] else 1


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
    # Verify optional native dependencies from the actual frozen payload,
    # rather than from the publishing machine's Python installation.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key=Ed25519PrivateKey.generate();message=b'FlickDock frozen crypto dependency probe'
    key.public_key().verify(key.sign(message),message)
    report.update(publisher_crypto_runtime=True)
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
    from PySide6.QtCore import QTimer, QPointF, Qt, QEvent, QCoreApplication
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtNetwork import QSslSocket
    from PySide6.QtWidgets import QApplication
    from flickdock_icons import glyph_pixmap
    from media_preview import MediaPreview, QMediaPlayer
    from app_version import APP_VERSION
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
    report = {"ok": False, "version": APP_VERSION, "platform": app.platformName(), "muted": True,
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
                report["media_plugin"] = module_path("ffmpegmediaplugin.dll")
                assert report["media_plugin"], "Packaged FFmpeg media plugin did not load"
                report['codec_libraries']={name:module_path(name) for name in ('avcodec-63.dll','avformat-63.dll','avutil-61.dll','swresample-7.dll','swscale-10.dll')}
                assert all(report['codec_libraries'].values()), 'Missing FFmpeg runtime library'
                report['audio_available']=viewer.player.hasAudio()
                if report["frozen"]:
                    assert Path(report["media_plugin"]).is_relative_to(Path(sys._MEIPASS)), "Plugin loaded outside bundle"
                    assert all(Path(value).is_relative_to(Path(sys._MEIPASS)) for value in report['codec_libraries'].values())
                assert QSslSocket.supportsSsl(), "Packaged Qt TLS backend is unavailable"
                report["tls_backend"] = QSslSocket.activeBackend()
                assert hashlib.sha256(b"abc").hexdigest() == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
                icon = glyph_pixmap("pin", 24, "#182635", dpr=1.25)
                assert not icon.isNull() and icon.width() == 30
                report["svg_dpr"] = icon.devicePixelRatio()
                frame.save(str(output.with_suffix(".png")))
                assert viewer.surface.native is not None and viewer.surface.native.isVisible()
                report['native_renderer']=True
                point=QPointF(round(viewer.seek_slider.width()*.6),viewer.seek_slider.height()//2)
                for kind,buttons in ((QEvent.MouseButtonPress,Qt.LeftButton),(QEvent.MouseButtonRelease,Qt.NoButton)):
                    QCoreApplication.sendEvent(viewer.seek_slider,QMouseEvent(kind,point,
                        QPointF(viewer.seek_slider.mapToGlobal(point.toPoint())),Qt.LeftButton,buttons,Qt.NoModifier))
                stage=report['phase']='seek'
            elif stage=='seek':
                current=viewer.video_sink.videoFrame()
                if not current.isValid() or current.startTime()<viewer.player.duration()*450:return
                assert viewer.player.duration()*.5<viewer.player.position()<viewer.player.duration()*.7
                report['clicked_seek_ms']=viewer.player.position()
                report['decoded_seek_frame_ms']=current.startTime()//1000
                viewer.toggle_playback();stage=report['phase']='play'
            elif (stage == "play" and viewer.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
                  and viewer.player.position()>report['clicked_seek_ms']+500):
                report['playback_advanced_ms']=viewer.player.position()-report['clicked_seek_ms']
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
