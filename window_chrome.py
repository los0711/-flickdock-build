"""Resizable utility windows; no hooks, polling or changes to shelf geometry."""
from __future__ import annotations
import atexit,json,sys,weakref
from pathlib import Path
from PySide6.QtCore import QCoreApplication,QObject,QEvent,QPoint,QRect,QRectF,QSize,Qt,QTimer
from PySide6.QtGui import QColor,QCursor,QGuiApplication,QPainter,QPen
from PySide6.QtWidgets import QApplication,QPushButton,QWidget
from platform_native import native_input_available
from shiboken6 import isValid

_dock_windows=weakref.WeakSet()
_chromes=weakref.WeakSet()

def _shutdown_chrome():
    app=QApplication.instance()
    if app is not None and isValid(app):
        for chrome in tuple(_chromes):
            if isValid(chrome):app.removeEventFilter(chrome)

atexit.register(_shutdown_chrome)

def minimize_utility(window):
    dock_window_state(window,True)
    window.showMinimized()

def dock_window_state(window,needed,*,appkit=None):
    if sys.platform!='darwin' and appkit is None:return
    if needed:_dock_windows.add(window)
    else:_dock_windows.discard(window)
    if appkit is None:
        if not native_input_available():return
        import AppKit as appkit
    policy=appkit.NSApplicationActivationPolicyRegular if _dock_windows else appkit.NSApplicationActivationPolicyAccessory
    appkit.NSApplication.sharedApplication().setActivationPolicy_(policy)


class _WindowButton(QPushButton):
    def __init__(self,window,role):
        super().__init__('',window);self.owner=window;self.role=role
        self._keyboard_focus=False
        self.setFixedSize(44,32);self.setAutoDefault(False);self.setDefault(False)
        self.setAccessibleName({'min':'最小化','max':'最大化或还原','close':'关闭窗口'}[role]);self.setToolTip(self.accessibleName())
        self.setStyleSheet('QPushButton{border:0;border-radius:0;background:transparent;padding:0;}')
        self.clicked.connect(self._activate)
    @property
    def glyph_rect(self):
        rect=QRectF(0,0,14,14);rect.moveCenter(QRectF(self.rect()).center());return rect
    def focusInEvent(self,event):
        self._keyboard_focus=event.reason() in (Qt.TabFocusReason,Qt.BacktabFocusReason,Qt.ShortcutFocusReason)
        super().focusInEvent(event);self.update()
    def focusOutEvent(self,event):
        self._keyboard_focus=False;super().focusOutEvent(event);self.update()
    def mousePressEvent(self,event):
        self._keyboard_focus=False;super().mousePressEvent(event);self.update()
    def _activate(self):
        if self.role=='min':self.owner.showMinimized()
        elif self.role=='max':self.owner.showNormal() if self.owner.isMaximized() else self.owner.showMaximized()
        else:self.owner.close()
    def paintEvent(self,event):
        from reference_widgets import palette,ink
        from native_ui_common import colors
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        c=colors('dark' if self.dark else 'light') if hasattr(self,'dark') else colors(self.owner._theme) if hasattr(self.owner,'_theme') else palette()
        if self.underMouse():p.fillRect(self.rect(),ink('#E81123' if self.role=='close' else c['hover']))
        p.setPen(QPen(QColor('white' if self.role=='close' and self.underMouse() else c['secondary']),1.2))
        x,y=self.width()//2,self.height()//2
        if self.role=='min':p.drawLine(x-5,y+2,x+5,y+2)
        elif self.role=='close':p.drawLine(x-5,y-5,x+5,y+5);p.drawLine(x+5,y-5,x-5,y+5)
        elif self.owner.isMaximized():p.drawRect(x-3,y-5,8,8);p.fillRect(x-5,y-3,8,8,QColor(c['surface']));p.drawRect(x-5,y-3,8,8)
        else:p.drawRect(x-5,y-5,10,10)
        if self.hasFocus() and self._keyboard_focus:
            p.setPen(QPen(QColor(c['blue']),1.5));p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(self.rect()).adjusted(2,2,-2,-2))
        p.end()


class WindowChrome(QObject):
    def __init__(self,window,title_bar,settings,key,*,surface=None):
        super().__init__(window);self.window=window;self.title=title_bar;self.surface=surface or window
        self.key=key;self.path=Path(settings.base)/'window-sizes.json';self._drag=None;self._cursor=False
        self._normal=window.size();self._restoring=False
        if sys.platform=='win32':
            window.setWindowFlags(window.windowFlags()|Qt.WindowMinimizeButtonHint|Qt.WindowMaximizeButtonHint|Qt.WindowSystemMenuHint)
            old=getattr(window,'close_button',None)
            if old is not None:title_bar.layout().removeWidget(old);old.hide()
            controls=QWidget(title_bar)
            from PySide6.QtWidgets import QHBoxLayout
            row=QHBoxLayout(controls);row.setContentsMargins(0,0,0,0);row.setSpacing(0)
            self.buttons={role:_WindowButton(window,role) for role in ('min','max','close')}
            for button in self.buttons.values():row.addWidget(button)
            window.close_button=self.buttons['close'];title_bar.layout().addWidget(controls)
            if old is not None:window.close_button.setAccessibleName(old.accessibleName())
        self._save_timer=QTimer(self);self._save_timer.setSingleShot(True);self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self.save_size)
        self.restore_size();QApplication.instance().installEventFilter(self)
        _chromes.add(self)
        window.finished.connect(lambda *_:dock_window_state(window,False))
        from ui_transitions import WindowFade
        window._window_fade=WindowFade(window)

    def _available(self):
        screen=QGuiApplication.screenAt(QCursor.pos()) or self.window.screen() or QGuiApplication.primaryScreen()
        return screen.availableGeometry() if screen else QRect(0,0,1024,768)

    def restore_size(self):
        size=None
        try:
            if self.path.stat().st_size<=16384:
                data=json.loads(self.path.read_text(encoding='utf8')).get(self.key)
                if isinstance(data,list) and len(data)==2 and all(type(n) is int and 200<=n<=16000 for n in data):size=QSize(*data)
        except (OSError,ValueError,AttributeError):pass
        if size is not None:self.fit_screen(size)

    def fit_screen(self,size=None):
        size=size or self.window.size()
        if self.window.isMaximized() or self.window.isMinimized():return
        area=self._available();minimum=self.window.minimumSize()
        # Scrollable windows keep every control available on short screens.
        self.window.setMinimumSize(min(minimum.width(),max(320,area.width()-16)),min(minimum.height(),max(260,area.height()-16)))
        self._restoring=True
        self.window.resize(max(self.window.minimumWidth(),min(size.width(),area.width()-16)),max(self.window.minimumHeight(),min(size.height(),area.height()-16)))
        self._normal=self.window.size();self._restoring=False

    def save_size(self):
        self._save_timer.stop()
        try:
            data=json.loads(self.path.read_text(encoding='utf8')) if self.path.exists() and self.path.stat().st_size<=16384 else {}
            if not isinstance(data,dict):data={}
            data[self.key]=[self._normal.width(),self._normal.height()]
            self.path.parent.mkdir(parents=True,exist_ok=True)
            temporary=self.path.with_suffix('.tmp');temporary.write_text(json.dumps(data),encoding='utf8');temporary.replace(self.path)
        except (OSError,ValueError):pass

    def _edges(self,point):
        if self.window.isMaximized() or self.window.isMinimized():return Qt.Edges()
        rect=self.surface.geometry() if self.surface is not self.window else self.window.rect()
        if not rect.adjusted(-5,-5,5,5).contains(point):return Qt.Edges()
        edges=Qt.Edges()
        if abs(point.x()-rect.left())<=5:edges|=Qt.LeftEdge
        if abs(point.x()-rect.right())<=5:edges|=Qt.RightEdge
        if abs(point.y()-rect.top())<=5:edges|=Qt.TopEdge
        if abs(point.y()-rect.bottom())<=5:edges|=Qt.BottomEdge
        return edges

    def eventFilter(self,watched,event):
        if QCoreApplication.closingDown() or not isValid(self.window):return False
        w=self.window;kind=event.type()
        if watched is w:
            if kind==QEvent.Resize and not w.isMaximized() and not w.isMinimized() and not self._restoring:
                self._normal=w.size();self._save_timer.start()
            elif kind==QEvent.Hide:
                self.save_size()
                if not w.isMinimized():dock_window_state(w,False)
            elif kind==QEvent.WindowStateChange:
                dock_window_state(w,w.isVisible() or w.isMinimized())
            elif kind==QEvent.Show:dock_window_state(w,True)
        if not isinstance(watched,QWidget) or watched.window() is not w:return False
        if kind not in (QEvent.MouseButtonPress,QEvent.MouseButtonDblClick,QEvent.MouseMove,QEvent.MouseButtonRelease):return False
        global_point=event.globalPosition().toPoint();point=w.mapFromGlobal(global_point)
        edges=self._edges(point)
        if kind==QEvent.MouseMove:
            if self._drag:
                start,rect,drag_edges=self._drag;delta=global_point-start;target=QRect(rect)
                if not drag_edges:target.moveTopLeft(rect.topLeft()+delta)
                else:
                    if drag_edges&Qt.LeftEdge:target.setLeft(min(rect.left()+delta.x(),rect.right()-w.minimumWidth()+1))
                    if drag_edges&Qt.RightEdge:target.setRight(max(rect.right()+delta.x(),rect.left()+w.minimumWidth()-1))
                    if drag_edges&Qt.TopEdge:target.setTop(min(rect.top()+delta.y(),rect.bottom()-w.minimumHeight()+1))
                    if drag_edges&Qt.BottomEdge:target.setBottom(max(rect.bottom()+delta.y(),rect.top()+w.minimumHeight()-1))
                w.setGeometry(target);event.accept();return True
            if edges:
                diagonal=(bool(edges&Qt.LeftEdge) and bool(edges&Qt.TopEdge)) or (bool(edges&Qt.RightEdge) and bool(edges&Qt.BottomEdge))
                cursor=Qt.SizeFDiagCursor if diagonal else Qt.SizeBDiagCursor if (edges&(Qt.LeftEdge|Qt.RightEdge) and edges&(Qt.TopEdge|Qt.BottomEdge)) else Qt.SizeHorCursor if edges&(Qt.LeftEdge|Qt.RightEdge) else Qt.SizeVerCursor
                w.setCursor(cursor);self._cursor=True
            elif self._cursor:w.unsetCursor();self._cursor=False
        if kind==QEvent.MouseButtonRelease and self._drag:self._drag=None;event.accept();return True
        if kind==QEvent.MouseButtonPress and event.button()==Qt.LeftButton and edges:
            if not (native_input_available() and w.windowHandle() and w.windowHandle().startSystemResize(edges)):
                self._drag=(global_point,QRect(w.geometry()),edges)
            event.accept();return True
        header=watched is self.title or self.title.isAncestorOf(watched)
        interactive=isinstance(watched,QPushButton) or watched.cursor().shape()==Qt.PointingHandCursor or watched.focusPolicy()!=Qt.NoFocus
        if header and not interactive and event.button()==Qt.LeftButton:
            if kind==QEvent.MouseButtonDblClick:
                w.showNormal() if w.isMaximized() else w.showMaximized();event.accept();return True
            if kind==QEvent.MouseButtonPress:
                if not (native_input_available() and w.windowHandle() and w.windowHandle().startSystemMove()):
                    if not w.isMaximized():self._drag=(global_point,QRect(w.geometry()),Qt.Edges())
                event.accept();return True
        return False
