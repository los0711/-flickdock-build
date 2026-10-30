"""Latest-first, five-card content artwork shared by compact and drag previews."""
from pathlib import Path
from PySide6.QtCore import QRectF, Qt, QVariantAnimation, QEasingCurve, QAbstractAnimation
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPixmap
from flickdock_icons import content_color, device_pixel_ratio, document_pixmap
from flickdock_motion import ContentTile
from transfer_visuals import draw_progress_badge


def stack_refs(refs):
    """Paint order: oldest visible card first, latest received card last."""
    return tuple(refs)[-5:]


def stack_pixmap(repository, refs, size=110, *, dpr=None, preview_cache=None, arrival=1.0,front_art=None,layer_ref=None):
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
        if layer_ref is not None and ref!=layer_ref:continue
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
        self._seen_cloud_arrivals={}
        self._quiet_anchor=None;self._quiet_keys=set();self._all_keys=set()
        self._layers={};self._exit_from={};self._exit_to={};self._exit_order=()
        self._exit_depths={};self._exit_progress=1.0
        self.removal=QVariantAnimation(self);self.removal.setDuration(200)
        curve=QEasingCurve(QEasingCurve.Type.BezierSpline)
        from PySide6.QtCore import QPointF
        curve.addCubicBezierSegment(QPointF(.23,1),QPointF(.32,1),QPointF(1,1))
        self.removal.setEasingCurve(curve)
        self.removal.valueChanged.connect(self._remove_frame)
        self.removal.finished.connect(self._remove_finished)
        self.landing=QVariantAnimation(self)
        self.landing.setDuration(260)
        self.landing.setEasingCurve(QEasingCurve.Type.OutBack)
        self.landing.valueChanged.connect(self._arrive)
        self.landing.finished.connect(self._landing_finished)

    def _arrive(self,value):
        self.arrival=float(value);self.update()

    def _remove_frame(self,value):
        self._exit_progress=float(value);self.update()

    def _remove_finished(self):
        self._exit_from={};self._exit_to={};self._exit_order=();self._exit_depths={}
        self._exit_progress=1.0;self.update()

    def _exit_layers(self):
        """Small cached artwork layers only; no file reads during animation."""
        frames={};progress=self._exit_progress
        for ref in self._exit_order:
            old=self._exit_from.get(ref);new=self._exit_to.get(ref)
            sample=new if new is not None else old
            if sample is None:continue
            result=QPixmap(sample.size());result.setDevicePixelRatio(sample.devicePixelRatioF());result.fill(Qt.transparent)
            painter=QPainter(result);painter.setRenderHint(QPainter.SmoothPixmapTransform)
            delta=self._exit_depths.get(ref,0)
            if old is not None and new is None:
                painter.setOpacity(1-progress)
                painter.drawPixmap(QRectF(-2.2*delta*progress*1.28,2.5*delta*progress*1.28,128,128),old,QRectF(old.rect()))
            if new is not None:
                # A surviving card is one opaque layer translated to its new
                # depth, never two translucent copies that darken its centre.
                painter.setOpacity(1.0 if old is not None else progress)
                painter.drawPixmap(QRectF(2.2*delta*(1-progress)*1.28,-2.5*delta*(1-progress)*1.28,128,128),new,QRectF(new.rect()))
            painter.end();frames[ref]=result
        return frames

    def _exit_pixmap(self):
        ratio=self.devicePixelRatioF()
        result=QPixmap(round(128*ratio),round(128*ratio));result.setDevicePixelRatio(ratio);result.fill(Qt.transparent)
        painter=QPainter(result)
        for layer in self._exit_layers().values():painter.drawPixmap(0,0,layer)
        painter.end();return result

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
        from native_ui_common import reduced_motion
        if not self.motion or reduced_motion() or not self.refs:return
        self.landing.stop()
        self._arrive(0.0)
        self.landing.setStartValue(0.0);self.landing.setEndValue(1.0)
        self.landing.start()

    def set_stack(self,repository,refs,preview_cache=None):
        from native_ui_common import reduced_motion
        animate=self.motion and not reduced_motion()
        if not animate:
            self.removal.stop();self._remove_finished()
        all_refs=tuple(refs)
        def key(ref):
            identity=(ref.removeprefix('flickdock:transfer:') if ref.startswith('flickdock:transfer:')
                      else repository.metadata.get(ref,{}).get('_cloud_item_id'))
            return ('cloud',identity) if identity else ('local',ref)
        keys={key(ref):ref for ref in all_refs}
        quiet=bool(getattr(repository,'quiet_cloud_refresh',False))
        new_local=any(value[0]=='local' and value not in self._all_keys for value in keys)
        if quiet:
            if self._quiet_anchor is None and self.refs:self._quiet_anchor=key(self.refs[-1])
            self._quiet_keys.update(keys)
        elif self._quiet_anchor is not None and any(value not in self._quiet_keys for value in keys):
            self._quiet_anchor=None;self._quiet_keys.clear()
        if new_local and self._all_keys:
            self._quiet_anchor=None
        if self._quiet_anchor not in keys:self._quiet_anchor=None
        # Preserve the card already under the pointer while old files catch
        # up. Only the artwork order changes; the shelf's real order and the
        # overview selection/scroll remain untouched. A fresh live item or a
        # local drop releases this visual anchor immediately.
        display=all_refs
        if self._quiet_anchor is not None:
            anchor=keys[self._quiet_anchor]
            display=tuple(ref for ref in all_refs if ref!=anchor)+(anchor,)
        self._all_keys=set(keys)
        self._quiet_keys.intersection_update(keys)
        new_refs=stack_refs(display)
        previous=self.refs
        # Removing the front reveals an existing card, never a new arrival.
        incoming=bool(new_refs and new_refs[-1] not in previous)
        if quiet and not new_local:
            incoming=False;self.landing.stop();self.arrival=1.0
        old_layers=self._exit_layers() if self.removal.state()==QAbstractAnimation.Running else self._layers
        identities={ref:(ref.removeprefix('flickdock:transfer:') if ref.startswith('flickdock:transfer:')
            else repository.metadata.get(ref,{}).get('_cloud_item_id')) for ref in new_refs}
        new_identities={value for value in identities.values() if value}
        removed=any(ref not in all_refs and
            getattr(self,'_identities',{}).get(ref) not in new_identities for ref in previous)
        self._identities=identities
        if new_refs:
            ref=new_refs[-1]
            identity=(ref.removeprefix('flickdock:transfer:') if ref.startswith('flickdock:transfer:')
                      else repository.metadata.get(ref,{}).get('_cloud_item_id'))
            if identity:
                incoming=incoming and identity not in self._seen_cloud_arrivals
                self._seen_cloud_arrivals[identity]=None
                while len(self._seen_cloud_arrivals)>512:self._seen_cloud_arrivals.pop(next(iter(self._seen_cloud_arrivals)))
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
        if animate:
            self._layers={ref:stack_pixmap(repository,new_refs,128,dpr=ratio,preview_cache=self.preview_cache,
                front_art=self._front_art,layer_ref=ref) for ref in new_refs}
            if previous!=new_refs and (removed or self.removal.state()==QAbstractAnimation.Running):
                self.removal.stop();self.landing.stop();self.arrival=1.0
                self._exit_from=old_layers;self._exit_to=self._layers
                self._exit_order=tuple(dict.fromkeys((*old_layers,*new_refs)))
                self._exit_depths={ref:(len(new_refs)-1-new_refs.index(ref))-(len(previous)-1-previous.index(ref))
                    for ref in new_refs if ref in previous}
                self._exit_progress=0.0
                self.removal.setStartValue(0.0);self.removal.setEndValue(1.0);self.removal.start()
        self.set_content('file',pixmap=self._stack_cache)
        if incoming and animate and self.removal.state()!=QAbstractAnimation.Running:
            start=self.arrival if self.landing.state()==QAbstractAnimation.State.Running else 0.0
            self.landing.stop();self.landing.setStartValue(min(.8,start));self.landing.setEndValue(1.0);self.landing.start()
        elif not new_refs:
            self.landing.stop();self.arrival=1

    def paintEvent(self,event):
        original_kind=self._kind
        if self.removal.state()==QAbstractAnimation.Running:
            # Even the last card fades over the immediate empty state.
            self._pixmap=self._exit_pixmap();self._kind='file'
        elif self.repository is not None and self.refs:
            ratio=self.devicePixelRatioF()
            if self.landing.state()==QAbstractAnimation.State.Running:
                self._pixmap=stack_pixmap(self.repository,self.refs,128,dpr=ratio,preview_cache=self.preview_cache,arrival=self.arrival,front_art=self._front_art)
            else:
                if ratio!=self._stack_dpr:
                    self._stack_cache=stack_pixmap(self.repository,self.refs,128,dpr=ratio,preview_cache=self.preview_cache)
                    self._stack_dpr=ratio
                self._pixmap=self._stack_cache
        states=getattr(self.repository,'transfers',{})
        state=next((states[ref] for ref in reversed(self.refs) if ref in states),None)
        super().paintEvent(event)
        self._kind=original_kind
        if state:
            painter=QPainter(self)
            draw_progress_badge(painter,QRectF(self.width()-41,self.height()-39,24,24),state)
