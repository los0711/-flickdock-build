"""Qt rendering for the two supplied, single-path Lottie animations."""
from dataclasses import dataclass
from time import perf_counter

from PySide6.QtCore import QPointF, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from display_frames import FramePulse
from native_ui_common import colors, reduced_motion
import ui_theme


@dataclass(frozen=True)
class Key:
    frame: int
    value: float
    curve: tuple = (.42, 0., .58, 1.)
    hold: bool = False


def _bezier(t, first, second):
    inverse = 1. - t
    return 3 * inverse * inverse * t * first + 3 * inverse * t * t * second + t ** 3


def _value(keys, frame):
    if frame <= keys[0].frame:
        return keys[0].value
    for begin, end in zip(keys, keys[1:]):
        if frame < end.frame:
            if begin.hold:
                return begin.value
            fraction = (frame - begin.frame) / (end.frame - begin.frame)
            x1, y1, x2, y2 = begin.curve
            lo, hi = 0., 1.
            for _ in range(10):
                middle = (lo + hi) / 2
                if _bezier(middle, x1, x2) < fraction:
                    lo = middle
                else:
                    hi = middle
            progress = _bezier((lo + hi) / 2, y1, y2)
            return begin.value + progress * (end.value - begin.value)
    return keys[-1].value


def _path(coordinates):
    path = QPainterPath()
    path.moveTo(*coordinates[0])
    for x, y in coordinates[1:]:
        path.lineTo(x, y)
    path.closeSubpath()
    return path


# Geometry and keyframes from assets/motion/zap.json and navigation.json.
ZAP = _path(((13, 2), (3, 14), (12, 14), (11, 22), (21, 10), (12, 10)))
NAVIGATION = _path(((3, 11), (22, 2), (13, 21), (11, 13)))
_ZAP_ROTATION = tuple(Key(t, v) for t, v in ((0, 0), (10, 0), (12, -5), (14, 4), (16, -2), (18, 0)))
_ZAP_FLASH = tuple(Key(t, v, hold=True) for t, v in ((0, 1), (2, .15), (4, 1), (6, .15), (8, 1)))
_ZAP_SCALE = (Key(6, 1., (0., 0., .58, 1.)), Key(10, .88, (0., 0., .58, 1.)),
              Key(16, 1.144), Key(22, .964), Key(28, 1.))
_NAVIGATION_OFFSET = (
    Key(0, 0., (0., 0., .58, 1.)), Key(5, -2.16, (.42, 0., 1., 1.)), Key(13, 27., hold=True),
    Key(14, -27., (0., 0., .58, 1.)), Key(25, 1.35), Key(30, 0.)
)


def _draw(painter, rect, path, frame, tint):
    painter.save()
    painter.setClipRect(rect)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.translate(rect.center())
    ratio = min(rect.width(), rect.height()) / 32
    painter.scale(ratio, ratio)
    if path is ZAP:
        painter.setOpacity(painter.opacity() * _value(_ZAP_FLASH, frame))
        painter.rotate(_value(_ZAP_ROTATION, frame))
        scale = _value(_ZAP_SCALE, frame)
        painter.scale(scale, scale)
    else:
        offset = _value(_NAVIGATION_OFFSET, frame)
        painter.translate(offset, -offset)
    painter.translate(-12, -12)
    painter.setPen(QPen(QColor(tint), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    painter.drawPath(path)
    painter.restore()


class LiveTranslationMark(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedSize(19, 19)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAccessibleName('自动翻译运行中')
        self.started = None
        self.running = False
        self.pulse = FramePulse(self, follow_refresh_rate=True)
        self.pulse.timeout.connect(self._tick)
        self.settled = QTimer(self, singleShot=True, interval=1200)
        self.settled.timeout.connect(self.finish)
        self.repeat = QTimer(self, interval=3000, timerType=Qt.PreciseTimer)
        self.repeat.timeout.connect(self._play)
        self.hide()

    def set_running(self, running):
        self.running = bool(running)
        self.setVisible(self.running)

    def showEvent(self, event):
        super().showEvent(event)
        if self.running and not reduced_motion():
            self._play()
            self.repeat.start()

    def hideEvent(self, event):
        self.repeat.stop()
        self.finish()
        super().hideEvent(event)

    def _play(self):
        if not self.isVisible() or reduced_motion():
            return
        self.started = perf_counter()
        self.pulse.start()
        self.settled.start()

    def _tick(self):
        if self.started is not None and perf_counter() - self.started >= 34 / 30:
            self.finish()
        else:
            self.update()

    def finish(self):
        self.settled.stop()
        self.pulse.stop()
        self.started = None
        self.update()

    def paintEvent(self, event):
        with QPainter(self) as painter:
            frame = 34 if self.started is None else (perf_counter() - self.started) * 30
            tint = colors(ui_theme.current_theme())['action']
            _draw(painter, QRectF(self.rect()), ZAP, frame, tint)


class LiveTranslationNotice(QWidget):
    DURATION = 1.63

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAccessibleName('自动翻译已开启')
        self.started = None
        self.pulse = FramePulse(self, follow_refresh_rate=True)
        self.pulse.timeout.connect(self._tick)
        self.deadline = QTimer(self, singleShot=True, interval=1700)
        self.deadline.timeout.connect(self.hide)
        self.hide()

    def present(self):
        self.started = perf_counter()
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()
        if not reduced_motion():
            self.pulse.start()
        self.deadline.start()

    def _tick(self):
        if self.started is None or perf_counter() - self.started > self.DURATION:
            self.hide()
            return
        self.update()

    def hideEvent(self, event):
        self.pulse.stop()
        self.deadline.stop()
        self.started = None
        super().hideEvent(event)

    def paintEvent(self, event):
        if self.started is None:
            return
        elapsed = perf_counter() - self.started
        still = reduced_motion()
        entrance = 1. if still else 1 - (1 - min(1., elapsed / .19)) ** 3
        exit_alpha = 1. if still else min(1., max(0., (self.DURATION - elapsed) / .22))
        rect = QRectF(self.rect())
        with QPainter(self) as painter:
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setOpacity(entrance * exit_alpha)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 189))
            painter.drawRoundedRect(rect.adjusted(.5, .5, -.5, -.5), 27, 27)
            y = rect.center().y()
            icon_rect = QRectF(rect.center().x() - 42, y - 91, 84, 84)
            _draw(painter, icon_rect, NAVIGATION, 40 if still else elapsed * 30, colors(ui_theme.current_theme())['action'])
            painter.translate(0, 0 if still else 9 * (1 - entrance))
            font = self.font()
            font.setPixelSize(23)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor('#F5F6F8'))
            painter.drawText(QRectF(12, y + 2, rect.width() - 24, 37), Qt.AlignCenter, '自动翻译已开启')
            font.setPixelSize(13)
            font.setBold(False)
            painter.setFont(font)
            painter.setPen(QColor('#D2D5DD'))
            painter.drawText(QRectF(12, y + 46, rect.width() - 24, 26), Qt.AlignCenter,
                             '在输入框里输入文字时，会自动显示实时译文。')
