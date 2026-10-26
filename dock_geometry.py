"""Screen-edge trigger and reveal geometry in device-independent pixels."""
from PySide6.QtCore import QRect, QSize


def trigger_rect(area, edge='right', position=8, length=76, thickness=8):
    area = QRect(area)
    vertical = edge in ('left', 'right')
    along = area.height() if vertical else area.width()
    across = area.width() if vertical else area.height()
    length = max(1, min(int(length), along))
    thickness = max(1, min(int(thickness), across))
    start = round((along - length) * max(0, min(100, position)) / 100)
    if edge == 'left':
        return QRect(area.left(), area.top() + start, thickness, length)
    if edge == 'top':
        return QRect(area.left() + start, area.top(), length, thickness)
    if edge == 'bottom':
        return QRect(area.left() + start, area.bottom() - thickness + 1, length, thickness)
    return QRect(area.right() - thickness + 1, area.top() + start, thickness, length)


def reveal_rect(area, trigger, size, edge='right'):
    area, trigger, size = QRect(area), QRect(trigger), QSize(size)
    width, height = min(size.width(), area.width()), min(size.height(), area.height())
    x = trigger.center().x() - width // 2
    y = trigger.center().y() - height // 2
    if edge == 'left': x = area.left()
    elif edge == 'top': y = area.top()
    elif edge == 'bottom': y = area.bottom() - height + 1
    else: x = area.right() - width + 1
    x = max(area.left(), min(x, area.right() - width + 1))
    y = max(area.top(), min(y, area.bottom() - height + 1))
    return QRect(x, y, width, height)


def dock_hover_rect(surface, trigger, edge):
    surface, trigger = QRect(surface), QRect(trigger)
    if edge == 'left': surface.setLeft(min(surface.left(), trigger.left()))
    elif edge == 'right': surface.setRight(max(surface.right(), trigger.right()))
    elif edge == 'top': surface.setTop(min(surface.top(), trigger.top()))
    else: surface.setBottom(max(surface.bottom(), trigger.bottom()))
    return surface.united(trigger)
