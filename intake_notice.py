"""Latest-copy feedback: one capsule that contracts without hiding active work."""
from weakref import WeakSet
from pathlib import Path
from shiboken6 import isValid
from PySide6.QtCore import QPoint,QPointF,QRect,QRectF,Qt,QTimer,QEasingCurve
from PySide6.QtGui import QBrush,QColor,QFontMetricsF,QGuiApplication,QPainter,QPainterPath,QRegion,QTransform
from PySide6.QtWidgets import QWidget
from flickdock_icons import app_icon,document_pixmap,file_pixmap
from async_images import RASTER_SUFFIXES
from native_ui_common import colors,icon,reduced_motion
from platform_native import keep_tool_visible_when_inactive
from display_frames import DisplayAnimation,FramePulse
from ui_transitions import motion_allowed,COPY_NOTICE_MORPH_MS,COPY_NOTICE_FADE_MS,COPY_NOTICE_OVERSHOOT,NOTICE_SPLIT_MS
from clipboard_delivery import sync_progress
import notice_cluster
from transfer_visuals import draw_ring

NOTICE_WIDTH_PX=244
NOTICE_HEIGHT_PX=64
NOTICE_MARGIN_PX=16
NOTICE_PAIR_GAP_PX=8
NOTICE_POSITION_MS=COPY_NOTICE_MORPH_MS
NOTICE_PROGRESS_SMOOTH_MS=240
NOTICE_ACTION_COLOR='#087CEF'
PILL_WIDTH_PX=220
PILL_HEIGHT_PX=44
COMPACT_SIDE_PX=44
MIN_SHAPE_WIDTH_PX=20
PILL_PADDING_PX=12
LOGO_SIZE_PX=24
RING_SIZE_PX=30
RING_VISIBLE_SIZE_PX=24
TEXT_SIZE_PX=13
CONTENT_GAP_PX=10
MASK_ANTIALIAS_MARGIN_PX=2
ENTRY_RISE_PX=6
ENTRY_HEIGHT_PX=36
EXIT_SIDE_PX=28
NOTICE_DURATION_MS=2200
NOTICE_PROGRESS_DURATION_MS=6000
SUCCESS_DURATION_MS=3000
COMPACT_SUCCESS_DURATION_MS=SUCCESS_DURATION_MS
RESULT_COALESCE_MS=160


def paint_circular_artwork(painter,rect,image,theme,cache):
    key=(image.cacheKey(),painter.device().devicePixelRatioF())
    if cache.get('key')!=key:
        side=round(LOGO_SIZE_PX*key[1])
        prepared=image.scaled(side,side,Qt.KeepAspectRatioByExpanding,Qt.SmoothTransformation)
        prepared.setDevicePixelRatio(1.);cache.update(key=key,image=prepared)
    image=cache['image']
    transform=QTransform();transform.translate(rect.center().x(),rect.center().y())
    side=min(image.width(),image.height());transform.scale(rect.width()/side,rect.height()/side)
    transform.translate(-image.width()/2,-image.height()/2)
    brush=QBrush(image);brush.setTransform(transform)
    painter.save();painter.setPen(Qt.NoPen);painter.setBrush(QColor(colors(theme)['control']));painter.drawEllipse(rect)
    painter.setBrush(brush);painter.drawEllipse(rect);painter.restore()


def _reflow_bottom_notices(manager,area,opening=None):
    notices=sorted((window for window in manager._bottom_notices
        if isValid(window) and window._notice_area==area and not window._hiding and (window.isVisible() or window is opening)),
        key=lambda window:window.NOTICE_ORDER)
    if not notices:return
    splitting=opening is not None and not opening.isVisible() and len(notices)>1
    offsets=[];total=0
    for window in notices:
        offsets.append(total+(window._layout_width-window.width())/2)
        total+=window._layout_width+NOTICE_PAIR_GAP_PX
    total-=NOTICE_PAIR_GAP_PX
    first=min(offsets);last=max(offset+window.width() for offset,window in zip(offsets,notices))
    horizontal=last-first<=area.width()-2*NOTICE_MARGIN_PX
    left=max(area.left()+NOTICE_MARGIN_PX-first,min(area.left()+(area.width()-total)/2,area.right()+1-NOTICE_MARGIN_PX-last))
    for index,window in enumerate(notices):
        point=QPoint(round(left+offsets[index]) if horizontal else area.left()+(area.width()-window.width())//2,
            area.bottom()-window.height()-NOTICE_MARGIN_PX+1-(0 if horizontal else index*(PILL_HEIGHT_PX+NOTICE_PAIR_GAP_PX)))
        if (window.isVisible() and motion_allowed() and window._position_motion.state()==window._position_motion.Running
                and window._position_motion.endValue()==point):
            continue
        window._position_motion.stop()
        window._position_motion.setDuration(NOTICE_SPLIT_MS if splitting else NOTICE_POSITION_MS)
        if window.isVisible() and motion_allowed() and window.pos()!=point:
            curve=QEasingCurve(QEasingCurve.OutBack);curve.setOvershoot(COPY_NOTICE_OVERSHOOT)
            window._position_motion.setEasingCurve(curve)
            window._position_motion.setStartValue(window.pos());window._position_motion.setEndValue(point);window._position_motion.start()
        else:
            window.move(point)
            notice_cluster.clear(window)


def place_bottom_notice(window,area):
    """Keep both capsule hosts in the work area, never over the taskbar/Dock."""
    manager=window.manager
    if not hasattr(manager,'_bottom_notices'):manager._bottom_notices=WeakSet()
    previous=window._notice_area
    peers=[other for other in getattr(manager,'_bottom_notices',()) if other is not window
           and isValid(other) and other.isVisible() and not other._hiding and other._notice_area==area]
    manager._bottom_notices.add(window);window._notice_area=QRect(area)
    if previous is not None and previous!=area:_reflow_bottom_notices(manager,previous)
    _reflow_bottom_notices(manager,area,opening=window)
    if not window.isVisible() and motion_allowed():notice_cluster.prepare_split(window,peers)


def copy_presentation(ok,context):
    """Byte completion is not confirmation, and backup is not a device receipt."""
    phase=context.get('phase');cloud=sync_progress(context)['cloud']
    if phase=='size_limit':
        kind=context.get('value',('text',{}))[0]
        label='此文件不能同步' if kind=='files' else '此图片不能同步' if kind=='image' else '此内容不能同步'
        return 'limited',label,'超过 20 MB 同步上限，内容保留在本机',None
    if phase=='superseded':
        return 'superseded','已有更新的复制','本次同步已由更新的复制接替',None
    completed=context.get('backed_up') or phase=='synced' or cloud['phase']=='complete'
    if completed or ok is True:
        return 'success','复制已同步','已同步到云端',None
    if phase=='error' or cloud['phase']=='error' or ok is False and phase!='uploading':
        return 'error','同步未完成','同步未完成，内容保留在本机',None
    progress=cloud if cloud['phase'] in ('uploading','verifying') else dict(phase='preparing',received=0,total=0)
    description='等待云端确认' if cloud['phase']=='verifying' else '正在同步到云端'
    return 'syncing','剪贴板同步中',description,progress


class CapsuleNotice(QWidget):
    """Shared fixed-host capsule artwork, geometry and interruptible motion."""
    HOST_WIDTH=NOTICE_WIDTH_PX
    HOST_HEIGHT=NOTICE_HEIGHT_PX
    MAX_CAPSULE_WIDTH=PILL_WIDTH_PX
    NOTICE_ORDER=0
    def __init__(self,manager):
        super().__init__();self.manager=manager
        self.setWindowFlags(Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint|Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating);self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.NoFocus);self.setCursor(Qt.PointingHandCursor)
        keep_tool_visible_when_inactive(self);self.setWindowTitle('FlickDock · 复制同步提示')
        self.setFixedSize(self.HOST_WIDTH,self.HOST_HEIGHT)
        self._logo=app_icon()
        self._artwork_cache={}
        self._phase='idle';self._text='';self._description='';self._progress=None;self._compact=False;self._hiding=False
        self._shape=QPointF(COMPACT_SIDE_PX,ENTRY_HEIGHT_PX);self._alpha=0.;self._full_width=PILL_WIDTH_PX;self._layout_width=PILL_WIDTH_PX
        self._morph=DisplayAnimation(self);self._morph.setDuration(COPY_NOTICE_MORPH_MS)
        curve=QEasingCurve(QEasingCurve.OutBack);curve.setOvershoot(COPY_NOTICE_OVERSHOOT)
        self._morph.setEasingCurve(curve);self._morph.valueChanged.connect(self._shape_changed)
        self._morph.finished.connect(self._clip_finished)
        self._fade=DisplayAnimation(self);self._fade.setDuration(COPY_NOTICE_FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.OutCubic);self._fade.valueChanged.connect(self._alpha_changed)
        self._fade.finished.connect(self._fade_finished)
        self._ring_clock=FramePulse(self);self._ring_clock.timeout.connect(self.update)
        self._progress_value=0.
        self._progress_motion=DisplayAnimation(self);self._progress_motion.setDuration(NOTICE_PROGRESS_SMOOTH_MS)
        self._progress_motion.setEasingCurve(QEasingCurve.Linear);self._progress_motion.valueChanged.connect(self._progress_changed)
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self._expire)

        self._notice_area=None
        self._liquid_peer=None;self._liquid_children=WeakSet();self._liquid_target=None;self._liquid_mode=None;self._liquid_geometry=None
        self._mask_path=None
        self._position_motion=DisplayAnimation(self);self._position_motion.setDuration(NOTICE_POSITION_MS)
        slide_curve=QEasingCurve(QEasingCurve.OutBack);slide_curve.setOvershoot(COPY_NOTICE_OVERSHOOT)
        self._position_motion.setEasingCurve(slide_curve);self._position_motion.valueChanged.connect(self.move)
        self._position_motion.valueChanged.connect(lambda _:notice_cluster.frame(self))
        self._position_motion.finished.connect(lambda:notice_cluster.clear(self))

    def _shape_changed(self,value):
        self._shape=value;notice_cluster.frame(self)

    def _alpha_changed(self,value):self._alpha=float(value);self.update()

    def capsule_rect(self):
        width=max(MIN_SHAPE_WIDTH_PX,min(self.width()-4,self._shape.x()))
        height=self._shape.y()
        return QRectF((self.width()-width)/2,(self.height()-height)/2+ENTRY_RISE_PX*(1-self._alpha),width,height)

    def _clip_finished(self):
        if not self.isVisible():return
        # Native masks are binary. Keep their edge outside the translucent
        # paint fringe so hit testing cannot clip the antialiased capsule.
        margin=MASK_ANTIALIAS_MARGIN_PX
        rect=self.capsule_rect().adjusted(-margin,-margin,margin,margin)
        path=QPainterPath();path.addRoundedRect(rect,rect.height()/2,rect.height()/2)
        self._set_notice_mask(notice_cluster.shape(self,path,mask=True))

    def _set_notice_mask(self,path):
        # Recentring changes the native position, not its local hit region.
        # Reinstalling an identical mask forces needless compositor work.
        if self._mask_path is not None and self._mask_path==path:return
        self._mask_path=QPainterPath(path)
        self.setMask(QRegion(path.toFillPolygon().toPolygon(),Qt.WindingFill))

    def _animate_shape(self,width,height=PILL_HEIGHT_PX,*,closing=False):
        self._morph.stop();target=QPointF(width,height)
        self._morph.setDuration(NOTICE_SPLIT_MS if self._liquid_mode=='split' and not closing else COPY_NOTICE_MORPH_MS)
        curve=QEasingCurve(QEasingCurve.OutCubic if closing else QEasingCurve.OutBack)
        if not closing:curve.setOvershoot(COPY_NOTICE_OVERSHOOT)
        self._morph.setEasingCurve(curve)
        occupied=max(MIN_SHAPE_WIDTH_PX,min(self.width()-4,width))
        if occupied!=self._layout_width:
            self._layout_width=occupied
            if self.isVisible() and self._notice_area is not None:_reflow_bottom_notices(self.manager,self._notice_area)
        if not motion_allowed():self._shape_changed(target);self._clip_finished();return
        self._morph.setStartValue(self._shape);self._morph.setEndValue(target);self._morph.start()

    def _animate_alpha(self,target):
        self._fade.stop()
        if not motion_allowed():self._alpha_changed(target);self._fade_finished();return
        closing=target==0.
        self._fade.setDuration(COPY_NOTICE_MORPH_MS if closing else COPY_NOTICE_FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.OutCubic)
        self._fade.setStartValue(self._alpha);self._fade.setEndValue(target);self._fade.start()

    def _fade_finished(self):
        if self._hiding:self.hide()
        elif self._morph.state()!=self._morph.Running:self._clip_finished()

    def _sync_ring_clock(self):
        waiting=self._progress is not None and not self._progress['total']
        if self.isVisible() and self._phase=='syncing' and waiting and not reduced_motion():self._ring_clock.start()
        else:self._ring_clock.stop()

    def _progress_changed(self,value):self._progress_value=float(value);self.update()

    def _set_progress(self,state,*,reset=False):
        previous=self._progress;self._progress=state
        if not reset and previous is not None and state is not None and (previous['received'],previous['total'])==(state['received'],state['total']):return
        self._progress_motion.stop()
        total=state['total'] if state is not None else 0
        target=min(1.,max(0.,state['received']/total)) if total>0 else 0.
        if reset or not self.isVisible() or not motion_allowed() or total<=0 or target<=self._progress_value:
            self._progress_changed(target);return
        self._progress_motion.setStartValue(self._progress_value);self._progress_motion.setEndValue(target);self._progress_motion.start()

    def _painted_progress(self):
        if self._progress is None or self._progress['total']<=0:return self._progress
        return dict(self._progress,received=self._progress_value*self._progress['total'])

    def _place(self,area):place_bottom_notice(self,area)

    def _notice_font(self):
        font=self.font();font.setPixelSize(TEXT_SIZE_PX);font.setWeight(font.Weight.Medium);return font

    def _content_width(self):
        text_width=QFontMetricsF(self._notice_font()).horizontalAdvance(self._text)
        return min(self.MAX_CAPSULE_WIDTH,self.width()-4,2*PILL_PADDING_PX+LOGO_SIZE_PX+2*CONTENT_GAP_PX+text_width+RING_VISIBLE_SIZE_PX)

    def _sync_accessibility(self):
        self.setAccessibleName(self._text)
        self.setAccessibleDescription(self._description+('；点击展开提示' if self._compact else '；点击收起提示'))

    def _begin_presentation(self,area):
        self._hiding=False;self._compact=False
        self.setFixedSize(min(self.HOST_WIDTH,max(COMPACT_SIDE_PX,area.width()-2*NOTICE_MARGIN_PX)),min(self.HOST_HEIGHT,area.height()-2*NOTICE_MARGIN_PX))
        self._full_width=self._content_width();self._layout_width=self._full_width;self._place(area)
        if not self.isVisible():self._shape=QPointF(COMPACT_SIDE_PX,ENTRY_HEIGHT_PX);self._alpha=0.
        self.show();self.raise_();self._animate_shape(self._full_width);self._animate_alpha(1.)
        notice_cluster.start_split(self)
        self._sync_accessibility()

    def dismiss(self):
        if self._hiding:return
        self.timer.stop();self._hiding=True;self._ring_clock.stop()
        self._animate_shape(EXIT_SIDE_PX,EXIT_SIDE_PX,closing=True);self._animate_alpha(0.)
        if motion_allowed() and self.isVisible():
            peers=[other for other in self.manager._bottom_notices if other is not self and isValid(other)
                   and other.isVisible() and not other._hiding and other._notice_area==self._notice_area]
            notice_cluster.start_merge(self,peers)

    def supersede(self):
        self._morph.stop();self._fade.stop();self.timer.stop();self._ring_clock.stop();self._progress_motion.stop();self._position_motion.stop();self.hide()

    def _expire(self):self.dismiss()
    def _activate(self):self.dismiss()

    def closeEvent(self,event):
        event.ignore()
        if self.isVisible() and not self._hiding:self.dismiss()

    def hideEvent(self,event):
        self.timer.stop();self._ring_clock.stop();self._morph.stop();self._fade.stop();self._progress_motion.stop();self._position_motion.stop()
        super().hideEvent(event)
        notice_cluster.clear(self)
        if self._notice_area is not None:_reflow_bottom_notices(self.manager,self._notice_area)

    def mouseReleaseEvent(self,event):
        if event.button()==Qt.LeftButton and self.capsule_rect().contains(event.position()):self._activate();event.accept()
        else:super().mouseReleaseEvent(event)

    def paintEvent(self,event):
        c=colors(self.manager.settings.theme);p=QPainter(self)
        # A shrinking translucent surface must erase pixels from its old shape.
        p.setCompositionMode(QPainter.CompositionMode_Source);p.fillRect(self.rect(),Qt.transparent)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        p.setRenderHints(QPainter.Antialiasing|QPainter.SmoothPixmapTransform);p.setOpacity(max(0.,min(1.,self._alpha)))
        rect=self.capsule_rect();path=QPainterPath();path.addRoundedRect(rect,rect.height()/2,rect.height()/2)
        p.setPen(Qt.NoPen);p.setBrush(QColor(c['surface']));p.drawPath(notice_cluster.shape(self,path));p.setClipPath(path)
        reveal=max(0.,min(1.,(rect.width()-COMPACT_SIDE_PX)/max(1,self._full_width-COMPACT_SIDE_PX)))
        ring_x=rect.center().x()-RING_SIZE_PX/2+reveal*(rect.width()/2-PILL_PADDING_PX-RING_VISIBLE_SIZE_PX/2)
        if reveal:
            p.save();p.setOpacity(p.opacity()*reveal)
            # Keep glyph layout stable as the capsule contracts; clipping and
            # opacity avoid switching through several intermediate ellipses.
            p.setClipRect(QRectF(rect.left()+PILL_PADDING_PX,rect.top(),max(0,ring_x+(RING_SIZE_PX-RING_VISIBLE_SIZE_PX)/2-CONTENT_GAP_PX-rect.left()-PILL_PADDING_PX),rect.height()),Qt.IntersectClip)
            self._paint_leading(p,QRectF(rect.left()+PILL_PADDING_PX,rect.center().y()-LOGO_SIZE_PX/2,LOGO_SIZE_PX,LOGO_SIZE_PX))
            p.setFont(self._notice_font())
            p.setPen(QColor(c['link'] if self._phase in ('limited','superseded') else c['red'] if self._phase=='error' else c['text']))
            text_rect=QRectF(rect.left()+PILL_PADDING_PX+LOGO_SIZE_PX+CONTENT_GAP_PX,rect.top(),
                max(1,self._full_width-2*PILL_PADDING_PX-LOGO_SIZE_PX-RING_VISIBLE_SIZE_PX-2*CONTENT_GAP_PX),rect.height())
            p.drawText(text_rect,Qt.AlignVCenter|Qt.AlignLeft,p.fontMetrics().elidedText(self._text,Qt.ElideRight,round(text_rect.width())))
            p.restore()
        ring=QRectF(ring_x,rect.center().y()-RING_SIZE_PX/2,RING_SIZE_PX,RING_SIZE_PX)
        self._paint_indicator(p,ring,c)
        p.end()

    def _paint_leading(self,painter,rect):self._logo.paint(painter,rect.toAlignedRect())


    def _paint_indicator(self,p,ring,c):
        if self._phase=='syncing':draw_ring(p,ring,self._painted_progress())
        else:icon('circle-check' if self._phase=='success' else 'circle-alert',c['green'] if self._phase=='success' else c['red'] if self._phase=='error' else c['link']).paint(p,ring.toAlignedRect())


class ClipboardNotice(CapsuleNotice):
    """Copy feedback follows cloud commit; device receipts stay in the preview."""
    def __init__(self,manager):
        super().__init__(manager)
        self._pending_result=None;self._current_result=None;self._dismissed_id=None
        self._result_timer=QTimer(self);self._result_timer.setSingleShot(True)
        self._result_timer.setInterval(RESULT_COALESCE_MS);self._result_timer.timeout.connect(self._deliver_result)
        self._presenter=getattr(manager,'clipboard_presenter',None)
        self._file_artwork_key=None;self._file_artwork=None
        if self._presenter is not None:self._presenter.changed.connect(self.update)

    def _paint_leading(self,painter,rect):
        context=self._current_result[1] if self._current_result is not None else {}
        value=context.get('value');image=None;kind='txt'
        if value:
            if value[0]=='image':image=value[1].get('image');kind='png'
            elif value[0]=='files':kind=Path(value[1][0]).suffix or 'file'
        if image is None and self._presenter is not None and self._presenter.state.get('id')==context.get('id') and not self._presenter.thumbnail.isNull():image=self._presenter.thumbnail
        if image is None or image.isNull():
            path=value[1][0] if value and value[0]=='files' and value[1] else None
            key=(context.get('id'),path,self.devicePixelRatioF())
            if key!=self._file_artwork_key:
                self._file_artwork_key=key
                # Raster decoding belongs to the shared presenter worker.
                pixmap=(file_pixmap(path,LOGO_SIZE_PX,dpr=self.devicePixelRatioF())
                    if path and Path(path).suffix.lower() not in RASTER_SUFFIXES|{'.svg','.svgz'}
                    else document_pixmap(kind,LOGO_SIZE_PX,dpr=self.devicePixelRatioF()))
                self._file_artwork=pixmap.toImage()
            image=self._file_artwork
        paint_circular_artwork(painter,rect,image,self.manager.settings.theme,self._artwork_cache)

    def replay(self,state):
        self._result_timer.stop();self._pending_result=None;self._dismissed_id=None;self._current_result=None
        self.show_result(None,state)

    def dismiss(self):
        context=self._current_result[1] if self._current_result is not None else self._pending_result[1] if self._pending_result is not None else {}
        self._dismissed_id=context.get('id');self._result_timer.stop();self._pending_result=None;self.timer.stop()
        super().dismiss()
    def supersede(self):
        if self._current_result is not None:self._dismissed_id=self._current_result[1].get('id')
        self._result_timer.stop();self._pending_result=None;self._current_result=None
        super().supersede()
    def schedule_result(self,ok,context):
        if context.get('id') and context['id']==self._dismissed_id:return
        self._pending_result=(ok,dict(context));self._result_timer.start()

    def update_delivery(self,state,*,authoritative=False):
        if not self.manager.settings.clipboard_success_notice:self.supersede();return
        identity=state.get('id')
        current=self._current_result or self._pending_result
        if authoritative and current is not None and (not identity or identity!=current[1].get('id') or state.get('permission_required')):
            if identity and state.get('local_copy') and state.get('phase')=='uploading' and not state.get('permission_required'):
                self.replay(state)
            else:self.dismiss()
            return
        if not identity or identity==self._dismissed_id or state.get('permission_required'):return
        if self._pending_result is not None:
            ok,context=self._pending_result
            if context.get('id')==identity:context.update(state)
        if self._current_result is None or not self.isVisible():
            if self._pending_result is None and state.get('local_copy') and state.get('phase')=='uploading':
                self.schedule_result(None,state)
            return
        ok,context=self._current_result
        if context.get('id')==identity:self.show_result(ok,dict(context,**state))

    def _deliver_result(self):
        result,self._pending_result=self._pending_result,None
        if (result is not None and not getattr(self.manager,'_quitting',False)
                and getattr(self.manager,'_clipboard_runtime_enabled',lambda:True)()
                and self.manager.settings.clipboard_success_notice):self.show_result(*result)

    def show_result(self,ok,context):
        if not self.manager.settings.clipboard_success_notice:return
        if self._presenter is not None:
            state=self._presenter.state
            if not context.get('id') or context['id']!=state.get('id'):return
            context=dict(context,**state)
            ok=True if state.get('backed_up') or state.get('phase')=='synced' else False if state.get('phase')=='error' else None
        if context.get('id') and context['id']==self._dismissed_id:return
        same=bool(self.isVisible() and self._current_result is not None and context.get('id')
                  and context['id']==self._current_result[1].get('id'))
        previous=self._phase
        self._phase,self._text,self._description,progress=copy_presentation(ok,context)
        self._set_progress(progress,reset=not same)
        self._current_result=(ok,dict(context));self._hiding=False
        if not same:
            screen=QGuiApplication.screenAt(QPoint(*(context.get('point') or (0,0)))) or QGuiApplication.primaryScreen()
            self._begin_presentation(screen.availableGeometry())
            self.timer.start(NOTICE_PROGRESS_DURATION_MS if self._phase=='syncing' else SUCCESS_DURATION_MS if self._phase=='success' else NOTICE_DURATION_MS)
        elif previous!=self._phase:
            self._full_width=self._content_width()
            if self._phase=='syncing':self.timer.start(NOTICE_PROGRESS_DURATION_MS)
            else:
                if self._phase!='success' and self._compact:
                    self._compact=False
                self.timer.start(SUCCESS_DURATION_MS if self._phase=='success' else COMPACT_SUCCESS_DURATION_MS if self._compact else NOTICE_DURATION_MS)
            if not self._compact:self._animate_shape(self._full_width)
        self._sync_accessibility();self._sync_ring_clock();self.update()

    def _expire(self):
        if self._phase=='syncing':
            self.timer.stop();self._compact=True;self._animate_shape(COMPACT_SIDE_PX)
            self._sync_accessibility()
        else:self.dismiss()

    def toggle_expanded(self):
        if self._hiding or not self.isVisible():return
        self._compact=not self._compact
        self._animate_shape(COMPACT_SIDE_PX if self._compact else self._full_width)
        if self._phase=='syncing':
            if self._compact:self.timer.stop()
            else:self.timer.start(NOTICE_PROGRESS_DURATION_MS)
        else:self.timer.start(SUCCESS_DURATION_MS if self._phase=='success' else COMPACT_SUCCESS_DURATION_MS if self._compact else NOTICE_DURATION_MS)
        self._sync_accessibility()

    def _activate(self):self.toggle_expanded()

    def hideEvent(self,event):
        self._current_result=None;super().hideEvent(event)
