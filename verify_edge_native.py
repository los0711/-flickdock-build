"""Operator-driven real native drag acceptance, using only disposable fixtures.

Run on Windows/Cocoa with --output <disposable-dir>. Drag fixture files from
Explorer/Finder to a configured screen edge. It never injects input, fabricates
QDragEnter events, uses accounts, reads the system clipboard, or changes startup.
The JSONL log identifies native callback types and states, not file contents.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import sys
import threading
import time

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QCursor, QGuiApplication, QPainter, QColor
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from edge_absorb import EdgeAbsorb
from edge_file_drag import EdgeFileDrag
from edge_magnetic_model import MagneticConfig
from platform_native import mouse_buttons_mask, window_move_active


class NativeFixture(QWidget):
    def __init__(self, output):
        super().__init__()
        self.setWindowTitle('FlickDock Magnetic Native QA')
        self.setObjectName('FlickDockMagneticNativeQA')
        self.output = output
        self.fixtures = output/'fixtures'
        self.received = output/'received'
        self.fixtures.mkdir(parents=True,exist_ok=True)
        self.received.mkdir(exist_ok=True)
        for name in ('magnetic-test-a.txt','magnetic-test-b.txt','magnetic-test-c.txt'):
            (self.fixtures/name).write_bytes(b'FlickDock disposable native drop fixture\n')
        self.events = queue.Queue()
        self.writer = threading.Thread(target=self._write_events,name='edge-native-test-log',daemon=True)
        self.writer.start()
        self.payloads = []
        self.label=QLabel(self);self.label.setWordWrap(True)
        box=QVBoxLayout(self);box.setContentsMargins(30,30,30,30);box.addWidget(self.label)
        self.setStyleSheet('QWidget{background:#1b2533;color:#e2e8f0}QLabel{font-size:15px}')
        self.visual = EdgeAbsorb(seed=20)
        from magnetic_settings import defaults
        self.visual.configure_regions(defaults())
        self.gate = EdgeFileDrag(self.visual,self.receive,note=self.note)
        self._native_observer()
        self.timer=QTimer(self);self.timer.setTimerType(Qt.PreciseTimer);self.timer.setInterval(20)
        self.timer.timeout.connect(self.poll);self.timer.start()
        self.status=QTimer(self);self.status.setInterval(200);self.status.timeout.connect(self.refresh);self.status.start()
        screen=QGuiApplication.primaryScreen();area=screen.availableGeometry()
        self.setGeometry(area.right()-710,area.top()+100,680,min(620,area.height()-140))
        self.refresh()
        self.note('qa.started',backend=QApplication.platformName(),screens=[{'geometry':s.geometry().getRect(),'dpr':s.devicePixelRatio(),'refresh_hz':s.refreshRate()} for s in QGuiApplication.screens()])
        QTimer.singleShot(10,self,self.write_ready)

    def _native_observer(self):
        original=self.gate._native_move
        def observed(event,position,*,entering=False):
            original(event,position,entering=entering)
            if entering:
                self.note('qt.native_drag_enter_observed',spontaneous=event.spontaneous(),accepted=event.isAccepted(),local_file_urls=event.mimeData().hasUrls())
        self.gate._native_move=observed
        leave=self.gate._native_leave
        def left():
            self.note('qt.native_drag_leave_observed');leave()
        self.gate._native_leave=left
        drop=self.gate._native_drop
        def dropped(event,position):
            drop(event,position)
            self.note('qt.native_drop_observed',spontaneous=event.spontaneous(),accepted=event.isAccepted())
        self.gate._native_drop=dropped

    def _write_events(self):
        with (self.output/'native-events.jsonl').open('w',encoding='utf-8') as file:
            while True:
                row=self.events.get()
                if row is None:return
                file.write(json.dumps(row,ensure_ascii=False)+'\n');file.flush()

    def note(self,name,**fields):
        self.events.put({'time':time.perf_counter(),'event':name,**fields})

    def poll(self):
        self.gate.update_pointer(QCursor.pos(),bool(mouse_buttons_mask()&1),eligible=not window_move_active())

    def refresh(self):
        self.label.setText('Native file drag verification\n\n'
            'Drag magnetic-test-a.txt toward the middle half of the LEFT or RIGHT screen edge.\n'
            'This window uses no account, cloud, clipboard, global hotkey or installed FlickDock.\n\n'
            f'Native backend: {QApplication.platformName()}\n'
            f'File destination armed: {self.gate.armed}\n'
            f'Native file MIME validated: {self.gate.validated}\n'
            f'Field state: {self.visual.state}\n'
            f'Visible particles: {len(self.visual.field.particles)}\n'
            f'Accepted fixture drops: {len(self.payloads)}\n\n'
            f'Fixture folder:\n{self.fixtures}\n\n'
            'After a drop the original fixture must still exist.\n'
            'Closing this QA window stops every test window and animation.')

    def receive(self,mime,position):
        paths=[Path(url.toLocalFile()).resolve() for url in mime.urls()]
        if not paths or any(path.parent!=self.fixtures.resolve() for path in paths):
            self.note('qa.reject_non_fixture')
            return False
        for path in paths:
            destination=self.received/path.name
            shutil.copyfile(path,destination)
            source_hash=hashlib.sha256(path.read_bytes()).hexdigest()
            self.payloads.append({'sha256':source_hash,'matches':source_hash==hashlib.sha256(destination.read_bytes()).hexdigest(),'source_preserved':path.exists()})
        self.note('qa.fixture_verified',count=len(paths),matches=all(row['matches'] for row in self.payloads),source_preserved=all(row['source_preserved'] for row in self.payloads))
        return True

    def write_ready(self):
        report={'backend':QApplication.platformName(),'fixture_directory':str(self.fixtures),'window_id':int(self.winId()),
                'screen_geometries':[s.geometry().getRect() for s in QGuiApplication.screens()]}
        if sys.platform=='win32':
            import ctypes
            from ctypes import wintypes
            user=ctypes.WinDLL('user32',use_last_error=True)
            user.GetWindowLongPtrW.argtypes=[wintypes.HWND,ctypes.c_int];user.GetWindowLongPtrW.restype=ctypes.c_ssize_t
            self.visual.overlay.winId()
            style=user.GetWindowLongPtrW(int(self.visual.overlay.winId()),-20)
            report['native_overlay_exstyle']=style
            report['native_overlay_ws_ex_transparent']=bool(style&0x20)
        self.note('qa.ready',**report)

    def closeEvent(self,event):
        self.timer.stop();self.status.stop();self.gate.close();self.visual.close()
        self.note('qa.closed',accepted=len(self.payloads),payloads=self.payloads)
        self.events.put(None);self.writer.join(timeout=2)
        event.accept();QApplication.instance().quit()


def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--output','--verify-output',dest='output',type=Path,required=True)
    parser.add_argument('--verify-edge-native',action='store_true')
    parser.add_argument('--duration',type=float,default=0,help='Optional native-window smoke duration; does not simulate a file drag')
    args=parser.parse_args(argv);args.output.mkdir(parents=True,exist_ok=True)
    app=QApplication([])
    from shelf_ui_common import load_font
    load_font(app)
    if app.platformName() not in ('windows','cocoa'):
        raise SystemExit('Native verification requires Windows or Cocoa; offscreen is not accepted.')
    app.setQuitOnLastWindowClosed(False)
    fixture=NativeFixture(args.output);fixture.show()
    # A hidden-console launcher can suppress the first native ShowWindow.
    # Re-show only this disposable QA surface, without activating another app.
    def reveal():
        fixture.hide();fixture.show()
    QTimer.singleShot(200,fixture,reveal)
    if args.duration>0:
        QTimer.singleShot(round(min(30,args.duration)*1000),fixture,fixture.close)
    sys.exit(app.exec())


if __name__=='__main__':main()
