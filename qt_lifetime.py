"""Weak QObject callbacks. Live callback failures reach the normal error hook."""
import weakref
from PySide6.QtCore import QTimer
from shiboken6 import isValid


def guard(target, callback, *dependencies):
    references = tuple(weakref.ref(obj) for obj in (target, *dependencies))

    def invoke(*args, **kwargs):
        objects = tuple(reference() for reference in references)
        if all(obj is not None and isValid(obj) for obj in objects):
            return callback(*objects, *args, **kwargs)
    return invoke


def later(milliseconds, target, callback, *dependencies):
    QTimer.singleShot(milliseconds, target, guard(target, callback, *dependencies))
