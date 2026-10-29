"""Non-interactive desktop preview for screen-edge settings drafts."""
from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QWidget

from dock_geometry import trigger_rect
import ui_theme


class DockPreview(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus |
                         Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setWindowTitle('FlickDock edge preview')
        self.target_rect = QRect()
        self.badge_rect = QRect()
        self.values = {}
        self.caption = ''

    def show_preview(self, area, values):
        self.values = dict(values)
        area = QRect(area)
        edge = values['dock_edge']
        self.target_rect = trigger_rect(area, edge, values['dock_position'],
                                        values['dock_length'], values['dock_thickness'])
        self.caption = ('触发区域预览' if values['trigger_visible'] else '触发区域 · 已隐藏标记')
        self.caption += f"  {values['dock_length']} × {values['dock_thickness']} px"
        width = QFontMetrics(self.font()).horizontalAdvance(self.caption) + 28
        width = min(width, area.width() - 12)
        height = 34
        target = self.target_rect
        if edge == 'right':
            x, y = target.left() - width - 12, target.center().y() - height // 2
        elif edge == 'left':
            x, y = target.right() + 12, target.center().y() - height // 2
        elif edge == 'top':
            x, y = target.center().x() - width // 2, target.bottom() + 12
        else:
            x, y = target.center().x() - width // 2, target.top() - height - 12
        x = max(area.left() + 6, min(x, area.right() - width - 5))
        y = max(area.top() + 6, min(y, area.bottom() - height - 5))
        self.badge_rect = QRect(x, y, width, height)
        self.setGeometry(self.target_rect.united(self.badge_rect).adjusted(-2, -2, 2, 2))
        self.show()
        self.raise_()
        self.update()

    def paintEvent(self, event):
        if self.target_rect.isEmpty():
            return
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.GlobalColor.transparent)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        origin = self.geometry().topLeft()
        target = QRectF(self.target_rect.translated(-origin))
        tint = QColor(self.values['dock_color'])
        tint.setAlpha(round(255 * self.values['dock_opacity'] / 100))
        if not self.values['trigger_visible']:
            tint.setAlpha(0)
        p.setBrush(tint)
        outline = QColor(self.values['dock_color']);outline.setAlpha(255)
        p.setPen(QPen(outline, 1.5, Qt.PenStyle.DashLine))
        p.drawRoundedRect(target.adjusted(.75, .75, -.75, -.75), 3, 3)
        badge = QRectF(self.badge_rect.translated(-origin))
        p.setPen(QPen(QColor(ui_theme.color('border')), 1))
        p.setBrush(QColor(ui_theme.color('surface')))
        p.drawRoundedRect(badge.adjusted(.5, .5, -.5, -.5), 10, 10)
        p.setPen(QColor(ui_theme.color('text')))
        p.drawText(badge, Qt.AlignmentFlag.AlignCenter, self.caption)
        p.end()
