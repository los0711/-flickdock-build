"""Existing nonmodal recommendation for large shelf files."""
def make_notice(manager):
    from PySide6.QtCore import Qt,QTimer
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QWidget,QFrame,QVBoxLayout,QHBoxLayout,QLabel,QPushButton
    from platform_native import keep_tool_visible_when_inactive
    class Notice(QWidget):
        def __init__(self):
            super().__init__();self.paths=[]
            self.setWindowFlags(Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint|Qt.WindowDoesNotAcceptFocus)
            self.setAttribute(Qt.WA_ShowWithoutActivating);self.setAttribute(Qt.WA_TranslucentBackground)
            keep_tool_visible_when_inactive(self)
            root=QVBoxLayout(self);root.setContentsMargins(0,0,0,0);self.card=QFrame(self);root.addWidget(self.card)
            row=QHBoxLayout(self.card);row.setContentsMargins(14,12,14,12);row.setSpacing(12)
            self.label=QLabel('较大文件，建议使用远程传输。',self.card);self.label.setWordWrap(True);row.addWidget(self.label,1)
            self.action=QPushButton('使用远程传输',self.card);self.action.setAutoDefault(False);row.addWidget(self.action)
            self.action.clicked.connect(self.open_remote)
            self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self.hide);self.setFixedWidth(380)
        def open_remote(self):
            paths=list(self.paths);self.hide()
            manager._remote_intake(paths)
        def show_paths(self,paths):
            from native_ui_common import colors
            c=colors(manager.settings.theme);self.paths=list(dict.fromkeys(paths))
            self.card.setStyleSheet(f"QFrame{{background:{c['surface']};border:1px solid {c['border']};border-radius:8px;}} QLabel{{color:{c['text']};font-size:12px;border:0;}} QPushButton{{background:{c['blue']};color:white;border:0;border-radius:8px;padding:7px;}}")
            self.adjustSize();anchor=manager.shelf.visual_geometry();screen=QGuiApplication.screenAt(anchor.center()) or QGuiApplication.primaryScreen();area=screen.availableGeometry()
            self.move(max(area.left()+8,min(anchor.right()-self.width(),area.right()-self.width()-8)),max(area.top()+8,min(anchor.bottom()+8,area.bottom()-self.height()-8)))
            self.show();self.raise_();self.timer.start(8000)
    return Notice()
