"""Probe the composited desktop pixels on disposable native CI sessions."""
import json
import os
import statistics
import sys
import time


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or sys.platform not in ('win32', 'darwin'):
        raise SystemExit('This probe requires a disposable native CI desktop')
    os.environ['QT_QPA_PLATFORM'] = 'windows' if sys.platform == 'win32' else 'cocoa'
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QColor, QPainter
    from PySide6.QtWidgets import QApplication, QWidget

    class CalibrationWindow(QWidget):
        def __init__(self):
            super().__init__(None, Qt.Tool | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
            self.code = 0

        def paintEvent(self, event):
            painter = QPainter(self)
            painter.fillRect(self.rect(), QColor(48, 97, self.code))
            painter.end()

    app = QApplication([])
    window = CalibrationWindow()
    screen = app.primaryScreen()
    point = screen.availableGeometry().topLeft()
    window.setGeometry(point.x() + 44, point.y() + 44, 96, 96)
    window.show()
    window.raise_()
    origin = screen.geometry().topLeft() if sys.platform == 'win32' else point * 0
    samples = []
    started = time.perf_counter()

    def sample():
        before = time.perf_counter()
        shot = screen.grabWindow(0, window.x() + 40 - origin.x(), window.y() + 40 - origin.y(), 8, 8)
        after = time.perf_counter()
        pixel = shot.toImage().pixelColor(0, 0).getRgb() if not shot.isNull() else None
        samples.append((after - started, 1000 * (after - before), pixel))

    def change():
        window.code = (window.code + 1) % 222
        window.update()

    capture = QTimer(timerType=Qt.PreciseTimer)
    capture.timeout.connect(sample)
    capture.start(max(1, int(420 / screen.refreshRate())))
    pattern = QTimer(timerType=Qt.PreciseTimer)
    pattern.timeout.connect(change)
    pattern.start(max(1, int(1000 / screen.refreshRate())))
    QTimer.singleShot(1700, app.quit)
    app.exec()
    diffs = [stamp for i, (stamp, _, pixel) in enumerate(samples)
             if i and pixel is not None and pixel != samples[i - 1][2]]
    matching = sum(pixel is not None and abs(pixel[0] - 48) <= 8 and abs(pixel[1] - 97) <= 8
                   for _, _, pixel in samples)
    print(json.dumps(dict(platform=app.platformName(), reported_refresh_hz=screen.refreshRate(),
                          screen=screen.geometry().getRect(), scale=screen.devicePixelRatio(),
                          reads=len(samples), matching=matching, actual_pixel_changes=len(diffs),
                          capture_median_ms=statistics.median(s[1] for s in samples),
                          last_pixel=samples[-1][2])))
    if matching < 10:
        raise SystemExit('Screen capture did not contain the calibration surface')


if __name__ == '__main__':
    main()
