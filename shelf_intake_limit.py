"""Shelf-only byte limit. Remote/LAN transfers and local originals are untouched."""
from pathlib import Path
import os
MAX_SHELF_BYTES=1024**3


def classify(paths,cancel):
    allowed=[];blocked=[]
    for ref in paths:
        if cancel.is_set():raise InterruptedError()
        if str(ref).startswith('flickdock:text:'):allowed.append(ref);continue
        path=Path(ref);size=0
        if path.is_dir():
            def fail(error):raise error
            for folder,dirs,files in os.walk(path,followlinks=False,onerror=fail):
                dirs[:]=[name for name in dirs if not Path(folder,name).is_symlink() and not Path(folder,name).is_junction()]
                for name in files:
                    if cancel.is_set():raise InterruptedError()
                    child=Path(folder,name)
                    if child.is_symlink() or child.is_junction():continue
                    size+=child.stat().st_size
                    if size>MAX_SHELF_BYTES:break
                if size>MAX_SHELF_BYTES:break
        else:
            try:size=path.stat().st_size
            except OSError:pass
        (blocked if size>MAX_SHELF_BYTES else allowed).append(ref)
    return allowed,blocked


def make_notice(manager,*,recommendation=False):
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
            self.label=QLabel('较大文件，建议使用远程传输。' if recommendation else '超过 1 GB，无法放入搁架，请使用远程传输。',self.card);self.label.setWordWrap(True);row.addWidget(self.label,1)
            self.action=QPushButton('使用远程传输' if recommendation else '远程传输',self.card);self.action.setAutoDefault(False);row.addWidget(self.action)
            self.action.clicked.connect(self.open_remote)
            self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self.hide);self.setFixedWidth(380)
        def open_remote(self):
            paths=list(self.paths);self.hide()
            if recommendation:manager._remote_intake(paths)
            else:manager.show_wan_dialog();manager.wan_dialog.panel.queue_paths(paths)
        def show_paths(self,paths):
            from native_ui_common import colors
            c=colors(manager.settings.theme);self.paths=list(dict.fromkeys(paths))
            self.card.setStyleSheet(f"QFrame{{background:{c['surface']};border:1px solid {c['border']};border-radius:8px;}} QLabel{{color:{c['text']};font-size:12px;border:0;}} QPushButton{{background:{c['blue']};color:white;border:0;border-radius:8px;padding:7px;}}")
            self.adjustSize();anchor=manager.shelf.visual_geometry();screen=QGuiApplication.screenAt(anchor.center()) or QGuiApplication.primaryScreen();area=screen.availableGeometry()
            self.move(max(area.left()+8,min(anchor.right()-self.width(),area.right()-self.width()-8)),max(area.top()+8,min(anchor.bottom()+8,area.bottom()-self.height()-8)))
            self.show();self.raise_();self.timer.start(8000)
    return Notice()
