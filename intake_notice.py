"""Latest-copy feedback: one capsule that contracts without hiding active work."""
from PySide6.QtCore import QPoint,QPointF,QRectF,Qt,QTimer,QEasingCurve
from PySide6.QtGui import QColor,QFontMetricsF,QGuiApplication,QPainter,QPainterPath,QRegion
from PySide6.QtWidgets import QWidget
from flickdock_icons import app_icon
from native_ui_common import colors,icon,reduced_motion
from platform_native import keep_tool_visible_when_inactive
from display_frames import DisplayAnimation,FramePulse
from ui_transitions import motion_allowed,COPY_NOTICE_MORPH_MS,COPY_NOTICE_FADE_MS,COPY_NOTICE_OVERSHOOT
from clipboard_delivery import sync_progress
from transfer_visuals import draw_ring

NOTICE_WIDTH_PX=244
NOTICE_HEIGHT_PX=64
NOTICE_MARGIN_PX=16
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
ENTRY_RISE_PX=6
ENTRY_HEIGHT_PX=36
EXIT_SIDE_PX=28
EXIT_FADE_START=.55
NOTICE_DURATION_MS=2200
NOTICE_PROGRESS_DURATION_MS=6000
COMPACT_SUCCESS_DURATION_MS=1000
RESULT_COALESCE_MS=160


def place_bottom_notice(window,area):
    """Use the work area so the taskbar/Dock remains clear on every screen."""
    window.move(area.left()+(area.width()-window.width())//2,area.bottom()-window.height()-NOTICE_MARGIN_PX+1)


def copy_presentation(ok,context):
    """Byte completion is not confirmation, and backup is not a device receipt."""
    phase=context.get('phase');cloud=sync_progress(context)['cloud']
    if phase=='size_limit':
        return 'limited','此文件不能同步','超过 20 MB 同步上限，内容保留在本机',None
    if phase=='superseded':
        return 'superseded','已有更新的复制','本次同步已由更新的复制接替',None
    completed=context.get('backed_up') or phase=='synced' or cloud['phase']=='complete'
    if phase=='error' or cloud['phase']=='error' or ok is False and not completed and phase!='uploading':
        return 'error','同步未完成','同步未完成，内容保留在本机',None
    pending=any(row['success']<row['total'] for row in context.get('deliveries',{}).values())
    if (completed or ok is True) and not pending:
        return 'success','复制已同步','云端已备份，当前在线设备已接收完成',None
    progress=cloud if cloud['phase']=='uploading' else dict(phase='preparing',received=0,total=0)
    description=('等待在线设备接收确认' if completed or ok is True else
                 '等待云端确认' if cloud['phase']=='verifying' else '正在同步到云端')
    return 'syncing','剪贴板同步中',description,progress


class ClipboardNotice(QWidget):
    """Fixed native host; only the painted capsule changes shape or opacity."""
    def __init__(self,manager):
        super().__init__();self.manager=manager
        self.setWindowFlags(Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint|Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating);self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.NoFocus);self.setCursor(Qt.PointingHandCursor)
        keep_tool_visible_when_inactive(self);self.setWindowTitle('FlickDock · 复制同步提示')
        self.setFixedSize(NOTICE_WIDTH_PX,NOTICE_HEIGHT_PX)
        self._logo=app_icon().pixmap(LOGO_SIZE_PX,LOGO_SIZE_PX)
        self._pending_result=None;self._current_result=None;self._dismissed_id=None
        self._phase='idle';self._text='';self._progress=None;self._compact=False;self._hiding=False
        self._shape=QPointF(COMPACT_SIDE_PX,ENTRY_HEIGHT_PX);self._alpha=0.;self._full_width=PILL_WIDTH_PX
        self._morph=DisplayAnimation(self);self._morph.setDuration(COPY_NOTICE_MORPH_MS)
        curve=QEasingCurve(QEasingCurve.OutBack);curve.setOvershoot(COPY_NOTICE_OVERSHOOT)
        self._morph.setEasingCurve(curve);self._morph.valueChanged.connect(self._shape_changed)
        self._morph.finished.connect(self._clip_finished)
        self._fade=DisplayAnimation(self);self._fade.setDuration(COPY_NOTICE_FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.OutCubic);self._fade.valueChanged.connect(self._alpha_changed)
        self._fade.finished.connect(self._fade_finished)
        self._ring_clock=FramePulse(self);self._ring_clock.timeout.connect(self.update)
        self._result_timer=QTimer(self);self._result_timer.setSingleShot(True)
        self._result_timer.setInterval(RESULT_COALESCE_MS);self._result_timer.timeout.connect(self._deliver_result)
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self._expire)

    def _shape_changed(self,value):self._shape=value;self.update()
    def _alpha_changed(self,value):self._alpha=float(value);self.update()

    def capsule_rect(self):
        width=max(MIN_SHAPE_WIDTH_PX,min(self.width()-4,self._shape.x()))
        height=self._shape.y()
        return QRectF((self.width()-width)/2,(self.height()-height)/2+ENTRY_RISE_PX*(1-self._alpha),width,height)

    def _clip_finished(self):
        if not self.isVisible():return
        path=QPainterPath();rect=self.capsule_rect();path.addRoundedRect(rect,rect.height()/2,rect.height()/2)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def _animate_shape(self,width,height=PILL_HEIGHT_PX):
        self.clearMask();self._morph.stop();target=QPointF(width,height)
        if not motion_allowed():self._shape_changed(target);self._clip_finished();return
        self._morph.setStartValue(self._shape);self._morph.setEndValue(target);self._morph.start()

    def _animate_alpha(self,target):
        self._fade.stop()
        if not motion_allowed():self._alpha_changed(target);self._fade_finished();return
        closing=target==0.
        self._fade.setDuration(COPY_NOTICE_MORPH_MS if closing else COPY_NOTICE_FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.Linear if closing else QEasingCurve.OutCubic)
        self._fade.setKeyValueAt(EXIT_FADE_START,self._alpha if closing else self._alpha+(target-self._alpha)*EXIT_FADE_START)
        self._fade.setStartValue(self._alpha);self._fade.setEndValue(target);self._fade.start()

    def _fade_finished(self):
        if self._hiding:self.hide()
        elif self._morph.state()!=self._morph.Running:self._clip_finished()

    def _sync_ring_clock(self):
        waiting=self._progress is not None and not self._progress['total']
        if self.isVisible() and self._phase=='syncing' and waiting and not reduced_motion():self._ring_clock.start()
        else:self._ring_clock.stop()

    def dismiss(self):
        context=self._current_result[1] if self._current_result is not None else self._pending_result[1] if self._pending_result is not None else {}
        self._dismissed_id=context.get('id');self._result_timer.stop();self._pending_result=None;self.timer.stop()
        self._hiding=True;self._ring_clock.stop();self._animate_shape(EXIT_SIDE_PX,EXIT_SIDE_PX);self._animate_alpha(0.)

    def supersede(self):
        if self._current_result is not None:self._dismissed_id=self._current_result[1].get('id')
        self._result_timer.stop();self._pending_result=None;self._current_result=None
        self._morph.stop();self._fade.stop();self.timer.stop();self._ring_clock.stop();self.hide()

    def schedule_result(self,ok,context):
        if context.get('id') and context['id']==self._dismissed_id:return
        self._pending_result=(ok,dict(context));self._result_timer.start()

    def update_delivery(self,state):
        if not self.manager.settings.clipboard_success_notice:self.supersede();return
        identity=state.get('id')
        if not identity or identity==self._dismissed_id:return
        if self._pending_result is not None:
            ok,context=self._pending_result
            if context.get('id')==identity:context.update(state)
        if self._current_result is None or not self.isVisible():
            if self._pending_result is None and state.get('local_copy') and state.get('phase')=='uploading':
                self.schedule_result(None,state)
            return
        ok,context=self._current_result
        if context.get('id')==identity:self.show_result(ok,dict(context,**state))

    def _place(self,area):place_bottom_notice(self,area)

    def _notice_font(self):
        font=self.font();font.setPixelSize(TEXT_SIZE_PX);font.setWeight(font.Weight.Medium);return font

    def _content_width(self):
        text_width=QFontMetricsF(self._notice_font()).horizontalAdvance(self._text)
        return min(PILL_WIDTH_PX,self.width()-4,2*PILL_PADDING_PX+LOGO_SIZE_PX+2*CONTENT_GAP_PX+text_width+RING_VISIBLE_SIZE_PX)

    def _deliver_result(self):
        result,self._pending_result=self._pending_result,None
        if (result is not None and not getattr(self.manager,'_quitting',False)
                and getattr(self.manager,'_clipboard_runtime_enabled',lambda:True)()
                and self.manager.settings.clipboard_success_notice):self.show_result(*result)

    def show_result(self,ok,context):
        if not self.manager.settings.clipboard_success_notice:return
        if context.get('id') and context['id']==self._dismissed_id:return
        same=bool(self.isVisible() and self._current_result is not None and context.get('id')
                  and context['id']==self._current_result[1].get('id'))
        previous=self._phase
        self._phase,self._text,description,self._progress=copy_presentation(ok,context)
        self.setAccessibleName(self._text);self.setAccessibleDescription(description+'；点击关闭提示')
        self._current_result=(ok,dict(context));self._hiding=False
        if not same:
            screen=QGuiApplication.screenAt(QPoint(*(context.get('point') or (0,0)))) or QGuiApplication.primaryScreen()
            area=screen.availableGeometry();self.setFixedSize(min(NOTICE_WIDTH_PX,max(COMPACT_SIDE_PX,area.width()-2*NOTICE_MARGIN_PX)),NOTICE_HEIGHT_PX)
            self._place(area);self._compact=False;self._full_width=self._content_width()
            if not self.isVisible():self._shape=QPointF(COMPACT_SIDE_PX,ENTRY_HEIGHT_PX);self._alpha=0.
            self.show();self.raise_();self._animate_shape(self._full_width);self._animate_alpha(1.)
            self.timer.start(NOTICE_PROGRESS_DURATION_MS if self._phase=='syncing' else NOTICE_DURATION_MS)
        elif previous!=self._phase:
            self._full_width=self._content_width()
            if self._phase=='syncing':self.timer.start(NOTICE_PROGRESS_DURATION_MS)
            else:
                if self._phase!='success' and self._compact:
                    self._compact=False
                self.timer.start(COMPACT_SUCCESS_DURATION_MS if self._compact else NOTICE_DURATION_MS)
            if not self._compact:self._animate_shape(self._full_width)
        self._sync_ring_clock();self.update()

    def _expire(self):
        if self._phase=='syncing':
            self.timer.stop();self._compact=True;self._animate_shape(COMPACT_SIDE_PX)
        else:self.dismiss()

    def hideEvent(self,event):
        self.timer.stop();self._ring_clock.stop();self._morph.stop();self._fade.stop()
        self._current_result=None;super().hideEvent(event)

    def mouseReleaseEvent(self,event):
        if event.button()==Qt.LeftButton and self.capsule_rect().contains(event.position()):self.dismiss();event.accept()
        else:super().mouseReleaseEvent(event)

    def paintEvent(self,event):
        c=colors(self.manager.settings.theme);p=QPainter(self)
        p.setRenderHints(QPainter.Antialiasing|QPainter.SmoothPixmapTransform);p.setOpacity(max(0.,min(1.,self._alpha)))
        rect=self.capsule_rect();path=QPainterPath();path.addRoundedRect(rect,rect.height()/2,rect.height()/2)
        p.setPen(Qt.NoPen);p.setBrush(QColor(c['surface']));p.drawPath(path);p.setClipPath(path)
        reveal=max(0.,min(1.,(rect.width()-COMPACT_SIDE_PX)/max(1,self._full_width-COMPACT_SIDE_PX)))
        ring_x=rect.center().x()-RING_SIZE_PX/2+reveal*(rect.width()/2-PILL_PADDING_PX-RING_VISIBLE_SIZE_PX/2)
        if reveal:
            p.save();p.setOpacity(p.opacity()*reveal)
            # Keep glyph layout stable as the capsule contracts; clipping and
            # opacity avoid switching through several intermediate ellipses.
            p.setClipRect(QRectF(rect.left()+PILL_PADDING_PX,rect.top(),max(0,ring_x+(RING_SIZE_PX-RING_VISIBLE_SIZE_PX)/2-CONTENT_GAP_PX-rect.left()-PILL_PADDING_PX),rect.height()),Qt.IntersectClip)
            p.drawPixmap(round(rect.left()+PILL_PADDING_PX),round(rect.center().y()-LOGO_SIZE_PX/2),self._logo)
            p.setFont(self._notice_font())
            p.setPen(QColor(c['link'] if self._phase in ('limited','superseded') else c['red'] if self._phase=='error' else c['text']))
            text_rect=QRectF(rect.left()+PILL_PADDING_PX+LOGO_SIZE_PX+CONTENT_GAP_PX,rect.top(),
                max(1,self._full_width-2*PILL_PADDING_PX-LOGO_SIZE_PX-RING_VISIBLE_SIZE_PX-2*CONTENT_GAP_PX),rect.height())
            p.drawText(text_rect,Qt.AlignVCenter|Qt.AlignLeft,p.fontMetrics().elidedText(self._text,Qt.ElideRight,round(text_rect.width())))
            p.restore()
        ring=QRectF(ring_x,rect.center().y()-RING_SIZE_PX/2,RING_SIZE_PX,RING_SIZE_PX)
        if self._phase=='syncing':draw_ring(p,ring,self._progress)
        else:icon('circle-check' if self._phase=='success' else 'circle-alert',c['green'] if self._phase=='success' else c['red'] if self._phase=='error' else c['link']).paint(p,ring.toAlignedRect())
        p.end()
