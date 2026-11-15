"""Cloud shelf capacity presentation; local intake has no cloud byte limit."""
from pathlib import Path
import os
import time
from datetime import datetime,timezone

from PySide6.QtCore import QPoint, QRectF, Qt,QTimer,Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen,QCursor,QGuiApplication
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QLabel, QPushButton,QFrame,QVBoxLayout
from shelf_intake_limit import TransferSuggestion
import ui_theme
from native_ui_common import (colors, rounded_popup, keyboard_focus_visible, set_ui_weight,
    RADIUS_SUBCONTROL_PX,RADIUS_CONTROL_PX,RADIUS_CARD_PX)

CLOUD_SHELF_BYTES = 1024**3
RESET_NOTICE = '云端副本每日零点清空，本地不受影响。'
CAPACITY_INSET_PX = 8
CAPACITY_RING_SIZE_PX = 18
CAPACITY_LABEL_GAP_PX = 6
CAPACITY_LABEL_SIZE_PX = 11
CAPACITY_POPOVER_TEXT_SIZE_PX = 13
CAPACITY_RULE_SIZE_PX = 12
CAPACITY_HOVER_DELAY_MS = 180
CAPACITY_HOVER_REENTRY_SECONDS = .8


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
    def __init__(self, parent=None, *, show_value=False):
        super().__init__(parent)
        self.show_value=bool(show_value)
        self.storage = None
        self._sync=None;self._reset_requested=None;self._deadline=None;self._reading=False
        self.popover=QFrame(self,Qt.ToolTip|Qt.FramelessWindowHint)
        self.popover.setAttribute(Qt.WA_ShowWithoutActivating)
        rounded_popup(self.popover,RADIUS_CARD_PX)
        self.popover.setFixedWidth(270)
        box=QVBoxLayout(self.popover);box.setContentsMargins(16,12,16,12);box.setSpacing(8)
        title=QLabel('云端搁架容量',self.popover);box.addWidget(title)
        self.values={}
        for key,name in (('used','已使用'),('remaining','剩余容量'),('total','总容量')):
            row=QHBoxLayout();row.addWidget(QLabel(name,self.popover));value=QLabel(self.popover)
            value.setAlignment(Qt.AlignRight|Qt.AlignVCenter);row.addWidget(value,1);box.addLayout(row);self.values[key]=value
        self.countdown=QLabel(self.popover);self.countdown.setObjectName('capacityCountdown');box.addWidget(self.countdown)
        self.rule=QLabel(RESET_NOTICE,self.popover);self.rule.setObjectName('capacityRule');self.rule.setWordWrap(True);box.addWidget(self.rule)
        self.clock=QTimer(self);self.clock.setInterval(1000);self.clock.timeout.connect(self._tick)
        self.hide_delay=QTimer(self);self.hide_delay.setSingleShot(True);self.hide_delay.timeout.connect(self._hide_outside)
        self.hover_delay=QTimer(self);self.hover_delay.setSingleShot(True);self.hover_delay.timeout.connect(self.show_popover)
        self._last_dismissed=0.0
        self.setFixedSize(152 if self.show_value else 28,32)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.clicked.connect(self._open_from_click)
        self.reset_due.connect(self._refresh_capacity)
        self.set_storage(None)

    def set_storage(self, value):
        if value is None:self._reading=False
        self.storage = storage_value(value) if value is not None else None
        self._deadline=(time.monotonic()+max(0,(datetime.fromisoformat(self.storage['reset_at'].replace('Z','+00:00'))-datetime.fromisoformat(self.storage['server_time'].replace('Z','+00:00'))).total_seconds())) if self.storage else None
        text = ('云端搁架容量尚未读取\n总容量：1 GB\n'+RESET_NOTICE
                if self.storage is None else
                f"云端搁架\n已使用：{byte_label(self.storage['used_bytes'])}\n剩余容量：{byte_label(self.storage['remaining_bytes'])}\n总容量：1 GB\n{RESET_NOTICE}")
        self.setAccessibleName(text);self._tick()
        if self.show_value:
            width=(2*CAPACITY_INSET_PX+CAPACITY_RING_SIZE_PX+CAPACITY_LABEL_GAP_PX+
                   QFontMetrics(self._label_font()).horizontalAdvance(self._label_text()))
            self.setFixedWidth(width)
        self.update()

    def _label_font(self):
        font=self.font();font.setPixelSize(CAPACITY_LABEL_SIZE_PX);return set_ui_weight(font,400)

    def _label_text(self):
        return (byte_label(self.storage['remaining_bytes']) if self.storage else '—')+' / 1 GB'

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
        self.countdown.setText('距离云端副本清空  '+(f'{seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02}' if seconds is not None else '--:--:--'))
        if seconds==0 and self.popover.isVisible() and self._reset_requested!=self.storage['reset_at']:
            self._reset_requested=self.storage['reset_at'];self.reset_due.emit()

    def show_popover(self):
        if self.storage is None:self._refresh_capacity()
        dark=ui_theme.current_theme()=='dark';c=colors('dark' if dark else 'light')
        self.popover.setStyleSheet(f"QFrame{{background:{c['card']};border:1px solid {c['border']};border-radius:{RADIUS_CARD_PX}px;}} QLabel{{color:{c['text']};font-size:{CAPACITY_POPOVER_TEXT_SIZE_PX}px;background:transparent;border:0;}} QLabel#capacityRule{{color:{c['secondary']};font-size:{CAPACITY_RULE_SIZE_PX}px;}} QLabel#capacityCountdown{{color:{'#000000' if dark else '#FFFFFF'};background:{'#FFFFFF' if dark else '#000000'};border:0;border-radius:{RADIUS_SUBCONTROL_PX}px;padding:8px;}}")
        self._tick();self.popover.adjustSize();point=self.mapToGlobal(QPoint(0,-self.popover.height()-6))
        screen=self.screen() or QGuiApplication.primaryScreen();area=screen.availableGeometry()
        self.popover.move(max(area.left(),min(point.x(),area.right()-self.popover.width())),max(area.top(),min(point.y(),area.bottom()-self.popover.height())))
        self.popover.show();self.clock.start();self.hide_delay.start(200)

    def _open_from_click(self):
        self.hover_delay.stop();self.show_popover()

    def enterEvent(self,event):
        self.hide_delay.stop()
        if self.popover.isVisible() or time.monotonic()-self._last_dismissed<CAPACITY_HOVER_REENTRY_SECONDS:self.show_popover()
        else:self.hover_delay.start(CAPACITY_HOVER_DELAY_MS)
        super().enterEvent(event)

    def leaveEvent(self,event):
        self.hover_delay.stop()
        if self.popover.isVisible():self.hide_delay.start(200)
        super().leaveEvent(event)

    def focusOutEvent(self,event):
        if self.popover.isVisible():self.hide_delay.start(200)
        super().focusOutEvent(event)

    def hideEvent(self,event):
        self.clock.stop();self.hide_delay.stop();self.hover_delay.stop();self.popover.hide();super().hideEvent(event)

    def _hide_outside(self):
        point=QCursor.pos()
        if self.rect().contains(self.mapFromGlobal(point)) or self.popover.frameGeometry().contains(point):
            self.hide_delay.start(200);return
        if keyboard_focus_visible(self):return
        self._last_dismissed=time.monotonic()
        self.popover.hide();self.clock.stop()

    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        if self.show_value or self.isEnabled() and (self.underMouse() or self.isDown()):
            from reference_widgets import ink
            palette=colors(ui_theme.current_theme())
            fill=ink(palette['control_pressed'] if self.isDown() else palette['control_hover'] if self.underMouse() else palette['control'])
            p.setPen(Qt.NoPen);p.setBrush(fill)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(.5,.5,-.5,-.5),RADIUS_CONTROL_PX,RADIUS_CONTROL_PX)
        if keyboard_focus_visible(self):
            p.setBrush(Qt.NoBrush);p.setPen(QPen(QColor(colors(ui_theme.current_theme())['ring']),1))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1,1,-1,-1),RADIUS_CONTROL_PX,RADIUS_CONTROL_PX)
        arc=QRectF(CAPACITY_INSET_PX if self.show_value else 5,7,CAPACITY_RING_SIZE_PX,CAPACITY_RING_SIZE_PX)
        p.setBrush(Qt.NoBrush);p.setPen(QPen(QColor('#3A3A3A' if ui_theme.current_theme()=='dark' else '#D8D8D8'),2.5))
        p.drawEllipse(arc)
        fraction=self.storage['remaining_bytes']/CLOUD_SHELF_BYTES if self.storage else 0
        pen=QPen(QColor('#0A84FF'),2.5);pen.setCapStyle(Qt.RoundCap);p.setPen(pen)
        if fraction:p.drawArc(arc,90*16,-round(fraction*360*16))
        if self.show_value:
            text=self._label_text();p.setFont(self._label_font())
            p.setPen(QColor(colors(ui_theme.current_theme())['secondary']))
            start=CAPACITY_INSET_PX+CAPACITY_RING_SIZE_PX+CAPACITY_LABEL_GAP_PX
            p.drawText(QRectF(start,0,self.width()-start-CAPACITY_INSET_PX,self.height()),Qt.AlignVCenter|Qt.AlignLeft,text)


class CloudCapacityNotice(TransferSuggestion):
    def __init__(self,manager):
        super().__init__(manager,text='云端容量不足，建议使用文件互传。',
            description='文件保留在本机，无法同步到云端',open_paths=self.open_transfer)
        self.setWindowTitle('云端容量不足')

    def open_transfer(self,paths):
        self.manager.show_transfer_dialog()
        self.manager.lan_dialog.panel.queue_paths(paths)
