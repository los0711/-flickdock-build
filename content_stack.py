"""Latest-first, five-card content artwork shared by compact and drag previews."""
from pathlib import Path
from PySide6.QtCore import QRectF, Qt, QVariantAnimation, QEasingCurve, QAbstractAnimation
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPixmap
from flickdock_icons import content_color, device_pixel_ratio, document_pixmap
from flickdock_motion import ContentTile
from transfer_visuals import draw_ring


def stack_refs(refs):
    """Paint order: oldest visible card first, latest received card last."""
    return tuple(refs)[-5:]


def stack_pixmap(repository, refs, size=110, *, dpr=None, preview_cache=None, arrival=1.0,front_art=None):
    selected=stack_refs(refs)
    ratio=device_pixel_ratio(dpr)
    pm=QPixmap(round(size*ratio),round(size*ratio))
    pm.setDevicePixelRatio(ratio);pm.fill(Qt.GlobalColor.transparent)
    if not selected:return pm
    p=QPainter(pm)
    p.setRenderHints(QPainter.RenderHint.Antialiasing|QPainter.RenderHint.SmoothPixmapTransform)
    p.scale(size/100,size/100)
    colors=[]
    for ref in selected:
        kind=repository.category(ref)
        name=repository.title(ref) if getattr(repository,'is_pending',lambda r:False)(ref) else ref
        value=('link' if kind=='links' else 'TEXT') if repository.is_text(ref) else Path(name).suffix or ('folder' if Path(ref).is_dir() else 'generic')
        variant=0;color=content_color(value)
        while color in colors:
            variant+=1;color=content_color(value,variant)
        colors.append(color)
    front=QRectF(27,8,62,74)
    for index,ref in enumerate(selected):
        depth=len(selected)-1-index
        p.save();p.translate(front.center());p.rotate(-depth*3.2);p.translate(-front.center())
        card=front.translated(-depth*2.2,depth*2.5)
        if depth==0 and arrival!=1:
            p.translate(0,-18*(1-arrival));p.translate(card.center())
            p.rotate(7*(1-arrival));p.scale(.94+.06*arrival,.94+.06*arrival);p.translate(-card.center())
            p.setOpacity(max(0,min(1,.35+.65*arrival)))
        p.setPen(Qt.PenStyle.NoPen);p.setBrush(QColor(16,30,50,24))
        p.drawRoundedRect(card.translated(0,2),8,8)
        p.setBrush(QColor(colors[index]));p.drawRoundedRect(card,8,8)
        p.setBrush(Qt.BrushStyle.NoBrush);p.setPen(QPen(QColor(255,255,255,45),.7))
        p.drawRoundedRect(card.adjusted(.4,.4,-.4,-.4),8,8)
        if depth==0:
            kind=repository.category(ref)
            cached=(preview_cache or {}).get(ref)
            actual=kind=='images' or (cached is not None and not cached.isNull()) or bool(repository.metadata.get(ref,{}).get('preview_image'))
            if actual:
                image=front_art if front_art is not None else QPixmap.fromImage(cached) if isinstance(cached,QImage) and not cached.isNull() else repository.item_pixmap(ref,90,dpr=ratio)
                clip=QPainterPath();clip.addRoundedRect(card.adjusted(4,4,-4,-4),5,5);p.setClipPath(clip)
                available=card.adjusted(4,4,-4,-4)
                logical=image.deviceIndependentSize()
                scale=min(available.width()/max(1,logical.width()),available.height()/max(1,logical.height()))
                width,height=logical.width()*scale,logical.height()*scale
                p.drawPixmap(QRectF(available.center().x()-width/2,available.center().y()-height/2,width,height),image,QRectF(image.rect()))
                p.setClipping(False)
            else:
                name=repository.title(ref) if getattr(repository,'is_pending',lambda r:False)(ref) else ref
                value=('link' if kind=='links' else 'TEXT') if repository.is_text(ref) else Path(name).suffix or 'generic'
                art=front_art if front_art is not None else document_pixmap(value,100,dpr=ratio)
                # Match the family artwork's 13,5,43,51 box to this front card.
                destination=QRectF(card.x()-13*card.width()/43,card.y()-5*card.height()/51,
                                   64*card.width()/43,64*card.height()/51)
                p.drawPixmap(destination,art,QRectF(art.rect()))
        p.restore()
    p.end();return pm


class StackTile(ContentTile):
    """Cached stack with an interruptible, short incoming-card landing."""
    def __init__(self,parent=None,motion=True):
        super().__init__(parent,motion)
        self.repository=None;self.refs=();self.preview_cache={};self.arrival=1.0
        self._stack_cache=QPixmap();self._stack_dpr=0
        self._front_art=None
        self.landing=QVariantAnimation(self)
        self.landing.setDuration(260)
        self.landing.setEasingCurve(QEasingCurve.Type.OutBack)
        self.landing.valueChanged.connect(self._arrive)
        self.landing.finished.connect(self._landing_finished)

    def _arrive(self,value):
        self.arrival=float(value);self.update()

    def _landing_finished(self):
        self._arrive(1.0)
        self._try_complete_pulse()

    def _pulse_ready(self):
        return self.landing.state()==QAbstractAnimation.State.Stopped

    def _finish_pulse_visual(self):
        self.landing.stop()
        self._arrive(1.0)

    def replay_landing(self):
        """Show the committed incoming card after an expanded view folds."""
        if not self.motion or not self.refs:return
        self.landing.stop()
        self._arrive(0.0)
        self.landing.setStartValue(0.0);self.landing.setEndValue(1.0)
        self.landing.start()

    def set_stack(self,repository,refs,preview_cache=None):
        new_refs=stack_refs(refs)
        incoming=bool(self.refs and new_refs and self.refs[-1]!=new_refs[-1]) or bool(not self.refs and new_refs)
        self.repository=repository;self.refs=new_refs;self.preview_cache=preview_cache or {}
        ratio=self.devicePixelRatioF()
        self._front_art=None
        if new_refs:
            ref=new_refs[-1];kind=repository.category(ref);cached=self.preview_cache.get(ref)
            if kind=='images' or (cached is not None and not cached.isNull()) or repository.metadata.get(ref,{}).get('preview_image'):
                self._front_art=QPixmap.fromImage(cached) if isinstance(cached,QImage) and not cached.isNull() else repository.item_pixmap(ref,90,dpr=ratio)
            else:
                value=('link' if kind=='links' else 'TEXT') if repository.is_text(ref) else Path(ref).suffix or 'generic'
                self._front_art=document_pixmap(value,100,dpr=ratio)
        self._stack_cache=stack_pixmap(repository,new_refs,128,dpr=ratio,preview_cache=self.preview_cache,front_art=self._front_art)
        self._stack_dpr=ratio
        self.set_content('file',pixmap=self._stack_cache)
        if incoming and self.motion:
            start=self.arrival if self.landing.state()==QAbstractAnimation.State.Running else 0.0
            self.landing.stop();self.landing.setStartValue(min(.8,start));self.landing.setEndValue(1.0);self.landing.start()
        elif not new_refs:
            self.landing.stop();self.arrival=1

    def paintEvent(self,event):
        if self.repository is not None and self.refs:
            ratio=self.devicePixelRatioF()
            if self.landing.state()==QAbstractAnimation.State.Running:
                self._pixmap=stack_pixmap(self.repository,self.refs,128,dpr=ratio,preview_cache=self.preview_cache,arrival=self.arrival,front_art=self._front_art)
            else:
                if ratio!=self._stack_dpr:
                    self._stack_cache=stack_pixmap(self.repository,self.refs,128,dpr=ratio,preview_cache=self.preview_cache)
                    self._stack_dpr=ratio
                self._pixmap=self._stack_cache
        state=getattr(self.repository,'transfers',{}).get(self.refs[-1]) if self.refs else None
        if state and not self._pixmap.isNull():
            original=self._pixmap
            dim=QPixmap(original.size());dim.setDevicePixelRatio(original.devicePixelRatioF());dim.fill(Qt.GlobalColor.transparent)
            painter=QPainter(dim);painter.setOpacity(.38);painter.drawPixmap(0,0,original);painter.end()
            self._pixmap=dim
        super().paintEvent(event)
        if state:
            self._pixmap=original
            painter=QPainter(self)
            side=min(82,self.width()*.55,self.height()*.7)
            draw_ring(painter,QRectF((self.width()-side)/2,(self.height()-side)/2,side,side),state)
