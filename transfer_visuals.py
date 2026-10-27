"""Lightweight progress artwork; repainting never rebuilds shelf layouts."""
import math
import time
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QLabel, QWidget
import ui_theme


CLOUD_STATUS_LABELS = {
    'synced': '已同步到云端',
    'uploading': '正在上传到云端',
    'pending': '等待上传到云端',
    'local': '仅保存在本机',
    'paused': '云端上传已暂停',
    'error': '云端上传失败',
}
_CLOUD_STATUS_COLORS = {
    'synced': '#25966c', 'uploading': '#287fe2', 'pending': '#5684bd',
    'local': '#8391a3', 'paused': '#bd822e', 'error': '#d04c59',
}
_CLOUD_STATUS_DARK_COLORS = {
    'synced': '#67d7a4', 'uploading': '#75b7ff', 'pending': '#9dc5f7',
    'local': '#b6c5d8', 'paused': '#efbb70', 'error': '#ff8d97',
}


def cloud_status_for_ref(shelf, ref):
    sync = getattr(getattr(shelf, 'manager', None), 'sync', None)
    lookup = getattr(sync, 'sync_status_for_ref', None)
    return lookup(ref) if callable(lookup) and ref else 'local'


def draw_cloud_status_badge(painter, rect, status, transfer=None):
    """Circular state glyph, placed at the lower-right of file artwork."""
    status = status if status in CLOUD_STATUS_LABELS else 'local'
    rect = QRectF(rect)
    ink_hex = (_CLOUD_STATUS_DARK_COLORS if ui_theme.current_theme() == 'dark'
               else _CLOUD_STATUS_COLORS)[status]
    ink = QColor(ink_hex)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.translate(rect.left(),rect.top())
    painter.scale(rect.width()/20,rect.height()/20)
    painter.setBrush(QColor(ui_theme.color('surface')))
    outline = QPen(ink,1.2)
    painter.setPen(outline)
    painter.drawEllipse(QRectF(1.2,1.2,17.6,17.6))
    fraction=transfer_fraction(transfer) if transfer else None
    if fraction is not None and fraction>0:
        arc=QPen(ink,2.2)
        arc.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(arc)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawArc(QRectF(1.2,1.2,17.6,17.6),90*16,-round(max(.025,fraction)*360*16))
    pen = QPen(ink, 1.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if status == 'synced':
        painter.drawLine(QPointF(5.5,10),QPointF(8.5,13))
        painter.drawLine(QPointF(8.5,13),QPointF(14.5,6.8))
    elif status == 'uploading':
        painter.drawLine(QPointF(10,14.2),QPointF(10,6.2))
        painter.drawLine(QPointF(6.7,9.4),QPointF(10,6.2))
        painter.drawLine(QPointF(10,6.2),QPointF(13.3,9.4))
    elif status == 'pending':
        painter.drawEllipse(QRectF(5.6,5.6,8.8,8.8))
        painter.drawLine(QPointF(10,7.4),QPointF(10,10.2))
        painter.drawLine(QPointF(10,10.2),QPointF(12,11.5))
    elif status == 'local':
        painter.drawLine(QPointF(6,10),QPointF(14,10))
    elif status == 'paused':
        painter.drawLine(QPointF(7.5,6.5),QPointF(7.5,13.5))
        painter.drawLine(QPointF(12.5,6.5),QPointF(12.5,13.5))
    elif status == 'error':
        painter.drawLine(QPointF(10,6),QPointF(10,11.3))
        painter.drawPoint(QPointF(10,14))
    painter.restore()


class CloudStatusBadge(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.status = 'local'
        self.transfer = None
        self.setFixedSize(20, 20)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.set_status('local')

    def set_status(self, status, transfer=None):
        status = status if status in CLOUD_STATUS_LABELS else 'local'
        if status == self.status and transfer == self.transfer and self.toolTip(): return
        self.status = status
        self.transfer = dict(transfer) if transfer else None
        self.setAccessibleName(CLOUD_STATUS_LABELS[status])
        self.setToolTip(CLOUD_STATUS_LABELS[status])
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        draw_cloud_status_badge(painter, self.rect(), self.status, self.transfer)


def transfer_fraction(state):
    """Return a known progress fraction; unknown sizes never imply a percentage."""
    try:
        total=max(0,int(state.get('total') or 0))
        received=max(0,int(state.get('received') or 0))
    except (TypeError,ValueError):
        return None
    return min(1,received/total) if total else None


def transfer_phase_label(state):
    phase=state.get('phase')
    return ('正在复制' if phase=='copying' else '正在下载' if phase=='downloading'
            else '等待云端确认' if phase=='verifying'
            else '等待重试' if phase=='retrying'
            else '校验原文件' if phase=='validating'
            else '等待上传' if phase=='queued' else '准备上传' if phase=='preparing'
            else '正在上传')


class TransferRateTracker:
    """Estimate remaining time from observed bytes, with a smoothed rate.

    A transfer or aggregate gets no ETA until its identity, total size and a
    substantial timed progress sample are known. Stalled work loses its ETA.
    """
    SAMPLE_SECONDS=.5
    SAMPLE_BYTES=64*1024
    STALE_SECONDS=6.0

    def __init__(self,clock=None):
        self._samples={}
        self._aggregate_key=None
        self._clock=clock or time.monotonic

    @staticmethod
    def _numbers(state):
        try:return max(0,int(state.get('received') or 0)),max(0,int(state.get('total') or 0))
        except (TypeError,ValueError):return 0,0

    def _observe_one(self,key,received,total,now,phase='uploading'):
        previous=self._samples.get(key)
        if (total<=0 or previous is None or previous['total']!=total
                or previous['phase']!=phase or received<previous['received']
                or (received>previous['received'] and now-previous['last_progress']>self.STALE_SECONDS)):
            self._samples[key]=dict(received=received,total=total,at=now,rate=None,
                                    last_progress=now,phase=phase)
            return
        delta=received-previous['received'];elapsed=now-previous['at']
        if delta>0:
            previous['last_progress']=now
            if elapsed>=self.SAMPLE_SECONDS and delta>=self.SAMPLE_BYTES:
                measured=delta/elapsed
                old=previous['rate']
                previous['rate']=measured if old is None else old*.65+measured*.35
                previous['received']=received;previous['at']=now

    def observe(self,transfers,now=None):
        if now is None:now=self._clock()
        refs=set(transfers)
        for key in tuple(self._samples):
            if key!='__aggregate__' and key not in refs:self._samples.pop(key,None)
        for ref,state in transfers.items():
            phase=state.get('phase')
            if phase in ('queued','preparing'):
                self._samples.pop(ref,None)
                continue
            received,total=self._numbers(state)
            self._observe_one(ref,received,total,now,phase)
        aggregate_key=tuple(sorted(refs))
        if aggregate_key!=self._aggregate_key:
            self._samples.pop('__aggregate__',None)
            self._aggregate_key=aggregate_key
        if (refs and any(state.get('phase') not in ('queued','preparing') for state in transfers.values())
                and all(self._numbers(state)[1]>0 for state in transfers.values())):
            received=sum(self._numbers(state)[0] for state in transfers.values())
            total=sum(self._numbers(state)[1] for state in transfers.values())
            self._observe_one('__aggregate__',received,total,now)
        else:self._samples.pop('__aggregate__',None)

    def remaining_text(self,ref,state,now=None):
        if now is None:now=self._clock()
        phase=state.get('phase')
        if phase=='queued':return '等待开始'
        if phase=='preparing':return '准备中'
        if phase=='verifying':return '等待云端确认'
        if phase=='retrying':return '等待重试'
        if phase=='validating':return '校验原文件'
        received,total=self._numbers(state)
        if total<=0:return '计算中'
        if received>=total:return '正在完成'
        sample=self._samples.get(ref)
        if not sample or not sample['rate']:return '计算中'
        if now-sample['last_progress']>self.STALE_SECONDS:return '等待传输'
        seconds=(total-received)/sample['rate']
        if not math.isfinite(seconds) or seconds<=0:return '计算中'
        if seconds<60:return f'剩余约 {max(1,math.ceil(seconds))} 秒'
        if seconds<3600:return f'剩余约 {math.ceil(seconds/60)} 分钟'
        return f'剩余约 {math.ceil(seconds/3600)} 小时'


def draw_ring(painter,rect,state):
    if not state: return
    rect=QRectF(rect).adjusted(3,3,-3,-3)
    painter.save();painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen=QPen(QColor(ui_theme.color('border')),2.4);painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush);painter.drawEllipse(rect)
    pen.setColor(QColor('#3989ef'));pen.setCapStyle(Qt.PenCapStyle.RoundCap);painter.setPen(pen)
    received,total=state.get('received',0),state.get('total',0)
    if total>0:
        angle=-max(.015,min(1,received/total))*5760
        painter.drawArc(rect,90*16,round(angle))
    else:
        start=round((time.monotonic()*240%360)*16)
        painter.drawArc(rect,start,-100*16)
    painter.restore()


def draw_progress_badge(painter,rect,state):
    if not state:return
    rect=QRectF(rect)
    painter.save();painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(ui_theme.color('border')),0.8))
    painter.setBrush(QColor(ui_theme.color('surface')))
    painter.drawEllipse(rect)
    painter.restore()
    draw_ring(painter,rect,state)


class TransferIconLabel(QLabel):
    def __init__(self,repository,ref,parent=None):
        super().__init__(parent);self.repository,self.ref=repository,ref
        self.cloud_status='local'

    def set_cloud_status(self,status):
        status=status if status in CLOUD_STATUS_LABELS else 'local'
        if status!=self.cloud_status:
            self.cloud_status=status
            self.update()

    def paintEvent(self,event):
        state=self.repository.transfers.get(self.ref)
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        pixmap=self.pixmap()
        if pixmap and not pixmap.isNull():
            painter.drawPixmap(0,0,28,28,pixmap)
        draw_cloud_status_badge(painter,QRectF(self.width()-18,self.height()-18,18,18),
                                self.cloud_status,state)


class TransferOverlay(QWidget):
    def __init__(self,parent,state):
        super().__init__(parent);self.state=state
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.hide()

    def paintEvent(self,event):
        state=self.state()
        if not state:return
        painter=QPainter(self)
        side=30
        rect=QRectF(self.width()-side-14,self.height()-side-14,side,side)
        draw_progress_badge(painter,rect,state)


class UploadCountBadge(QWidget):
    """Compact header feedback for outstanding uploads."""
    def __init__(self,parent=None):
        super().__init__(parent)
        self.count=0
        self.complete=False
        self.setFixedSize(26,26)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self._finish_timer=QTimer(self)
        self._finish_timer.setSingleShot(True)
        self._finish_timer.timeout.connect(self.hide)
        self.hide()

    def set_count(self,count):
        count=max(0,int(count))
        if count==self.count:return
        previous=self.count
        self.count=count
        self._finish_timer.stop()
        self.complete=previous>0 and count==0
        if count or self.complete:
            self.show();self.raise_();self.update()
        else:self.hide()
        if self.complete:self._finish_timer.start(1400)

    def paintEvent(self,event):
        painter=QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#22a46f' if self.complete else '#e4f1ff'))
        painter.drawEllipse(QRectF(1,1,24,24))
        if self.complete:
            painter.setPen(QPen(QColor('white'),2.2,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
            painter.drawLine(7,13,11,17)
            painter.drawLine(11,17,19,9)
        else:
            painter.setPen(QColor('#146bc8'))
            font=painter.font();font.setPointSize(9);font.setBold(True);painter.setFont(font)
            painter.drawText(self.rect(),Qt.AlignmentFlag.AlignCenter,'99+' if self.count>99 else str(self.count))
