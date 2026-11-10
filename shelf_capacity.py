"""Cloud shelf capacity presentation; local intake has no cloud byte limit."""
from pathlib import Path
import os
import time
from datetime import datetime,timezone

from PySide6.QtCore import QPoint, QRectF, Qt,QTimer,Signal
from PySide6.QtGui import QColor, QPainter, QPen,QCursor,QGuiApplication
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QLabel, QPushButton,QFrame,QVBoxLayout
from app_settings import ReferenceModalDialog
import ui_theme
from native_ui_common import colors

CLOUD_SHELF_BYTES = 1024**3


def intake_bytes(paths, texts, cancel):
    total = 0
    for ref in dict.fromkeys(paths):
        if cancel.is_set(): raise InterruptedError()
        if ref in texts:
            total += len(texts[ref].encode('utf-8'))
            continue
        path = Path(ref)
        if not path.is_dir():
            total += path.stat().st_size
            continue
        def failed(error): raise error
        for folder, dirs, files in os.walk(path, followlinks=False, onerror=failed):
            dirs[:] = [name for name in dirs if not Path(folder,name).is_symlink() and not Path(folder,name).is_junction()]
            for name in files:
                if cancel.is_set(): raise InterruptedError()
                child = Path(folder,name)
                if not child.is_symlink() and not child.is_junction(): total += child.stat().st_size
    return total


def storage_value(value):
    if (not isinstance(value,dict) or type(value.get('used_bytes')) is not int
            or value['used_bytes'] < 0 or value.get('quota_bytes') != CLOUD_SHELF_BYTES
            or type(value.get('quota_bytes')) is not int):
        raise ValueError('云端搁架容量信息无效。')
    result=dict(used_bytes=value['used_bytes'],quota_bytes=CLOUD_SHELF_BYTES,
                remaining_bytes=max(0,CLOUD_SHELF_BYTES-value['used_bytes']))
    for key in ('reset_at','server_time'):
        raw=value.get(key)
        if not isinstance(raw,str) or len(raw)>40:raise ValueError('云端容量重置时间无效。')
        stamp=datetime.fromisoformat(raw.replace('Z','+00:00'))
        if stamp.tzinfo is None:raise ValueError('云端容量重置时间无效。')
        result[key]=raw
    return result


def byte_label(value):
    if value >= CLOUD_SHELF_BYTES: return f'{value / CLOUD_SHELF_BYTES:.2f} GB'
    if value >= 1024**2: return f'{value / 1024**2:.1f} MB'
    if value >= 1024: return f'{value / 1024:.1f} KB'
    return f'{value} B'


class ShelfCapacityButton(QAbstractButton):
    reset_due=Signal()
    def __init__(self, parent=None):
        super().__init__(parent)
        self.storage = None
        self._sync=None;self._reset_requested=None;self._deadline=None;self._reading=False
        self.popover=QFrame(self,Qt.ToolTip|Qt.FramelessWindowHint)
        self.popover.setAttribute(Qt.WA_ShowWithoutActivating)
        self.popover.setFixedWidth(270)
        box=QVBoxLayout(self.popover);box.setContentsMargins(16,12,16,12);box.setSpacing(8)
        title=QLabel('云端搁架容量',self.popover);box.addWidget(title)
        self.values={}
        for key,name in (('used','已使用'),('remaining','剩余容量'),('total','总容量')):
            row=QHBoxLayout();row.addWidget(QLabel(name,self.popover));value=QLabel(self.popover)
            value.setAlignment(Qt.AlignRight|Qt.AlignVCenter);row.addWidget(value,1);box.addLayout(row);self.values[key]=value
        self.countdown=QLabel(self.popover);self.countdown.setObjectName('capacityCountdown');box.addWidget(self.countdown)
        self.rule=QLabel('北京时间每日 00:00 · 本地文件保留',self.popover);box.addWidget(self.rule)
        self.clock=QTimer(self);self.clock.setInterval(1000);self.clock.timeout.connect(self._tick)
        self.hide_delay=QTimer(self);self.hide_delay.setSingleShot(True);self.hide_delay.timeout.connect(self._hide_outside)
        self.setFixedSize(28,32)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.clicked.connect(self.show_popover)
        self.reset_due.connect(self._refresh_capacity)
        self.set_storage(None)

    def set_storage(self, value):
        if value is None:self._reading=False
        self.storage = storage_value(value) if value is not None else None
        self._deadline=(time.monotonic()+max(0,(datetime.fromisoformat(self.storage['reset_at'].replace('Z','+00:00'))-datetime.fromisoformat(self.storage['server_time'].replace('Z','+00:00'))).total_seconds())) if self.storage else None
        text = ('云端搁架容量尚未读取\n总容量：1 GB\n北京时间每日 0 点重置，本地搁架不自动删除'
                if self.storage is None else
                f"云端搁架\n已使用：{byte_label(self.storage['used_bytes'])}\n剩余容量：{byte_label(self.storage['remaining_bytes'])}\n总容量：1 GB\n北京时间每日 0 点重置，本地搁架不自动删除")
        self.setAccessibleName(text);self._tick();self.update()

    def bind_sync(self,sync):
        if sync is self._sync:return
        if self._sync is not None:self._sync.storage_changed.disconnect(self.set_storage)
        self._sync=sync;self.set_storage(sync.storage);sync.storage_changed.connect(self.set_storage)
    def _refresh_capacity(self):
        if self._sync is None or self._reading:return
        self._reading=True
        def done(value,error):
            self._reading=False
            if error:self._sync.status_changed.emit('读取云端容量失败：'+str(error))
        self._sync.refresh_storage(done)

    def _tick(self):
        self.values['used'].setText(byte_label(self.storage['used_bytes']) if self.storage else '尚未读取')
        self.values['remaining'].setText(byte_label(self.storage['remaining_bytes']) if self.storage else '尚未读取')
        self.values['total'].setText('1 GB')
        seconds=max(0,int(self._deadline-time.monotonic())) if self._deadline is not None else None
        self.countdown.setText('距离云端重置  '+(f'{seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02}' if seconds is not None else '--:--:--'))
        if seconds==0 and self.popover.isVisible() and self._reset_requested!=self.storage['reset_at']:
            self._reset_requested=self.storage['reset_at'];self.reset_due.emit()

    def show_popover(self):
        if self.storage is None:self._refresh_capacity()
        dark=ui_theme.current_theme()=='dark'
        self.popover.setStyleSheet(f"QFrame{{background:{'#29292D' if dark else '#FFFFFF'};border-radius:10px;}} QLabel{{color:{'#E3E6ED' if dark else '#293440'};font-size:11px;background:transparent;border:0;}} QLabel#capacityCountdown{{color:{'#85BFFF' if dark else '#0062CC'};background:{'#1E2D42' if dark else '#E5F0FF'};border-radius:6px;padding:8px;}}")
        self._tick();self.popover.adjustSize();point=self.mapToGlobal(QPoint(0,-self.popover.height()-6))
        screen=self.screen() or QGuiApplication.primaryScreen();area=screen.availableGeometry()
        self.popover.move(max(area.left(),min(point.x(),area.right()-self.popover.width())),max(area.top(),min(point.y(),area.bottom()-self.popover.height())))
        self.popover.show();self.clock.start();self.hide_delay.start(200)

    def enterEvent(self,event):self.show_popover();super().enterEvent(event)
    def leaveEvent(self,event):self.hide_delay.start(200);super().leaveEvent(event)
    def hideEvent(self,event):
        self.clock.stop();self.hide_delay.stop();self.popover.hide();super().hideEvent(event)

    def _hide_outside(self):
        point=QCursor.pos()
        if self.rect().contains(self.mapFromGlobal(point)) or self.popover.frameGeometry().contains(point):
            self.hide_delay.start(200);return
        self.popover.hide();self.clock.stop()

    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        if self.underMouse() or self.hasFocus():
            p.setPen(Qt.NoPen);p.setBrush(QColor('#26384C' if ui_theme.current_theme()=='dark' else '#E2EDFC'))
            p.drawRoundedRect(QRectF(self.rect()),8,8)
        arc=QRectF(5,7,18,18)
        p.setBrush(Qt.NoBrush);p.setPen(QPen(QColor('#3A4657' if ui_theme.current_theme()=='dark' else '#D4DFEE'),2.5))
        p.drawEllipse(arc)
        fraction=min(1,self.storage['used_bytes']/CLOUD_SHELF_BYTES) if self.storage else 0
        pen=QPen(QColor('#0A84FF'),2.5);pen.setCapStyle(Qt.RoundCap);p.setPen(pen)
        if fraction:p.drawArc(arc,90*16,-round(fraction*360*16))


class CloudCapacityNotice(ReferenceModalDialog):
    def __init__(self,manager):
        super().__init__(manager.shelf,surface_width=380,modal=False)
        self.manager=manager;self.paths=[]
        self.setWindowFlag(Qt.WindowStaysOnTopHint,True)
        self.setWindowTitle('云端容量不足')
        box=self.body_layout
        self.message=QLabel('云端容量不足，将无法同步给其他设备',self.body);self.message.setWordWrap(True)
        box.addWidget(self.message)
        line=QHBoxLayout();line.setSpacing(10)
        self.transfer=QPushButton('使用文件互传',self);self.acknowledge=QPushButton('我已知晓',self)
        for control in (self.transfer,self.acknowledge):control.setMinimumHeight(32);line.addWidget(control)
        self.transfer.clicked.connect(self.open_transfer);self.acknowledge.clicked.connect(self.accept)
        box.addLayout(line)

    def open_transfer(self):
        paths=list(self.paths);self.accept()
        self.manager.show_transfer_dialog()
        self.manager.lan_dialog.panel.queue_paths(paths)

    def show_paths(self,paths):
        self.paths=list(dict.fromkeys(paths));c=colors(self.manager.settings.theme)
        self.set_surface_theme(self.manager.settings.theme)
        self.body.setStyleSheet(f"QLabel{{color:{c['text']};font-size:13px;}} QPushButton{{background:{c['hover']};color:{c['text']};border:0;border-radius:8px;padding:6px 12px;}} QPushButton:hover{{background:{c['selected']};}} QPushButton:focus{{border:1px solid {c['blue']};}}")
        self.transfer.setStyleSheet(f"QPushButton{{background:{c['blue']};color:white;}} QPushButton:hover{{background:{c['action_hover']};}}")
        self.adjustSize();anchor=self.manager.shelf.visual_geometry()
        self.move(anchor.center()-self.rect().center());self.show();self.raise_();self.activateWindow()
