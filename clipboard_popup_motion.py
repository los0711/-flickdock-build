"""Clipboard-card expansion inside one fixed native host.

Only cached child surfaces are transformed. Native bounds are allocated once
per presentation; no root graphics effect, global input hook or clipboard I/O.
"""
from collections import OrderedDict
import sys
from PySide6.QtCore import QCoreApplication,QEvent,QObject,QPoint,QRect,QRectF,Qt,QVariantAnimation,QSignalBlocker,QTimer,QEasingCurve
from PySide6.QtGui import QColor,QGuiApplication,QPainter,QPainterPath,QPainterPathStroker,QPixmap,QContextMenuEvent
from PySide6.QtWidgets import QApplication,QWidget
from shiboken6 import isValid
from ui_transitions import exit_curve,motion_allowed


class ClipboardPopupMotion(QObject):
    def __init__(self,window):
        super().__init__(window);self.window=window;self.source=None;self.owner=None
        self.progress=0.;self.target=0.;self.presenting=False;self.transitioning=False
        self.source_rect=QRectF();self.target_rect=QRectF();self.host_rect=QRect()
        self.source_image=QPixmap();self.target_image=QPixmap();self._shadows=OrderedDict();self._app_filter=False;self._anchors=[];self.intent=0
        self._swallow_button=Qt.NoButton
        self._swallow_context=False
        self._release_timer=QTimer(self);self._release_timer.setSingleShot(True);self._release_timer.setInterval(1500)
        self._release_timer.timeout.connect(self._finish_pointer_swallow)
        self.animation=QVariantAnimation(self);self.animation.setEasingCurve(exit_curve())
        # A geometry-only ~0.8% rebound. Alpha is clamped before that rebound;
        # one progress value still owns both directions and reversals.
        self._geometry_curve=QEasingCurve(QEasingCurve.OutBack);self._geometry_curve.setOvershoot(.5)
        self.animation.valueChanged.connect(self._frame);self.animation.finished.connect(self._finished)
        window.installEventFilter(self)

    def _allowed(self):return bool(getattr(self.window.manager,'motion',True) and motion_allowed())

    def available(self,source):
        point=source.mapToGlobal(source.rect().center()) if source is not None else self.window.manager.shelf.frameGeometry().center()
        screen=QGuiApplication.screenAt(point) or self.window.manager.shelf.screen() or QGuiApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else QRect(0,0,1024,768)

    def _unbind(self):
        anchors,self._anchors=self._anchors,[]
        for widget,connection in anchors:
            if widget is not None and isValid(widget):
                widget.removeEventFilter(self)
            QObject.disconnect(connection)
        if self._app_filter and self._swallow_button==Qt.NoButton and not self._swallow_context:
            app=QApplication.instance()
            if app is not None:app.removeEventFilter(self)
            self._app_filter=False

    def _finish_pointer_swallow(self):
        self._swallow_button=Qt.NoButton;self._swallow_context=False;self._release_timer.stop()
        if not self.presenting:self._unbind()

    def _bind(self,source):
        self._unbind();self.source=source
        self.owner=source.window() if source is not None else self.window.manager.shelf
        for widget in (self.source,self.owner):
            if widget is not None:
                widget.installEventFilter(self);self._anchors.append((widget,widget.destroyed.connect(self._anchor_destroyed)))
        app=QApplication.instance()
        if app is not None:app.installEventFilter(self);self._app_filter=True

    def _anchor_destroyed(self,*_):
        if isValid(self.window):self.dismiss(immediate=True)

    def open(self,source):
        window=self.window
        if source is not None and (not isValid(source) or not source.isVisible()):source=None
        owner=source.window() if source is not None else window.manager.shelf
        if not owner.isVisible():return
        self.intent+=1
        if self.presenting:
            # A quick reopen resumes this very pose. Changing the source while
            # visible only changes the eventual destination, never native bounds.
            if source is not None and source is not self.source:
                if not self.host_rect.contains(QRect(source.mapToGlobal(QPoint()),source.size())):
                    self.dismiss(immediate=True);self.open(source);return
                self._bind(source);self.source_rect=QRectF(QPoint(source.mapToGlobal(QPoint())),source.size())
            self._animate(1.);return
        self._bind(source);area=self.available(source).adjusted(8,8,-8,-8)
        window.refresh();hint=window.sizeHint()
        width=max(1,min(hint.width()-40,area.width()-40));height=max(1,min(hint.height()-40,area.height()-40))
        visual=getattr(self.owner,'visual_geometry',None)
        center=(visual() if callable(visual) else self.owner.frameGeometry()).center()
        target=QRect(center.x()-width//2,center.y()-height//2,width,height)
        target.moveLeft(max(area.left()+20,min(target.left(),area.right()-20-width+1)))
        target.moveTop(max(area.top()+20,min(target.top(),area.bottom()-20-height+1)))
        self.target_rect=QRectF(target)
        self.source_rect=QRectF(QPoint(source.mapToGlobal(QPoint())),source.size()) if source is not None else QRectF(target.center().x()-16,target.center().y()-16,32,32)
        self.host_rect=target.adjusted(-20,-20,20,20).united(self.source_rect.toAlignedRect().adjusted(-2,-2,2,2)).intersected(area)
        self.presenting=True;self.progress=0.;self.target=1.;self.transitioning=True
        window.setGeometry(self.host_rect)
        window.surface.setGeometry(target.translated(-self.host_rect.topLeft()))
        window.surface.show();window._layout_surface()
        self.source_image=source.grab() if source is not None else QPixmap()
        self.target_image=window._capture_surface();window.surface.hide()
        window._show_host();window.raise_();window.activateWindow()
        self._animate(1.)

    def refresh_snapshot(self):
        if self.presenting and self.transitioning:self.target_image=self.window._capture_surface();self.window.update()

    def _animate(self,target):
        current=self.progress;self.animation.stop();self.target=float(target)
        if abs(self.target-current)<.000001 or not self._allowed():
            self._frame(self.target);self._finished();return
        if self.target==0. and not self.transitioning:
            self.target_image=self.window._capture_surface()
            self.source_image=self.source.grab() if self.source is not None and isValid(self.source) and self.source.isVisible() else QPixmap()
        self.transitioning=True;self.window.surface.hide()
        # Changing duration/start/end while the old clock has a nonzero time
        # emits intermediate values. Configure atomically before restarting.
        with QSignalBlocker(self.animation):
            self.animation.setDuration(max(80,round((230 if target else 180)*abs(self.target-current))))
            self.animation.setStartValue(current);self.animation.setEndValue(self.target);self.animation.setCurrentTime(0)
        self._frame(current);self.animation.start()

    def dismiss(self,*,immediate=False):
        if not self.presenting:return
        self.intent+=1
        if immediate:self.animation.stop();self.target=0.;self.progress=0.;self._finished()
        else:self._animate(0.)

    def _frame(self,value):
        if self.animation.state()==self.animation.State.Running and not self._allowed():
            self.animation.stop();self.progress=self.target;self.window.update();self._finished();return
        self.progress=float(value);self.window.update()

    def _finished(self):
        self.transitioning=False
        if self.target:
            self.progress=1.;self.window.surface.show();self.window.update()
        else:
            self.presenting=False;self.progress=0.;self._unbind();self.source=None;self.owner=None
            self.source_image=QPixmap();self.target_image=QPixmap()
            # QDialog.finished may synchronously open a new presentation. All
            # old cleanup must precede that callback, never erase its anchors.
            self.window._complete_close();return
        self.source_image=QPixmap();self.target_image=QPixmap()

    def hidden(self):
        self.intent+=1
        self.animation.stop();self.presenting=False;self.transitioning=False;self.progress=0.;self.target=0.
        self._unbind();self.source=None;self.owner=None;self.source_image=QPixmap();self.target_image=QPixmap()

    def current_rect(self):
        a,b=self.source_rect,self.target_rect
        t=self._geometry_curve.valueForProgress(max(0.,min(1.,self.progress)))
        width=a.width()+(b.width()-a.width())*t;height=a.height()+(b.height()-a.height())*t
        # Rebound around the final center rather than throwing the center past
        # the screen edge when the source button is far from the preview.
        position=max(0.,min(1.,t));center=a.center()+(b.center()-a.center())*position
        return QRectF(center.x()-width/2,center.y()-height/2,width,height)

    def contains_global(self,point):
        if not self.presenting:return False
        if self.current_rect().adjusted(-6,-6,6,6).contains(point):return True
        if self.source_rect.adjusted(-4,-4,4,4).contains(point):return True
        # The small pointer bridge lets users reach a detached preview without
        # treating the entire large transparent native reservation as hovered.
        start=self.source_rect.center();end=self.target_rect.center()
        path=QPainterPath(start);path.lineTo(end);stroke=QPainterPathStroker();stroke.setWidth(28)
        return stroke.createStroke(path).contains(point)

    def eventFilter(self,watched,event):
        if QCoreApplication.closingDown() or not isValid(self.window):return False
        kind=event.type()
        if self._swallow_context and kind==QEvent.MouseButtonPress:
            self._finish_pointer_swallow()
        if self._swallow_context and kind==QEvent.ContextMenu and event.reason()==QContextMenuEvent.Mouse:
            # A native right-button release may post its context request after
            # the popup has hidden. Consume that request, never a new click.
            self._finish_pointer_swallow();event.accept();return True
        if self._swallow_button!=Qt.NoButton and kind==QEvent.MouseButtonRelease and event.button()==self._swallow_button:
            if self._swallow_button==Qt.RightButton:
                self._swallow_button=Qt.NoButton;self._swallow_context=True
                self._release_timer.start(QApplication.doubleClickInterval())
            else:self._finish_pointer_swallow()
            event.accept();return True
        if self._swallow_button!=Qt.NoButton and kind in (QEvent.MouseButtonDblClick,QEvent.ContextMenu):
            event.accept();return True
        if watched is self.window and kind==QEvent.Hide:self.hidden()
        elif watched is self.window and kind==QEvent.WindowDeactivate and self.presenting:
            epoch=self.intent;QTimer.singleShot(0,self,lambda:self._deactivated(epoch))
        elif self.presenting and watched in (self.owner,self.source) and kind in (QEvent.Hide,QEvent.Close,QEvent.DeferredDelete):self.dismiss(immediate=True)
        elif self.presenting and watched is self.owner and (kind in (QEvent.Move,QEvent.Resize) or (kind==QEvent.WindowStateChange and self.owner.isMinimized())):self.dismiss(immediate=True)
        elif self.presenting and kind in (QEvent.MouseButtonPress,QEvent.MouseButtonDblClick) and isinstance(watched,QWidget):
            point=event.globalPosition().toPoint()
            if self.current_rect().contains(point):
                # Consume both halves, even if the exit ends before release.
                # Content is a view, never a text selection/image gesture or
                # a click which falls through onto the underlying shelf.
                self._swallow_button=event.button();self._release_timer.start(1500);self.dismiss();event.accept();return True
            # Outside clicks retain their original target/action.
            if not self.source_rect.contains(point):self.dismiss()
        elif self.presenting and kind in (QEvent.Wheel,QEvent.ContextMenu) and isinstance(watched,QWidget):
            if watched is self.window or self.window.isAncestorOf(watched):event.accept();return True
        return False

    def _deactivated(self,epoch):
        if not self.presenting or self.intent!=epoch or QApplication.activeWindow() is self.window:return
        popup=QApplication.activePopupWidget()
        while popup is not None:
            if popup is self.window:return
            popup=popup.parentWidget()
        # A local window activation event also covers clicks into another app
        # while the shelf is pinned. A newer source-button intent wins instead.
        self.dismiss()

    def paint(self,painter):
        if not self.transitioning:return
        from surface_morph import paint_surface_shadow
        rect=self.current_rect().translated(-self.window.x(),-self.window.y())
        t=max(0.,min(1.,self.progress));dark=self.window.manager.settings.theme=='dark';c=QColor('#1C1C1E' if dark else '#FFFFFF')
        shadow=QColor(0,0,0,107) if dark else QColor(18,31,53,41)
        painter.save();painter.setOpacity(t)
        paint_surface_shadow(painter,rect,self._shadows,20,QPoint(0,4),shadow);painter.restore()
        radius=8+((16 if sys.platform=='darwin' else 10)-8)*t
        path=QPainterPath();path.addRoundedRect(rect,radius,radius)
        painter.save();painter.setClipPath(path);painter.fillRect(rect,c)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        # Finish the thumbnail→content handoff when geometry first reaches its
        # target. A stretched source glyph must not ghost over the large card
        # while the size makes its tiny final rebound. This alpha is monotonic
        # and never inherits OutBack's >1 geometry value.
        content_mix=max(0.,min(1.,self._geometry_curve.valueForProgress(t)))
        if not self.source_image.isNull():
            painter.setOpacity(1-content_mix);painter.drawPixmap(rect,self.source_image,QRectF(self.source_image.rect()))
        if not self.target_image.isNull():
            painter.setOpacity(content_mix);painter.drawPixmap(rect,self.target_image,QRectF(self.target_image.rect()))
        painter.restore()
