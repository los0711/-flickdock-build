"""Bounded disposal of hidden item widgets after a view rebuild."""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid


class WidgetRetirementQueue(QObject):
    """Hide old widgets now and release a small batch on later event turns.

    Retired widgets should belong to the owner's widget tree. The owner then
    also destroys them if it closes permanently before the queue has drained.
    No per-widget signal connection or delayed closure retains a retired item.
    """

    def __init__(self, owner: QWidget, *, interval_ms: int = 8, batch_size: int = 12):
        super().__init__(owner)
        self._pending = deque()
        self._queued_ids = set()
        self._owner_alive = True
        self._batch_size = max(1, int(batch_size))
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(max(1, int(interval_ms)))
        self._timer.timeout.connect(self._drain)
        owner.destroyed.connect(self._owner_destroyed)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def is_idle(self) -> bool:
        """True after all queued widgets have been handed to deleteLater()."""
        return not self._pending

    def retire(self, widget: QWidget) -> bool:
        """Return whether a valid widget was newly queued for disposal."""
        if not self._owner_alive or not isValid(self) or not isValid(widget):
            return False
        key = id(widget)
        if key in self._queued_ids:
            return False
        widget.hide()
        # A hide handler can synchronously dispose its widget or owner.
        if not self._owner_alive or not isValid(self) or not isValid(widget):
            return False
        self._pending.append(widget)
        self._queued_ids.add(key)
        if not self._timer.isActive():
            self._timer.start()
        return True

    def retire_many(self, widgets: Iterable[QWidget]) -> int:
        return sum(self.retire(widget) for widget in widgets)

    def _drain(self):
        for _ in range(min(self._batch_size, len(self._pending))):
            widget = self._pending.popleft()
            self._queued_ids.discard(id(widget))
            if isValid(widget):
                widget.deleteLater()
        if self._pending:
            self._timer.start()

    def _owner_destroyed(self, _owner=None):
        self._owner_alive = False
        if isValid(self._timer):
            self._timer.stop()
        self._pending.clear()
        self._queued_ids.clear()
