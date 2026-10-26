"""Missing-file guards from the media UI, silent and offscreen."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['QT_MEDIA_BACKEND'] = 'windows'

from pathlib import Path
import shutil
import tempfile
import time
import sys
from unittest.mock import patch

from PySide6.QtCore import QObject, QEventLoop, QTimer, Signal
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QMediaPlayer as NativePlayer, QVideoFrame
from PySide6.QtWidgets import QApplication
import media_preview as subject
if '--baseline' in sys.argv:
    import importlib.util
    spec = importlib.util.spec_from_file_location('media_missing_baseline', Path(__file__).parent / 'analysis-audit' / 'media_preview.py')
    subject = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(subject)


def wait_until(predicate, timeout=8):
    started = time.monotonic()
    while not predicate() and time.monotonic() - started < timeout:
        loop = QEventLoop();QTimer.singleShot(5, loop.quit);loop.exec()
    assert predicate(), 'Media condition did not complete'


class GuardedPlayer(QObject):
    durationChanged = Signal(int)
    positionChanged = Signal(int)
    seekableChanged = Signal(bool)
    playbackStateChanged = Signal(object)
    mediaStatusChanged = Signal(object)
    errorOccurred = Signal(object, str)
    PlaybackState, MediaStatus = NativePlayer.PlaybackState, NativePlayer.MediaStatus
    constructed = []
    def __init__(self, parent):
        super().__init__(parent);self.source = None;self.state = self.PlaybackState.StoppedState
        self.plays = self.positions = self.opens = 0;self.__class__.constructed.append(self)
    def setAudioOutput(self, value):self.audio = value
    def setVideoSink(self, value):self.sink = value
    def setSource(self, value):
        if not value.isEmpty():
            assert Path(value.toLocalFile()).is_file(), 'Backend opened missing source'
            self.opens += 1
        self.source = value
        if not value.isEmpty():self.mediaStatusChanged.emit(self.MediaStatus.LoadedMedia)
    def duration(self):return 1000
    def position(self):
        self.positions += 1
        assert self.source is None or self.source.isEmpty() or Path(self.source.toLocalFile()).is_file(), 'Stale native position query'
        return 0
    def setPosition(self, value):assert Path(self.source.toLocalFile()).is_file(), 'Stale native seek'
    def isSeekable(self):return True
    def playbackState(self):return self.state
    def play(self):
        assert Path(self.source.toLocalFile()).is_file(), 'Backend played missing source'
        self.plays += 1;self.state = self.PlaybackState.PlayingState
        self.playbackStateChanged.emit(self.state)
    def pause(self):self.state = self.PlaybackState.PausedState
    def stop(self):self.state = self.PlaybackState.StoppedState
    def errorString(self):return 'guarded backend'


class Audio(QObject):
    def setVolume(self, value):pass
    def setMuted(self, value):pass


class Sink(QObject):
    videoFrameChanged = Signal(object)
    def setVideoFrame(self, value):pass


app = QApplication([])
with tempfile.TemporaryDirectory(prefix='flickdock-missing-media-') as temporary:
    directory = Path(temporary)
    # Real UI methods and callbacks; the deterministic backend fails on every
    # attempted native open/play/seek/position call after the path disappears.
    with patch.object(subject, 'QMediaPlayer', GuardedPlayer), patch.object(subject, 'QAudioOutput', Audio), patch.object(subject, 'QVideoSink', Sink):
        for operation in ('toggle', 'same-source', 'poster', 'start', 'seek', 'loaded'):
            path = directory / (operation + '.mp4');path.write_bytes(b'controlled media')
            viewer = subject.MediaPreview(motion=False);viewer.show();viewer.set_source(path)
            player = viewer.player
            image = QImage(64, 40, QImage.Format.Format_RGB32);image.fill(0xff2694c6)
            viewer.video_sink.videoFrameChanged.emit(QVideoFrame(image))
            assert not viewer.surface.image.isNull()
            plays, positions = player.plays, player.positions
            path.unlink()
            if operation == 'toggle':viewer.toggle_playback()
            elif operation == 'same-source':viewer.set_source(path)
            elif operation == 'poster':viewer._poster_pending = True;viewer._begin_poster()
            elif operation == 'start':viewer._start_playback()
            elif operation == 'seek':viewer._seek(500)
            else:viewer._status_changed(NativePlayer.MediaStatus.LoadedMedia)
            assert viewer.player is None and viewer._failed
            assert '删除' in viewer.status_label.text()
            assert not viewer.play_button.isEnabled() and not viewer.open_button.isEnabled()
            assert viewer.surface.image.isNull() and not viewer.surface.fallback.isNull()
            assert player.plays == plays and player.positions == positions
            count = len(GuardedPlayer.constructed)
            viewer.hide();viewer.show();viewer.toggle_playback()
            assert len(GuardedPlayer.constructed) == count
            viewer.release_source();viewer.close()
        print('PASS existing-player missing path never reaches native open/play/seek/position across six UI entry paths')

    fixture = Path(os.getenv('FLICKDOCK_TEST_MEDIA', str(Path(__file__).parent / 'media-test.mp4')))
    if fixture.is_file():
        source = directory / 'native-cached.mp4';shutil.copyfile(fixture, source)
        viewer = subject.MediaPreview(motion=False);viewer.resize(400, 320);viewer.toggle_mute();viewer.show()
        viewer.set_source(source)
        wait_until(lambda: not viewer.surface.image.isNull() or viewer._failed)
        assert not viewer._failed, viewer.status_label.toolTip()
        assert viewer.audio_output.isMuted()
        viewer.stop()
        # Native release can finish on the next event turn.
        def remove():
            try:source.unlink(missing_ok=True);return True
            except PermissionError:return False
        wait_until(remove, 3)
        assert not source.exists()
        with patch.object(subject, 'QMediaPlayer', side_effect=AssertionError('Missing source reopened backend')):
            started = time.perf_counter()
            viewer.toggle_playback();viewer.hide();viewer.show();viewer.set_source(source)
            assert time.perf_counter() - started < .2
            assert viewer.player is None and '删除' in viewer.status_label.text()
            assert viewer.surface.image.isNull() and not viewer.surface.fallback.isNull()
            beats = [];QTimer.singleShot(1, lambda: beats.append(True));wait_until(lambda: bool(beats), .2)
            viewer.release_source();viewer.hide();viewer.show();viewer.toggle_playback()
            assert not viewer.path and viewer.player is None
        viewer.close()
        print('PASS real native poster, release, original deletion, cached replay/reopen and heartbeat remain silent and responsive')
    else:
        print('SKIP native fixture unavailable')
print('MEDIA_MISSING_V163_OK')
