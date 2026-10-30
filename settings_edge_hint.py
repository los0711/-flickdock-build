"""A nonactivating, clickable close-settings handoff next to the edge strip."""
from PySide6.QtCore import QPoint,QRect,Qt
from PySide6.QtWidgets import QWidget,QFrame,QVBoxLayout,QLabel,QPushButton
from platform_native import keep_tool_visible_when_inactive,raise_edge_target


class SettingsEdgeHint(QWidget):
    def show(self):
        self._window_fade.reopen();super().show()

    def hide(self):
        if hasattr(self,'_window_fade') and self._window_fade.defer_done(None,lambda _:super(SettingsEdgeHint,self).hide()):return
        super().hide()

    def __init__(self,manager):
        super().__init__();self.manager=manager
        self.setWindowFlags(Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint|Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating);self.setAttribute(Qt.WA_TranslucentBackground)
        keep_tool_visible_when_inactive(self)
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0)
        self.card=QFrame(self);layout.addWidget(self.card)
        inside=QVBoxLayout(self.card);inside.setContentsMargins(14,12,14,12);inside.setSpacing(10)
        self.message=QLabel('关闭设置后才可正常使用',self.card);self.message.setWordWrap(True);inside.addWidget(self.message)
        self.close_button=QPushButton('关闭设置',self.card);self.close_button.setMinimumHeight(34)
        self.close_button.clicked.connect(manager.close_settings_from_edge);inside.addWidget(self.close_button)
        self.setFixedWidth(224)
        from ui_transitions import WindowFade
        self._window_fade=WindowFade(self)

    def show_hint(self):
        from reference_widgets import palette
        c=palette()
        self.card.setStyleSheet(f"QFrame{{background:{c['surface']};border:1px solid {c['border']};border-radius:12px;}} QLabel{{border:0;color:{c['secondary']};font-size:12px;}} QPushButton{{background:{c['blue']};color:white;border:0;border-radius:7px;padding:5px 12px;font-size:12px;}}")
        self.adjustSize();edge=self.manager.edge.geometry();area=self.manager.edge.screen_area();side=self.manager.settings.dock_edge
        if side=='right':point=QPoint(edge.left()-self.width()-8,edge.center().y()-self.height()//2)
        elif side=='left':point=QPoint(edge.right()+8,edge.center().y()-self.height()//2)
        elif side=='top':point=QPoint(edge.center().x()-self.width()//2,edge.bottom()+8)
        else:point=QPoint(edge.center().x()-self.width()//2,edge.top()-self.height()-8)
        self.move(max(area.left()+4,min(point.x(),area.right()-self.width()-4)),max(area.top()+4,min(point.y(),area.bottom()-self.height()-4)))
        self.show();raise_edge_target(self,above=getattr(self.manager,'dock_preview',None))

    def contains_pointer(self,point):
        # Include the short bridge so moving from the strip to the button
        # cannot dismiss the affordance halfway through the pointer's journey.
        return self.isVisible() and self.geometry().united(self.manager.edge.geometry()).adjusted(-6,-6,6,6).contains(point)
