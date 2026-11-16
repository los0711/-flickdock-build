"""Screen-edge trigger and reveal geometry in device-independent pixels."""
from PySide6.QtCore import QRect, QSize
from PySide6.QtGui import QRegion


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


def dock_hover_region(surface, trigger, edge):
    surface, trigger = QRect(surface), QRect(trigger)
    region=QRegion(surface)|QRegion(trigger)
    # The bridge occupies only the overlapping span. A bounding rectangle
    # also includes invisible corners above/below a long trigger bar.
    if edge in ('left','right'):
        top,bottom=max(surface.top(),trigger.top()),min(surface.bottom(),trigger.bottom())
        if bottom>=top:
            left,right=min(surface.left(),trigger.left()),max(surface.right(),trigger.right())
            region|=QRegion(QRect(left,top,right-left+1,bottom-top+1))
    else:
        left,right=max(surface.left(),trigger.left()),min(surface.right(),trigger.right())
        if right>=left:
            top,bottom=min(surface.top(),trigger.top()),max(surface.bottom(),trigger.bottom())
            region|=QRegion(QRect(left,top,right-left+1,bottom-top+1))
    return region
