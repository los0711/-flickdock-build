"""Deterministic offscreen control/lifecycle tests; never plays system audio."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from pathlib import Path
import tempfile
from unittest.mock import patch

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QFont, QFontDatabase, QImage
from PySide6.QtMultimedia import QMediaPlayer as NativePlayer, QVideoFrame
from PySide6.QtWidgets import QApplication

import media_preview as subject


class FakePlayer(QObject):
    durationChanged = Signal(int)
    positionChanged = Signal(int)
    seekableChanged = Signal(bool)
    playbackStateChanged = Signal(object)
    mediaStatusChanged = Signal(object)
    errorOccurred = Signal(object, str)
    PlaybackState = NativePlayer.PlaybackState
    MediaStatus = NativePlayer.MediaStatus

    def __init__(self, parent):
        super().__init__(parent)
        self.state = self.PlaybackState.StoppedState
        self.position_value = 0
        self.play_calls = self.stop_calls = 0
        self.source = None

    def setAudioOutput(self, audio):
        self.audio = audio

    def setVideoSink(self, sink):
        self.sink = sink

    def setSource(self, url):
        self.source = url
        self.mediaStatusChanged.emit(self.MediaStatus.LoadedMedia)

    def duration(self):
        return 12000

    def position(self):
        return self.position_value

    def setPosition(self, position):
        self.position_value = position
        self.positionChanged.emit(position)

    def isSeekable(self):
        return True

    def playbackState(self):
        return self.state

    def play(self):
        self.play_calls += 1
        self.state = self.PlaybackState.PlayingState
        self.playbackStateChanged.emit(self.state)

    def pause(self):
        self.state = self.PlaybackState.PausedState
        self.playbackStateChanged.emit(self.state)

    def stop(self):
        self.stop_calls += 1
        self.state = self.PlaybackState.StoppedState
        self.playbackStateChanged.emit(self.state)

    def errorString(self):
        return "Test unsupported codec"


class FakeAudio(QObject):
    def setVolume(self, volume):
        self.volume = volume

    def setMuted(self, muted):
        self.muted = muted


class FakeSink(QObject):
    videoFrameChanged = Signal(object)

    def setVideoFrame(self, frame):
        self.videoFrameChanged.emit(frame)


app = QApplication([])
font_id = QFontDatabase.addApplicationFont(str(Path(__file__).parent / "fonts" / "HarmonyOS_Sans_SC_Regular.ttf"))
app.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 11))
assert subject.MediaPreview.can_preview("example.MP4")
assert subject.MediaPreview.can_preview(Path("example.flac"))
assert not subject.MediaPreview.can_preview("example.pdf")
assert not subject.MediaPreview.can_preview(None)
assert subject._clock(7203000) == "2:00:03"

with tempfile.TemporaryDirectory(prefix="flickdock-media-") as temporary, \
     patch.object(subject, "QMediaPlayer", FakePlayer), \
     patch.object(subject, "QAudioOutput", FakeAudio), \
     patch.object(subject, "QVideoSink", FakeSink), \
     patch.object(subject.QDesktopServices, "openUrl") as open_url:
    video = Path(temporary) / "first.mp4"
    second = Path(temporary) / "second.mp4"
    audio = Path(temporary) / "sound.wav"
    unsupported = Path(temporary) / "notes.txt"
    for path in (video, second, audio, unsupported):
        path.write_bytes(b"Mock decoder data")
    typescript = Path(temporary) / "source.ts"
    transport = Path(temporary) / "video.ts"
    typescript.write_text("export const message: string = 'Readable source';", encoding="utf8")
    transport.write_bytes((b"\x47" + bytes(187)) * 2)
    assert not subject.MediaPreview.can_preview(typescript)
    assert subject.MediaPreview.can_preview(transport)
    assert not subject.MediaPreview.can_preview(Path(temporary) / "missing.ts")
    preview = subject.MediaPreview(motion=False)
    preview.resize(380, 290)
    preview.show()
    events, frames = [], []
    preview.playing_changed.connect(events.append)
    preview.frame_ready.connect(lambda path, image: frames.append((path, image.copy())))
    preview.set_source(video)
    assert preview.player.play_calls == 1 and preview.audio_output.muted
    assert not events
    image = QImage(64, 40, QImage.Format.Format_ARGB32)
    image.fill(0xff2584ed)
    preview.video_sink.videoFrameChanged.emit(QVideoFrame(image))
    assert preview.player.playbackState() == NativePlayer.PlaybackState.PausedState
    assert len(frames) == 1 and frames[0][0] == str(video.resolve()) and frames[0][1].size() == image.size()
    assert not events and not preview.surface.image.isNull()
    print("PASS visible video reads a muted first frame then pauses; poster extraction never reports playback")

    preview.toggle_playback()
    assert events == [True] and preview.audio_output.muted
    preview.toggle_mute()
    assert not preview.audio_output.muted
    preview.toggle_playback()
    assert events == [True, False]
    preview.seek_slider.setValue(5000)
    assert preview.player.position() == 5000 and preview.duration_label.text() == "0:05 / 0:12"
    red = QImage(image)
    red.fill(0xffed483b)
    preview.video_sink.videoFrameChanged.emit(QVideoFrame(red))
    assert preview.surface.image.pixelColor(0, 0).name() == "#ed483b" and len(frames) == 1
    print("PASS play/pause is explicit, keyboard/timeline seek updates a paused video frame and duration")

    stale_player, stale_sink = preview.player, preview.video_sink
    preview.set_source(second)
    assert stale_player.stop_calls >= 1 and preview.surface.image.isNull()
    stale_sink.videoFrameChanged.emit(QVideoFrame(red))
    assert preview.surface.image.isNull() and len(frames) == 1
    preview.video_sink.videoFrameChanged.emit(QVideoFrame(image))
    assert len(frames) == 2 and frames[-1][0] == str(second.resolve())
    stopped_player, stopped_audio = preview.player, preview.audio_output
    saved_poster = preview.surface.image.copy()
    preview.hide()
    assert preview.player is None and preview.audio_output is None and preview.video_sink is None
    assert stopped_player.source.isEmpty() and stopped_audio.muted and not preview._poster_timer.isActive()
    assert preview.path == str(second.resolve()) and preview.surface.image == saved_poster
    preview.show()
    assert preview.player is None, "Showing a remembered poster must not reopen a native source"
    preview.toggle_playback()
    assert preview.player is not None and preview.player is not stopped_player and preview.player.play_calls == 1
    assert events[-1] is True
    preview.release_source()
    assert preview.path == "" and preview.player is None and not preview.play_button.isEnabled()
    preview.hide(); preview.show(); preview.toggle_playback()
    assert preview.player is None
    print("PASS changing/hiding releases native source; a remembered poster replays lazily and removed refs never reopen")

    preview.show()
    preview.set_source(audio)
    assert preview.player.play_calls == 0 and preview.audio_output.muted
    preview.toggle_playback()
    assert not preview.audio_output.muted and events[-1]
    preview.toggle_mute()
    assert preview.audio_output.muted and preview.mute_button.accessibleName() == "取消静音"
    preview.toggle_mute()
    preview.volume_slider.setValue(35)
    assert abs(preview.audio_output.volume - .35) < .0001 and not preview.audio_output.muted
    preview.volume_slider.setValue(0)
    assert preview.audio_output.muted
    print("PASS audio never autoplays and explicit playback supports mute and adjustable volume")

    preview.player.errorOccurred.emit(NativePlayer.Error.FormatError, "Codec unavailable")
    assert not preview.play_button.isEnabled() and "默认应用" in preview.status_label.text()
    open_url.assert_not_called()
    preview.open_button.click()
    open_url.assert_called_once()
    preview.set_source(unsupported)
    assert preview.player is None and not preview.play_button.isEnabled() and preview.open_button.isEnabled()
    open_url.reset_mock()
    preview.set_source(Path(temporary) / "missing.mp4")
    assert not preview.open_button.isEnabled()
    open_url.assert_not_called()
    preview.set_source("")
    assert preview.path == "" and preview.player is None
    preview.close()
    app.processEvents()
    print("PASS native codec failures remain inline with an explicit external-open fallback")

print("MEDIA_PREVIEW_OK")
