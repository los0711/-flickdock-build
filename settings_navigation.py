"""One scroll document with accessible anchor navigation and scroll tracking."""
from PySide6.QtCore import QEvent, QObject, QSignalBlocker, QEasingCurve
from display_frames import DisplayAnimation
from ui_transitions import motion_allowed, COPY_NOTICE_MORPH_MS

ANCHOR_TOP_INSET_PX = 20
TRACKING_INSET_PX = 28


class SettingsNavigation(QObject):
    def __init__(self, dialog):
        super().__init__(dialog)
        self.dialog = dialog
        self.bar = dialog.scroll.verticalScrollBar()
        self.anchor = None
        self.motion = DisplayAnimation(self)
        self.motion.setDuration(COPY_NOTICE_MORPH_MS)
        self.motion.setEasingCurve(QEasingCurve.OutCubic)
        self.motion.valueChanged.connect(lambda value: self.bar.setValue(round(value)))
        self.motion.finished.connect(self.track)
        self.bar.valueChanged.connect(self.track)
        self.bar.sliderPressed.connect(self._interrupt)
        dialog.scroll.viewport().installEventFilter(self)
        self.bar.installEventFilter(self)

    def go(self, index):
        if not 0 <= index < len(self.dialog.setting_categories):
            return
        self.motion.stop()
        section = self.dialog.setting_categories[index][1][0]
        self.dialog.body_layout.activate()
        target = min(self.bar.maximum(), max(0, section.y() - ANCHOR_TOP_INSET_PX))
        self.anchor = (index,target)
        if motion_allowed() and self.dialog.isVisible():
            self.motion.setStartValue(float(self.bar.value()))
            self.motion.setEndValue(float(target))
            self.motion.start()
        else:
            self.bar.setValue(target)
            self.track()

    def track(self, *_):
        value = self.bar.value()
        if self.anchor is not None:
            index,target=self.anchor
            if self.motion.state()==self.motion.Running or abs(value-target)<=1:
                with QSignalBlocker(self.dialog.category_list):self.dialog.category_list.setCurrentRow(index)
                return
            self.anchor=None
        index = 0
        for number, (_, sections) in enumerate(self.dialog.setting_categories):
            if sections[0].y() <= value + TRACKING_INSET_PX:
                index = number
        if self.bar.maximum() and value >= self.bar.maximum():
            index = len(self.dialog.setting_categories) - 1
        with QSignalBlocker(self.dialog.category_list):
            self.dialog.category_list.setCurrentRow(index)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Wheel, QEvent.TouchBegin, QEvent.MouseButtonPress, QEvent.KeyPress):
            self._interrupt()
        return False

    def _interrupt(self):
        self.motion.stop()
        self.anchor=None
