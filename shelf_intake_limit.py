"""Nonmodal transfer recommendations using the shared copy-notice capsule."""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor,QGuiApplication
from flickdock_icons import document_pixmap
from intake_notice import (ACTION_PROMPT_DURATION_MS,CapsuleNotice,LOGO_SIZE_PX,
                           NOTICE_ACTION_COLOR,paint_circular_artwork)
from native_ui_common import icon


class TransferSuggestion(CapsuleNotice):
    HOST_WIDTH=352
    MAX_CAPSULE_WIDTH=328
    NOTICE_ORDER=3

    def __init__(self,manager,*,text,description,open_paths):
        super().__init__(manager);self.paths=[];self._open_paths=open_paths;self._art=None
        self._text=text;self._description=description;self._phase='suggestion'
        self._surface_color='#18181B';self._hovered=False;self.setMouseTracking(True)
        self.setWindowTitle('FlickDock · 文件互传建议')
        self.setToolTip('点击使用文件互传')

    def _paint_leading(self,painter,rect):
        if self._art is None:return super()._paint_leading(painter,rect)
        paint_circular_artwork(painter,rect,self._art,'dark',self._artwork_cache)

    def _paint_indicator(self,painter,rect,colors):
        circle=rect.adjusted(3,3,-3,-3)
        painter.save();painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(colors['action_pressed'] if self._pressed else colors['action_hover'] if self._hovered else colors['action']))
        painter.drawEllipse(circle)
        icon('right',colors['on_action']).paint(painter,circle.adjusted(4,4,-4,-4).toAlignedRect());painter.restore()

    def enterEvent(self,event):
        self._hovered=True;self.update();super().enterEvent(event)

    def leaveEvent(self,event):
        self._hovered=False;self.update();super().leaveEvent(event)

    def _sync_accessibility(self):
        self.setAccessibleName(self._text)
        self.setAccessibleDescription(self._description+'；点击使用文件互传')

    def show_paths(self,paths):
        self.paths=list(dict.fromkeys(paths))
        if not self.paths:return
        suffix=Path(self.paths[0]).suffix or 'file'
        self._art=document_pixmap(suffix,LOGO_SIZE_PX,dpr=self.devicePixelRatioF()).toImage()
        if not self.isVisible() or self._hiding:
            anchor=self.manager.shelf.visual_geometry()
            screen=QGuiApplication.screenAt(anchor.center()) or QGuiApplication.primaryScreen()
            self._begin_presentation(screen.availableGeometry())
        self.timer.start(ACTION_PROMPT_DURATION_MS);self.update()

    def _activate(self):
        if self._hiding or not self.paths:return
        paths=list(self.paths);self.dismiss();self._open_paths(paths)


def make_notice(manager):
    def open_transfer(paths):
        manager.show_transfer_dialog();manager.lan_dialog.panel.queue_paths(paths)
    return TransferSuggestion(manager,text='较大文件，建议使用文件互传。',
        description='文件已保留在搁架，可使用文件互传发送',open_paths=open_transfer)
