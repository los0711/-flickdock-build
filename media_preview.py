"""Native Windows media preview; no bundled decoder or external process.

Video opens silently, pauses on its first frame and only plays audibly after
an explicit button press. Each source owns a separate player generation so
late native callbacks cannot repaint a different item.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

if sys.platform == "win32":
    os.environ["QT_MEDIA_BACKEND"] = "windows"

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QImage, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoSink
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget

from flickdock_icons import document_pixmap, file_pixmap
from flickdock_motion import Spring
import ui_theme


VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".mpeg", ".mpg", ".3gp", ".ts", ".m2ts", ".vob"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".wma", ".opus", ".aiff", ".aif", ".amr", ".mid", ".midi"}


def _clock(milliseconds):
    seconds = max(0, int(milliseconds)) // 1000
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


class _MediaButton(QPushButton):
    def __init__(self, symbol, label, parent, motion=True):
        super().__init__(parent)
        self.symbol = symbol
        self._hovered = self._pressed = False
        self.setFixedSize(32, 32)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.spring = Spring(self, {"scale": 1, "hover": 0}, lambda _: self.update(), motion)
        self.set_label(symbol, label)

    def set_label(self, symbol, label):
        self.symbol = symbol
        self.setAccessibleName(label)
        self.setToolTip(label)
        self.update()

    def _feedback(self):
        self.spring.animate({"scale": .91 if self._pressed and self.spring.motion else 1,
                             "hover": float(self._hovered)}, response=.16, damping=.85)

    def enterEvent(self, event):
        self._hovered = True
        self._feedback()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = self._pressed = False
        self._feedback()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self._pressed = event.button() == Qt.MouseButton.LeftButton and self.isEnabled()
        self._feedback()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self._pressed = False
        self._feedback()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = QRectF(self.rect()).center()
        p.translate(center)
        p.scale(self.spring.values["scale"], self.spring.values["scale"])
        p.translate(-center)
        background = QColor(ui_theme.color("button"))
        background.setAlphaF(.65 + .35 * self.spring.values["hover"])
        p.setBrush(background)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 10, 10)
        if self.hasFocus():
            p.setPen(QPen(QColor(ui_theme.color("accent")), 1.4))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1.6, 1.6, -1.6, -1.6), 9, 9)
        if not self.isEnabled():
            p.setOpacity(.4)
        color = QColor(ui_theme.color("icon"))
        p.translate(5.5, 5.5)
        p.scale(.875,.875)
        pen = QPen(color, 1.7)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if self.symbol == "play":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawPolygon(QPolygonF([QPointF(9, 6), QPointF(18, 12), QPointF(9, 18)]))
        elif self.symbol == "pause":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawRoundedRect(QRectF(7, 6, 3.5, 12), 1.2, 1.2)
            p.drawRoundedRect(QRectF(14, 6, 3.5, 12), 1.2, 1.2)
        elif self.symbol in ("volume", "mute"):
            p.drawPolygon(QPolygonF([QPointF(4, 9), QPointF(8, 9), QPointF(12, 6), QPointF(12, 18), QPointF(8, 15), QPointF(4, 15)]))
            if self.symbol == "mute":
                p.drawLine(QPointF(16, 9), QPointF(21, 15))
                p.drawLine(QPointF(21, 9), QPointF(16, 15))
            else:
                p.drawArc(QRectF(10, 7, 8, 10), -60 * 16, 120 * 16)
                p.drawArc(QRectF(10, 4, 12, 16), -60 * 16, 120 * 16)
        else:
            p.drawLine(QPointF(13, 5), QPointF(20, 5))
            p.drawLine(QPointF(20, 5), QPointF(20, 12))
            p.drawLine(QPointF(20, 5), QPointF(11, 14))
            path = QPainterPath(QPointF(9, 6))
            path.lineTo(6, 6)
            path.lineTo(6, 19)
            path.lineTo(19, 19)
            path.lineTo(19, 16)
            p.drawPath(path)


class _VideoSurface(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.image = QImage()
        self.fallback = None
        self.setMinimumHeight(100)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        bounds = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        clipping = QPainterPath()
        clipping.addRoundedRect(bounds, 12, 12)
        p.setClipPath(clipping)
        p.fillRect(bounds, QColor("#11161e"))
        if not self.image.isNull():
            size = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRectF((self.width() - size.width()) / 2, (self.height() - size.height()) / 2,
                            size.width(), size.height())
            p.drawImage(target, self.image)
        elif self.fallback is not None and not self.fallback.isNull():
            size = min(110, self.width() - 24, self.height() - 16)
            image = self.fallback.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap((self.width() - image.width()) // 2, (self.height() - image.height()) // 2, image)


class MediaPreview(QWidget):
    frame_ready = Signal(str, QImage)
    playing_changed = Signal(bool)

    def __init__(self, parent=None, motion=True):
        super().__init__(parent)
        self.player = None
        self.video_sink = None
        self.audio_output = None
        self.path = ""
        self._generation = 0
        self._video = self._loaded = self._failed = False
        self._poster_pending = self._poster_sent = self._user_playing = False
        self._seek_frame_pending = False
        self._play_when_ready = False
        self._resume_position = 0
        self._reported_playing = self._muted = False
        self._poster_timer = QTimer(self)
        self._poster_timer.setSingleShot(True)
        self._poster_timer.setInterval(2200)
        self._poster_timer.timeout.connect(self._poster_timeout)
        self.setObjectName("mediaPreview")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(7)
        self.surface = _VideoSurface(self)
        root.addWidget(self.surface, 1)
        self.status_label = QLabel(self)
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)
        self.seek_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.setAccessibleName("播放进度")
        self.seek_slider.setEnabled(False)
        self.seek_slider.sliderMoved.connect(self._seek)
        self.seek_slider.sliderReleased.connect(lambda: self._seek(self.seek_slider.value()))
        self.seek_slider.valueChanged.connect(self._keyboard_seek)
        root.addWidget(self.seek_slider)
        controls = QHBoxLayout()
        controls.setSpacing(7)
        self.play_button = _MediaButton("play", "播放", self, motion)
        self.play_button.setEnabled(False)
        self.play_button.clicked.connect(self.toggle_playback)
        controls.addWidget(self.play_button)
        self.duration_label = QLabel("0:00 / 0:00", self)
        self.duration_label.setMinimumWidth(86)
        controls.addWidget(self.duration_label)
        controls.addStretch()
        self.mute_button = _MediaButton("volume", "静音", self, motion)
        self.mute_button.clicked.connect(self.toggle_mute)
        controls.addWidget(self.mute_button)
        self.volume_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(70)
        self.volume_slider.setFixedWidth(58)
        self.volume_slider.setAccessibleName("音量")
        self.volume_slider.valueChanged.connect(self._volume_changed)
        controls.addWidget(self.volume_slider)
        self.open_button = _MediaButton("external", "使用默认应用打开", self, motion)
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self.open_external)
        controls.addWidget(self.open_button)
        root.addLayout(controls)
        self.apply_theme()

    @staticmethod
    def can_preview(path):
        try:
            source = Path(path)
            if source.suffix.lower() == ".ts":
                try:
                    with source.open("rb") as stream:
                        probe = stream.read(376)
                    return len(probe) > 188 and probe[0] == 0x47 and probe[188] == 0x47
                except OSError:
                    return False
            return source.suffix.lower() in VIDEO_SUFFIXES | AUDIO_SUFFIXES
        except (TypeError, ValueError):
            return False

    def apply_theme(self, theme=None):
        ink, secondary = ui_theme.color("text"), ui_theme.color("secondary")
        edge, accent = ui_theme.color("border"), ui_theme.color("accent")
        self.setStyleSheet(f"""
            QWidget#mediaPreview {{background:transparent;}}
            QWidget#mediaPreview QLabel {{background:transparent;color:{secondary};font-size:12px;}}
            QWidget#mediaPreview QSlider::groove:horizontal {{height:4px;background:{edge};border-radius:2px;}}
            QWidget#mediaPreview QSlider::sub-page:horizontal {{background:{accent};border-radius:2px;}}
            QWidget#mediaPreview QSlider::handle:horizontal {{width:11px;margin:-4px 0;border-radius:5px;background:{ink};}}
            QWidget#mediaPreview QSlider::handle:horizontal:focus {{border:1px solid {accent};}}
        """)
        self.update()

    def set_source(self, path):
        value = str(Path(path).resolve()) if path else ""
        if not value:
            self.release_source()
            return
        # A remembered poster/player does not prove that its original still
        # exists. Check before the same-path fast path can touch the backend.
        if not self._source_exists(value):
            self.stop(remember_position=False)
            self.path = value
            self._video = Path(value).suffix.lower() in VIDEO_SUFFIXES
            self.surface.fallback = document_pixmap(Path(value).suffix, 110)
            self._set_error('原文件已移动或删除，无法播放。', missing=True)
            return
        if value == self.path and self.player is not None and not self._failed:
            if self._video and self.surface.image.isNull():
                self._poster_pending = True
                self._begin_poster()
            return
        same = value == self.path and not self._failed
        self.stop()
        self.path = value
        if not same:
            self.surface.image = QImage()
            self._poster_sent = False
            self._resume_position = 0
            self._set_duration(0)
        self.surface.fallback = file_pixmap(value, 110)
        self.surface.update()
        self._video = Path(value).suffix.lower() in VIDEO_SUFFIXES
        self._failed = False
        self._load_player(poster=self._video and self.surface.image.isNull())

    def _load_player(self, *, poster=False, play=False):
        """Create a fresh native source only for selection or explicit playback."""
        value = self.path
        exists = self._source_exists(value)
        self.open_button.setEnabled(exists)
        if not exists:
            self._set_error('原文件已移动或删除，无法播放。', missing=True)
            return False
        if self.player is not None:
            return True
        if not self.can_preview(value):
            self._set_error("文件不可用或不支持媒体预览。")
            return False
        self._generation += 1
        self._loaded = False
        self._poster_pending = bool(poster and self._video)
        self._play_when_ready = bool(play)
        self.open_button.setEnabled(True)
        self.play_button.setEnabled(False)
        self.seek_slider.setEnabled(False)
        self.status_label.setText("正在读取媒体…")
        self.status_label.show()
        generation = self._generation
        player = self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(self.volume_slider.value() / 100)
        self.audio_output.setMuted(True)
        player.setAudioOutput(self.audio_output)
        self.video_sink = QVideoSink(self)
        player.setVideoSink(self.video_sink)
        self.video_sink.videoFrameChanged.connect(lambda frame: self._frame_changed(frame, generation))
        player.durationChanged.connect(lambda duration: self._set_duration(duration) if generation == self._generation else None)
        player.positionChanged.connect(lambda position: self._set_position(position) if generation == self._generation else None)
        player.seekableChanged.connect(lambda value: self.seek_slider.setEnabled(value) if generation == self._generation else None)
        player.playbackStateChanged.connect(lambda state: self._state_changed(state) if generation == self._generation else None)
        player.mediaStatusChanged.connect(lambda status: self._status_changed(status) if generation == self._generation else None)
        player.errorOccurred.connect(lambda error, message: self._set_error(message) if generation == self._generation else None)
        player.setSource(QUrl.fromLocalFile(value))
        return self.player is player

    def _dispose_player(self):
        player, sink, audio = self.player, self.video_sink, self.audio_output
        self.player = self.video_sink = self.audio_output = None
        if audio is not None:
            audio.setMuted(True)
        if player is not None:
            # stop() alone retains the Media Foundation source and its file
            # handle. Clearing the source and outputs releases that ownership.
            player.stop()
            player.setSource(QUrl())
            player.setVideoSink(None)
            player.setAudioOutput(None)
        if sink is not None:
            sink.setVideoFrame(QVideoFrame())
        for item in (player, sink, audio):
            if item is not None:
                item.deleteLater()

    def _status_changed(self, status):
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            if not self._ensure_source_available() or self.player is None:
                return
            self._loaded = True
            self.play_button.setEnabled(True)
            self.seek_slider.setEnabled(self.player.isSeekable())
            self._set_duration(self.player.duration())
            self.status_label.setText("音频" if not self._video else "")
            self.status_label.setVisible(not self._video)
            if self._play_when_ready:
                self._start_playback()
            else:
                self._begin_poster()
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._set_error(self.player.errorString())
        elif status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._user_playing = False
            self._report_playing(False)
            self.play_button.set_label("play", "重新播放")

    def _begin_poster(self):
        if (self.player is not None and self._video and self._poster_pending and self._loaded
                and self.isVisible() and not self._failed and not self._user_playing
                and not self._poster_timer.isActive()):
            if not self._ensure_source_available():
                return
            self.audio_output.setMuted(True)
            self.player.setPosition(0)
            self._poster_timer.start()
            self.player.play()

    def _frame_changed(self, frame, generation):
        if generation != self._generation or self.player is None or not frame.isValid():
            return
        if not self._poster_pending and not self._user_playing and not self._seek_frame_pending:
            return
        image = frame.toImage()
        if image.isNull():
            return
        self.surface.image = image
        self._seek_frame_pending = False
        self.surface.update()
        first_frame = not self._poster_sent
        self._poster_sent = True
        if self._poster_pending:
            self._poster_pending = False
            self._poster_timer.stop()
            self.player.pause()
            self.player.setPosition(0)
            self._report_playing(False)
        self.status_label.hide()
        if first_frame:
            self.frame_ready.emit(self.path, image.copy())

    def _poster_timeout(self):
        if not self._poster_pending:
            return
        self._poster_pending = False
        if self.player is not None:
            self.player.pause()
        self.status_label.setText("封面暂不可用")
        self.status_label.show()
        self._report_playing(False)

    @staticmethod
    def _source_exists(path):
        try:
            return bool(path) and Path(path).is_file()
        except (OSError, ValueError, TypeError):
            return False

    def _ensure_source_available(self):
        if self._source_exists(self.path):
            return True
        self._set_error('原文件已移动或删除，无法播放。', missing=True)
        return False

    def _set_error(self, message, *, missing=False):
        self._failed = True
        self.stop(remember_position=not missing)
        self.play_button.setEnabled(False)
        self.seek_slider.setEnabled(False)
        self.open_button.setEnabled(self._source_exists(self.path))
        if missing:
            self.surface.image = QImage()
            self._poster_sent = False
            self._resume_position = 0
            if self.surface.fallback is None or self.surface.fallback.isNull():
                self.surface.fallback = document_pixmap(Path(self.path).suffix, 110)
            self.surface.update()
        self.status_label.setText(message if missing else "此媒体暂无法预览，可使用默认应用打开。")
        self.status_label.setToolTip(str(message or "系统暂不支持此媒体格式"))
        self.status_label.show()

    def _set_duration(self, duration):
        previous = self.seek_slider.blockSignals(True)
        self.seek_slider.setRange(0, max(0, min(2147483647, int(duration))))
        self.seek_slider.blockSignals(previous)
        self._set_position(self.player.position() if self.player is not None else 0)

    def _set_position(self, position):
        if not self.seek_slider.isSliderDown():
            previous = self.seek_slider.blockSignals(True)
            self.seek_slider.setValue(int(position))
            self.seek_slider.blockSignals(previous)
        self.duration_label.setText(f"{_clock(position)} / {_clock(self.seek_slider.maximum())}")

    def _seek(self, position):
        if self.player is not None and not self._failed and self._ensure_source_available() and self.player.isSeekable():
            self._seek_frame_pending = self._video
            self.player.setPosition(int(position))

    def _keyboard_seek(self, position):
        if not self.seek_slider.isSliderDown():
            self._seek(position)

    def toggle_playback(self):
        if not self.path or not self._ensure_source_available() or self._failed:
            return
        if self.player is None:
            self._load_player(play=True)
            return
        if self._user_playing and self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._user_playing = False
            self._play_when_ready = False
            self.player.pause()
        else:
            self._poster_pending = False
            self._poster_timer.stop()
            self._play_when_ready = True
            if self._loaded:
                self._start_playback()
        if self.player is not None:
            self._state_changed(self.player.playbackState())

    def _start_playback(self):
        if self.player is None or not self._ensure_source_available():
            return
        self._play_when_ready = False
        self._poster_pending = False
        self._poster_timer.stop()
        self._user_playing = True
        player = self.player
        self.audio_output.setMuted(self._muted or self.volume_slider.value() == 0)
        if self._resume_position:
            position, self._resume_position = self._resume_position, 0
            player.setPosition(position)
        player.play()
        if self.player is player:
            self._state_changed(player.playbackState())

    def _state_changed(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState and self._user_playing and not self._poster_pending
        self.play_button.set_label("pause" if playing else "play", "暂停" if playing else "播放")
        self._report_playing(playing)

    def _report_playing(self, playing):
        if self._reported_playing != playing:
            self._reported_playing = playing
            self.playing_changed.emit(playing)

    def toggle_mute(self):
        self._muted = not self._muted
        self._volume_changed(self.volume_slider.value())

    def _volume_changed(self, volume):
        muted = self._muted or volume == 0
        self.mute_button.set_label("mute" if muted else "volume", "取消静音" if muted else "静音")
        if self.audio_output is not None:
            self.audio_output.setVolume(volume / 100)
            self.audio_output.setMuted(muted or not self._user_playing or self._poster_pending)

    def open_external(self):
        if self.path and self._ensure_source_available():
            self.stop()
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.path))

    def stop(self, *, remember_position=True):
        """Release all native handles, remembering only path/CPU poster state.

        A subsequent explicit Play lazily creates a new player. Use
        release_source() when the item itself is removed from the shelf.
        """
        self._generation += 1
        self._poster_pending = self._user_playing = False
        self._play_when_ready = False
        self._seek_frame_pending = False
        self._poster_timer.stop()
        if self.player is not None and remember_position and self._source_exists(self.path):
            self._resume_position = self.player.position()
        self._dispose_player()
        self._loaded = False
        self.seek_slider.setEnabled(False)
        self.play_button.setEnabled(bool(self.path) and not self._failed and Path(self.path).is_file())
        self.play_button.set_label("play", "播放")
        self._report_playing(False)

    def release_source(self):
        """Forget a removed reference permanently; showing cannot reopen it."""
        self.stop()
        self.path = ""
        self._video = self._failed = self._poster_sent = False
        self._resume_position = 0
        self.surface.image = QImage()
        self.surface.fallback = None
        self.surface.update()
        self._set_duration(0)
        self.play_button.setEnabled(False)
        self.open_button.setEnabled(False)
        self.status_label.clear()
        self.status_label.hide()

    def showEvent(self, event):
        super().showEvent(event)
        if self.path and not self._ensure_source_available():
            return
        if self.player is not None and self._video and self.surface.image.isNull() and not self._failed:
            self._poster_pending = True
            self._begin_poster()

    def hideEvent(self, event):
        self.stop()
        super().hideEvent(event)

    def closeEvent(self, event):
        self.stop()
        super().closeEvent(event)
