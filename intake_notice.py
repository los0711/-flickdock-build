"""An expiring, nonactivating cloud-retention hint, independent of upload jobs."""
from PySide6.QtCore import QPoint,Qt,QTimer,Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget,QFrame,QHBoxLayout,QLabel,QPushButton,QVBoxLayout
from platform_native import keep_tool_visible_when_inactive


class ClipboardNotice(QWidget):
    """One reusable sender-only top-right card; it never accepts user input."""
    def __init__(self,manager):
        super().__init__();self.manager=manager;self.generation=0;self.jobs=set();self._closing=False
        self.setWindowFlags(Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint|Qt.WindowDoesNotAcceptFocus|Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_ShowWithoutActivating);self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents);keep_tool_visible_when_inactive(self)
        root=QHBoxLayout(self);root.setContentsMargins(0,0,0,0)
        self.card=QFrame(self);root.addWidget(self.card)
        row=QHBoxLayout(self.card);row.setContentsMargins(14,12,14,12);row.setSpacing(12)
        self.symbol=QLabel(self.card);self.symbol.setFixedSize(38,38);self.symbol.setAlignment(Qt.AlignCenter);row.addWidget(self.symbol)
        labels=QVBoxLayout();labels.setSpacing(4);labels.setContentsMargins(0,0,0,0)
        self.message=QLabel(self.card);self.preview=QLabel(self.card)
        self.message.setTextFormat(Qt.PlainText);self.preview.setTextFormat(Qt.PlainText)
        labels.addWidget(self.message);labels.addWidget(self.preview);row.addLayout(labels,1)
        self.setFixedSize(330,76)
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self.dismiss)
        from PySide6.QtCore import QVariantAnimation,QEasingCurve
        self.animation=QVariantAnimation(self);self.animation.setDuration(200);self.animation.setEasingCurve(QEasingCurve.OutCubic)
        self.animation.valueChanged.connect(self._frame);self.animation.finished.connect(self._finished)

    def _frame(self,value):
        fraction=float(value)
        self.move(round(self._outside.x()+(self._target.x()-self._outside.x())*fraction),self._target.y())
        self.setWindowOpacity(fraction)

    def _finished(self):
        if self._closing:self.hide()
        else:self.timer.start(2200)

    def dismiss(self):
        from native_ui_common import reduced_motion
        if not self.isVisible():return
        self._closing=True;self.animation.stop()
        if reduced_motion():self.hide();return
        self.animation.setStartValue(self.windowOpacity());self.animation.setEndValue(0.);self.animation.start()

    def show_result(self,ok,context):
        from native_ui_common import colors,reduced_motion,icon
        from pathlib import Path
        c=colors(self.manager.settings.theme);self.generation+=1;generation=self.generation
        self.message.setText('复制已同步' if ok else '复制同步失败')
        self.message.setStyleSheet('font-size:12px;font-weight:600;color:'+c['green' if ok else 'red']+';')
        self.card.setStyleSheet(f"QFrame{{background:{c['surface']};border:1px solid {c['border']};border-radius:8px;}} QLabel{{color:{c['text']};border:0;font-size:11px;background:transparent;}}")
        value=context.get('value');kind=value[0] if value else '';data=value[1] if value else None
        detail='';glyph='copy'
        if kind=='text':detail=str(data.get('text','')).replace('\n',' ');glyph='web' if detail.startswith(('https://','http://')) else 'text'
        elif kind=='image':detail='图片';glyph='images'
        elif kind=='files' and data:
            path=Path(data[0]);glyph='file';detail=path.name
            try:
                size=path.stat().st_size;detail+=f' · {size/1024/1024:.1f} MB' if size>=1024**2 else f' · {size/1024:.1f} KB'
            except OSError:pass
        self.preview.setText(self.preview.fontMetrics().elidedText(detail,Qt.ElideRight,250))
        self.symbol.setPixmap(icon(glyph,c['secondary']).pixmap(30,30))
        for job in self.jobs:job.cancel.set()
        if value and kind in ('image','files'):
            from clipboard_preview import read_clip_image
            from sync_files import DiskWork
            from PySide6.QtCore import QThreadPool
            from PySide6.QtGui import QImage,QPixmap
            from shiboken6 import isValid
            job=DiskWork(read_clip_image,value,38);self.jobs.add(job)
            def ready(image,error):
                self.jobs.discard(job)
                if isValid(self) and generation==self.generation and not error and isinstance(image,QImage) and not image.isNull():self.symbol.setPixmap(QPixmap.fromImage(image))
            job.signals.done.connect(ready);QThreadPool.globalInstance().start(job,10)
        point=QPoint(*context.get('point',(0,0)))
        screen=QGuiApplication.screenAt(point) or QGuiApplication.primaryScreen();area=screen.availableGeometry()
        self._target=QPoint(area.right()-self.width()-15,area.top()+16);self._outside=QPoint(area.right()+1,self._target.y())
        visible=self.isVisible();self.timer.stop();self.animation.stop();self._closing=False
        self.show();self.raise_()
        if reduced_motion():self._frame(1.);self.timer.start(2200)
        elif visible and self.windowOpacity()>=.99:self._frame(1.);self.timer.start(2200)
        else:
            self.animation.setStartValue(self.windowOpacity() if visible else 0.);self.animation.setEndValue(1.);self.animation.start()

    def hideEvent(self,event):
        self.timer.stop();self.animation.stop();super().hideEvent(event)
