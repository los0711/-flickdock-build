"""Bounded sender-only clipboard results; one nonactivating native window."""
import math,time
from pathlib import Path
from PySide6.QtCore import QPoint,Qt,QTimer,QThreadPool
from PySide6.QtGui import QGuiApplication,QPainter,QPixmap,QImage
from PySide6.QtWidgets import QWidget,QFrame,QHBoxLayout,QLabel,QVBoxLayout
from shiboken6 import isValid
from platform_native import keep_tool_visible_when_inactive


class _NoticeCard(QFrame):
    def __init__(self,parent):
        super().__init__(parent);self.setFixedSize(296,68)
        row=QHBoxLayout(self);row.setContentsMargins(12,10,12,10);row.setSpacing(10)
        self.symbol=QLabel(self);self.symbol.setFixedSize(34,34);self.symbol.setAlignment(Qt.AlignCenter);row.addWidget(self.symbol)
        labels=QVBoxLayout();labels.setSpacing(3);labels.setContentsMargins(0,0,0,0)
        self.message=QLabel(self);self.preview=QLabel(self)
        for label in (self.message,self.preview):label.setTextFormat(Qt.PlainText)
        labels.addWidget(self.message);labels.addWidget(self.preview);row.addLayout(labels,1)
        self.hide()


def _notice_thumbnail(value,cancel):
    """Metadata and image IO never run on the GUI drop/copy path."""
    from clipboard_preview import read_clip_image
    image=read_clip_image(value,34,cancel);size=None
    if not cancel.is_set() and value and value[0]=='files' and value[1]:
        try:size=Path(value[1][0]).stat().st_size
        except OSError:pass
    return image,size


class ClipboardNotice(QWidget):
    """Three overlapping cards and one fading ghost, painted in one tool window."""
    lifetime=2.2
    def __init__(self,manager):
        super().__init__();self.manager=manager;self.generation=0;self.jobs=set();self._job=None;self._job_token=None
        self._thumbnail_pending=None;self._cards=[];self._pending_result=None;self._hiding=False;self._visual_hide=False
        self.setWindowFlags(Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint|Qt.WindowDoesNotAcceptFocus|Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_ShowWithoutActivating);self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents);keep_tool_visible_when_inactive(self)
        self.setFixedSize(296,106)
        self._result_timer=QTimer(self);self._result_timer.setSingleShot(True);self._result_timer.setInterval(160)
        self._result_timer.timeout.connect(self._deliver_result)
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self._expire)
        from clipboard_notice_motion import ClipboardNoticeMotion
        self.motion=ClipboardNoticeMotion(self,self.update,self._release_entry,self._motion_finished)
        self.animation=self.motion.animation
        self._use_card(self._acquire_card())

    def _use_card(self,card):
        self.card=card;self.message=card.message;self.preview=card.preview;self.symbol=card.symbol

    def _acquire_card(self):
        used={entry['card'] for entry in self.motion.active+self.motion.ghosts}
        if len(used)>=4:self.motion.drop_ghosts();used={entry['card'] for entry in self.motion.active}
        for card in self._cards:
            if card not in used:return card
        card=_NoticeCard(self);self._cards.append(card);return card

    def _render(self,entry):
        card=entry['card'];card.ensurePolished();card.layout().activate();entry['cache']=card.grab();self.update()

    def paintEvent(self,event):
        painter=QPainter(self);painter.setCompositionMode(QPainter.CompositionMode_Source)
        painter.fillRect(self.rect(),Qt.transparent);painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        for entry in self.motion.ghosts+self.motion.active:
            painter.setOpacity(max(0.,min(1.,entry['alpha'])));painter.drawPixmap(QPoint(0,round(entry['y'])),entry['cache'])
        painter.end()

    def _release_entry(self,entry):
        if self._job_token==entry['token'] and self._job is not None:self._job.cancel.set()
        if self._thumbnail_pending and self._thumbnail_pending[0]==entry['token']:self._thumbnail_pending=None
        entry['cache']=None

    def _motion_finished(self):
        if not self.motion.active and not self.motion.ghosts:
            if self.isVisible():
                self._visual_hide=True
                try:self.hide()
                finally:self._visual_hide=False
            return
        self._schedule_expiry()

    def _schedule_expiry(self):
        if not self.motion.active:self.timer.stop();return
        deadline=min(entry['deadline'] for entry in self.motion.active)
        self.timer.start(max(1,math.ceil((deadline-time.monotonic())*1000)))

    def _expire(self):
        from native_ui_common import reduced_motion
        now=time.monotonic()
        self.motion.remove(lambda entry:entry['deadline']<=now,animate=not reduced_motion());self._schedule_expiry()

    def dismiss(self):
        from native_ui_common import reduced_motion
        self.timer.stop();self.motion.remove(lambda entry:True,animate=not reduced_motion())

    def supersede(self,*,clear_visible=True):
        self._result_timer.stop();self._pending_result=None;self.generation+=1;self._thumbnail_pending=None
        if self._job is not None:self._job.cancel.set()
        if clear_visible:self.hide();self.motion.clear();self.timer.stop()

    def disable_success(self):
        if self._pending_result is not None and self._pending_result[0]:
            self._result_timer.stop();self._pending_result=None
        self.motion.remove(lambda entry:entry['ok'],animate=False)

    def schedule_result(self,ok,context):
        if not self._permitted(ok):return
        self._pending_result=(ok,dict(context));self._result_timer.start()

    def _permitted(self,ok):
        return (not getattr(self.manager,'_quitting',False)
                and getattr(self.manager.settings,'clipboard_sync_enabled',True)
                and getattr(self.manager,'_clipboard_runtime_enabled',lambda:True)()
                and (not ok or getattr(self.manager.settings,'clipboard_success_notice',True)))

    def _deliver_result(self):
        result,self._pending_result=self._pending_result,None
        if result is not None and self._permitted(result[0]):self.show_result(*result)

    def show_result(self,ok,context):
        if not self._permitted(ok):return
        from native_ui_common import colors,reduced_motion,icon
        c=colors(self.manager.settings.theme);self.generation+=1;token=self.generation
        card=self._acquire_card();self._use_card(card)
        card.setStyleSheet(f"QFrame{{background:{c['surface']};border:1px solid {c['border']};border-radius:8px;}} QLabel{{color:{c['text']};border:0;font-size:11px;background:transparent;}}")
        card.message.setText('复制已同步' if ok else '复制同步失败')
        card.message.setStyleSheet('font-size:12px;font-weight:600;color:'+c['green' if ok else 'red']+';')
        value=context.get('value');kind=value[0] if value else '';data=value[1] if value else None;detail='';glyph='copy'
        if kind=='text':detail=str(data.get('text','')).replace('\n',' ');glyph='web' if detail.startswith(('https://','http://')) else 'text'
        elif kind=='image':detail='图片';glyph='images'
        elif kind=='files' and data:detail=Path(data[0]).name;glyph='file'
        card.preview.setText(card.preview.fontMetrics().elidedText(detail,Qt.ElideRight,216))
        card.symbol.setPixmap(icon(glyph,c['secondary']).pixmap(28,28))
        entry=dict(token=token,ok=bool(ok),card=card,detail=detail,deadline=time.monotonic()+self.lifetime)
        self._render(entry)
        point=QPoint(*context.get('point',(0,0)));screen=QGuiApplication.screenAt(point) or QGuiApplication.primaryScreen()
        area=screen.availableGeometry()
        self.move(max(area.left(),area.right()-self.width()-15),max(area.top(),min(area.top()+16,area.bottom()-self.height()+1)))
        self.motion.append(entry,animate=not reduced_motion());self.show();self.raise_();self._schedule_expiry()
        self._thumbnail_pending=(token,value) if value and kind in ('image','files') else None
        if self._job is not None:self._job.cancel.set()
        self._kick_thumbnail()

    def _kick_thumbnail(self):
        if not isValid(self) or not self.isVisible() or self._job is not None or self._thumbnail_pending is None:return
        token,value=self._thumbnail_pending;self._thumbnail_pending=None
        if not any(entry['token']==token for entry in self.motion.active):return
        from sync_files import DiskWork
        job=DiskWork(_notice_thumbnail,value);self.jobs.add(job);self._job=job;self._job_token=token
        def ready(result,error):
            self.jobs.discard(job)
            if not isValid(self):return
            self._job=None;self._job_token=None
            entry=next((entry for entry in self.motion.active if entry['token']==token),None)
            if entry is not None and not self._permitted(entry['ok']):
                self.supersede();return
            if entry is not None and self.isVisible() and not job.cancel.is_set() and not error:
                image,size=result
                if isinstance(image,QImage) and not image.isNull():entry['card'].symbol.setPixmap(QPixmap.fromImage(image))
                if size is not None:
                    detail=entry['detail']+(f' · {size/1024/1024:.1f} MB' if size>=1024**2 else f' · {size/1024:.1f} KB')
                    label=entry['card'].preview;label.setText(label.fontMetrics().elidedText(detail,Qt.ElideRight,216))
                self._render(entry)
            self._kick_thumbnail()
        job.signals.done.connect(ready);QThreadPool.globalInstance().start(job,10)

    def hideEvent(self,event):
        if not self._hiding:
            self._hiding=True;self.timer.stop();self._thumbnail_pending=None
            if not self._visual_hide:self._result_timer.stop();self._pending_result=None
            if self._job is not None:self._job.cancel.set()
            self.motion.clear();self._hiding=False
        super().hideEvent(event)
