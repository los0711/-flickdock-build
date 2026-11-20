"""Spring presentation for FlickDock's native popup windows."""
import math
import time
import weakref

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QRect, QRectF, QTimer, Qt, Slot
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QMenu, QWidget
from shiboken6 import isValid

from flickdock_motion import Spring


class StretchHighlight(QObject):
    """The side moving toward the next row leads the trailing side."""
    def __init__(self, widget):
        super().__init__(widget)
        self.target=QRectF()
        self.top=Spring(self,{'y':0.},lambda _:widget.update())
        self.bottom=Spring(self,{'y':0.},lambda _:widget.update())

    def move(self, target):
        from native_ui_common import reduced_motion
        target=QRectF(target)
        if target==self.target:return
        before=self.target
        self.target=target
        if before.isEmpty() or target.isEmpty() or reduced_motion():
            self.top.jump({'y':target.top()})
            self.bottom.jump({'y':target.bottom()})
        else:
            descending=target.center().y()>before.center().y()
            self.top.animate_physical({'y':target.top()},*((200,20) if descending else (620,36)))
            self.bottom.animate_physical({'y':target.bottom()},*((620,36) if descending else (200,20)))

    def paint(self,painter,color,radius,clip=None):
        if self.target.isEmpty():return False
        from native_ui_common import paint_rounded_surface
        low,high=self.top.values['y'],self.bottom.values['y']
        rect=QRectF(self.target.left(),low,self.target.width(),max(12,high-low))
        painter.save()
        if clip is not None:painter.setClipRect(clip,Qt.IntersectClip)
        paint_rounded_surface(painter,rect,radius,color)
        painter.restore()
        return True


def install_item_highlight(view, insets):
    if hasattr(view,'_dropdown_highlight'):return
    marker=view._dropdown_highlight=StretchHighlight(view.viewport())

    def set_index(index):
        rect=QRectF(view.visualRect(index))
        marker.move(rect.adjusted(*insets) if not rect.isEmpty() else rect)

    view.setMouseTracking(True)
    view.entered.connect(set_index)
    view.selectionModel().currentChanged.connect(set_index)
    view.verticalScrollBar().valueChanged.connect(lambda _:marker.move(QRectF()))


class DropdownTextRoll(QObject):
    def __init__(self, widget, *, combo=False):
        super().__init__(widget)
        self.widget=widget
        self.old=''
        self.pending=('',widget.currentText()) if combo else ('','')
        self.spring=Spring(self,{'roll':1.},lambda _:widget.update())
        if combo:
            widget.currentTextChanged.connect(self._record)
            widget.activated.connect(self._activated)

    def _record(self, text):
        self.pending=(self.pending[1],text)

    def _activated(self, _):
        old,new=self.pending
        self.pending=(new,new)
        self.begin(old,new)

    def begin(self, old, new):
        from native_ui_common import reduced_motion
        if old==new or not self.widget.isVisible() or reduced_motion():return
        self.old=old
        self.spring.jump({'roll':0.})
        self.spring.animate_physical({'roll':1.},340,26)

    @property
    def running(self):
        return self.spring._timer.isActive()

    def paint(self,painter,rect,text):
        def draw(value,y,alpha):
            value=painter.fontMetrics().elidedText(value,Qt.ElideRight,max(1,round(rect.width())))
            painter.setOpacity(alpha)
            painter.drawText(rect.translated(0,y),Qt.AlignLeft|Qt.AlignVCenter,value)
        if not self.running:
            draw(text,0,1.)
            return
        at=self.spring.values['roll']
        visible=max(0.,min(1.,at))
        painter.save();painter.setClipRect(rect,Qt.IntersectClip)
        draw(self.old,-20*at,1-visible)
        draw(text,20*(1-at),visible)
        painter.restore()


class _PopupPresentation(QWidget):
    """One input-transparent image above the live popup during motion."""

    def __init__(self,owner,controller,*,topmost=False,inline=False):
        flags=Qt.Widget if inline else (
            Qt.ToolTip | Qt.FramelessWindowHint | Qt.WindowTransparentForInput
            | Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint)
        if topmost and not inline:flags|=Qt.WindowStaysOnTopHint
        super().__init__(owner,flags)
        self.inline=inline
        if inline:
            self.setAttribute(Qt.WA_NoSystemBackground)
            self.setAttribute(Qt.WA_TransparentForMouseEvents)
        else:
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.controller = controller

    def paintEvent(self, event):
        motion = self.controller
        progress = max(0., min(1.06, motion.height.values['height']))
        opacity = max(0., min(1., motion.alpha.values['opacity']))
        p = QPainter(self)
        if self.inline:
            # Use the menu's backing store. Creating another top-level
            # window here can interrupt the native QMenu mouse grab.
            p.setCompositionMode(QPainter.CompositionMode_Source)
            p.fillRect(self.rect(),Qt.transparent)
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        if progress <= 0 or opacity <= 0 or motion.snapshot.isNull():
            p.end();return
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        p.setOpacity(opacity)
        width, height = motion.snapshot.deviceIndependentSize().toTuple()
        scale = .9 + .1 * progress
        p.translate(width * (1-scale)/2, 0)
        p.scale(scale, 1.)
        reveal = min(self.height(), height*progress)
        if motion.upward:
            p.translate(0,height-reveal)
        outline=QPainterPath()
        radius=min(motion.radius,reveal/2)
        outline.addRoundedRect(QRectF(0,0,width,reveal),radius,radius)
        p.setClipPath(outline)
        if motion.upward:
            p.translate(0,reveal-height)
        if not motion.rows:
            p.drawPixmap(0,0,motion.snapshot)
        else:
            motion.paint_rows(p, opacity)
        p.end()


class DropdownMotion(QObject):
    def __init__(self, popup):
        parent=popup.parentWidget()
        owner=parent.window() if parent is not None else None
        # The tray's native menu has no parent window. It is its own frame-clock
        # owner, so the animation still has a QWidget in its parent chain.
        super().__init__(owner if owner is not None else popup)
        self.owner=owner
        self.popup=weakref.ref(popup)
        self._owned_menu=False
        self._inline_open=bool(popup.property('flickdockInlineDropdown'))
        self.presentation=None
        self.snapshot=QPixmap()
        self.rows=()
        self.scene=QPainterPath()
        self.radius=12.
        self.upward=False
        self.fill=QColor()
        self.started=0.
        self.opened=False
        self.enabled=False
        self.height=Spring(self,{'height':0.},self._frame)
        self.alpha=Spring(self,{'opacity':0.},self._frame)
        self.height.settled.connect(self._settle)
        self.alpha.settled.connect(self._settle)
        self.after_layout=QTimer(self)
        self.after_layout.setSingleShot(True)
        self.after_layout.timeout.connect(self._open)
        popup.installEventFilter(self)
        popup.destroyed.connect(self._popup_gone)
        if owner is not None:owner.installEventFilter(self)

    def _live(self):
        widget=self.popup()
        return widget if widget is not None and isValid(widget) else None

    def eventFilter(self, watched, event):
        if QCoreApplication.closingDown() or not hasattr(self,'owner'):return False
        if watched is self.owner and event.type() in (QEvent.Hide,QEvent.Close):
            self._clear()
        elif watched is self._live():
            if event.type()==QEvent.Show:
                from native_ui_common import reduced_motion
                self.enabled=(not reduced_motion() and QGuiApplication.platformName() in ('windows','cocoa'))
                self.opened=True
                if self.enabled:
                    if not self._inline_open:watched.setWindowOpacity(.01)
                    self.after_layout.start(0)
            elif event.type()==QEvent.Hide:
                self.opened=False
                self.after_layout.stop()
                if self.enabled and self.presentation is not None and not self.snapshot.isNull():
                    if self._inline_open:
                        self._present(watched,inline=False)
                    self.presentation.show()
                    self._order_presentation()
                    self.height.animate_physical({'height':0.},300,23)
                    self.alpha.animate_physical({'opacity':0.},400,34)
                else:self._clear()
        return False

    def _capture_rows(self, popup):
        if isinstance(popup,QMenu):
            return tuple(QRectF(popup.actionGeometry(action)) for action in popup.actions()
                         if action.isVisible() and not action.isSeparator() and not popup.actionGeometry(action).isEmpty())
        view=popup.findChild(QAbstractItemView)
        if view is None or not view.isVisible():return ()
        viewport=view.viewport()
        origin=viewport.mapTo(popup,QPoint())
        entries=[]
        y=0
        while y<viewport.height() and len(entries)<10:
            index=view.indexAt(QPoint(max(0,viewport.width()//3),y))
            row=view.visualRect(index)
            if not index.isValid() or row.isEmpty():
                y+=4
                continue
            clipped=row.intersected(viewport.rect())
            if not clipped.isEmpty():entries.append(QRectF(clipped.translated(origin)))
            y=max(y+1,row.bottom()+1)
        return tuple(entries)

    def _open(self):
        popup=self._live()
        if popup is None or not popup.isVisible():return
        from native_ui_common import colors, qss_color, RADIUS_CARD_PX, RADIUS_PANEL_PX
        import ui_theme
        if self.owner is not None:
            previous=getattr(self.owner,'_moving_dropdown',None)
            if previous is not None and previous is not self:previous._clear()
            self.owner._moving_dropdown=self
        if self._inline_open and self.presentation is not None:
            self.presentation.hide()
        self.snapshot=popup.grab()
        if self.snapshot.isNull():
            self._clear()
            return
        self.rows=self._capture_rows(popup)
        self.scene=QPainterPath()
        self.scene.addRect(QRectF(0,0,*self.snapshot.deviceIndependentSize().toTuple()))
        for row in self.rows:self.scene.addRect(row)
        self.radius=getattr(popup,'_popup_clip_radius',RADIUS_CARD_PX if isinstance(popup,QMenu) else RADIUS_PANEL_PX)
        theme=popup.property('dropdownTheme') or getattr(popup,'surface_theme',None) or ui_theme.current_theme()
        self.fill=qss_color(colors(theme)['popup_surface'])
        anchor=getattr(popup,'_dropdown_anchor',None)
        if anchor is None and popup.parentWidget() is not self.owner:anchor=popup.parentWidget()
        if anchor is not None and not isValid(anchor):anchor=None
        point=popup.mapToGlobal(QPoint())
        self.upward=bool(anchor is not None and point.y()+popup.height()<=anchor.mapToGlobal(QPoint()).y()+2)
        self._owned_menu=bool(popup.property('flickdockOwnedShelfMenu'))
        self._present(popup,inline=self._inline_open)
        self.started=time.monotonic()
        self.presentation.show()
        self._order_presentation()
        self.height.animate_physical({'height':1.},300,23)
        self.alpha.animate_physical({'opacity':1.},400,34)

    def _present(self,popup,*,inline):
        if self.presentation is not None and self.presentation.inline!=inline:
            self.presentation.hide()
            self.presentation.deleteLater()
            self.presentation=None
        if self.presentation is None:
            topmost=bool(popup.windowFlags() & Qt.WindowStaysOnTopHint)
            self.presentation=_PopupPresentation(popup if inline else self.owner,self,topmost=topmost,inline=inline)
        bounds=popup.rect() if inline else QRect(popup.mapToGlobal(QPoint()),popup.size())
        self.presentation.setGeometry(bounds)
        if inline:self.presentation.raise_()

    def paint_rows(self, painter, opacity):
        """Leave the frame fixed and let just the visible rows drop into it."""
        progress=self.height.values['height']
        if not self.opened or progress>=.999 and time.monotonic()-self.started>.44:
            painter.drawPixmap(0,0,self.snapshot)
            return
        painter.save();painter.setClipPath(self.scene,Qt.IntersectClip);painter.drawPixmap(0,0,self.snapshot);painter.restore()
        elapsed=time.monotonic()-self.started
        ratio=self.snapshot.devicePixelRatio()
        for i,row in enumerate(self.rows):
            painter.fillRect(row,self.fill)
            age=max(0.,elapsed-.05-.045*i)
            # Analytic mass-one 300/24 spring from rest.
            frequency=math.sqrt(300-12**2)
            presence=1-math.exp(-12*age)*(math.cos(frequency*age)+12/frequency*math.sin(frequency*age))
            painter.save()
            painter.setClipRect(row,Qt.IntersectClip)
            painter.setOpacity(opacity*max(0.,min(1.,presence)))
            source=QRectF(row.x()*ratio,row.y()*ratio,row.width()*ratio,row.height()*ratio)
            painter.drawPixmap(row.translated(0,(presence-1)*14),self.snapshot,source)
            painter.restore()

    def _order_presentation(self):
        if self.presentation.inline or self.owner is None or not isValid(self.owner):return
        popup=self._live()
        if self._owned_menu:
            from platform_native import raise_owned_popup
            # This layer is also used briefly after the menu hides, so own it
            # by the shelf and order it above the live menu while both show.
            raise_owned_popup(self.presentation,self.owner,above=popup)
        elif popup is not None and popup.windowFlags() & Qt.WindowStaysOnTopHint and getattr(self.owner,'_native_revealed',False):
            from platform_native import raise_edge_target
            raise_edge_target(self.presentation,above=popup)

    def _frame(self, _):
        if self.presentation is not None:self.presentation.update()

    def _settle(self):
        if self.height._timer.isActive() or self.alpha._timer.isActive():return
        if self.opened:
            popup=self._live()
            if popup is not None:
                popup.setWindowOpacity(1.)
                if self._owned_menu and self.owner is not None and isValid(self.owner):
                    from platform_native import raise_owned_popup
                    raise_owned_popup(popup,self.owner)
            if self.presentation is not None:self.presentation.hide()
        else:self._clear()

    @Slot()
    def _popup_gone(self):
        if not hasattr(self,'presentation'):return
        if self.opened or self.presentation is None:
            self._clear()
        if self.presentation is None:self.deleteLater()

    def _clear(self):
        self.after_layout.stop()
        self.height.stop();self.alpha.stop()
        if self.presentation is not None:
            self.presentation.hide()
            self.presentation.deleteLater()
            self.presentation=None
        popup=self._live()
        if popup is not None:popup.setWindowOpacity(1.)
        self.snapshot=QPixmap()
        if self.owner is not None and isValid(self.owner) and getattr(self.owner,'_moving_dropdown',None) is self:
            self.owner._moving_dropdown=None
        if popup is None:self.deleteLater()


def install_dropdown_motion(popup):
    if not hasattr(popup,'_dropdown_motion'):
        popup._dropdown_motion=DropdownMotion(popup)
