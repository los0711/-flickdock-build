"""Finite, interruptible switch travel; no idle timer or global event filter."""
from PySide6.QtCore import QCoreApplication, QEvent, QObject
from frame_animation import FrameAnimation as QVariantAnimation
from shiboken6 import isValid


class ToggleMotion(QObject):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner=owner;self.value=float(owner.isChecked())
        self.animation=QVariantAnimation(self)
        self.animation.setDuration(160)
        from ui_transitions import exit_curve
        self.animation.setEasingCurve(exit_curve())
        self.animation.valueChanged.connect(self._frame)
        owner.toggled.connect(self.set_checked);owner.installEventFilter(self)

    def _frame(self,value):
        from native_ui_common import reduced_motion
        if self.animation.state()==self.animation.State.Running and reduced_motion():
            self.animation.stop();value=float(self.owner.isChecked())
        self.value=float(value)
        if isValid(self.owner):self.owner.update()

    def set_checked(self,checked):
        from native_ui_common import reduced_motion
        self.animation.stop();target=float(bool(checked))
        if not self.owner.isVisible() or reduced_motion():self._frame(target);return
        if self.value==target:return
        self.animation.setStartValue(self.value);self.animation.setEndValue(target)
        self.animation.start()

    def eventFilter(self,watched,event):
        owner=getattr(self,'owner',None)
        if owner is None or not hasattr(self,'animation'):return False
        if QCoreApplication.closingDown() or not isValid(owner):return False
        if event.type() in (QEvent.Hide,QEvent.Show,QEvent.EnabledChange):
            self.animation.stop();self._frame(float(self.owner.isChecked()))
        return False
