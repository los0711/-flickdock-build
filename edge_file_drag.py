"""A native Qt/OLE/Cocoa file drop destination, separate from the glow canvas.

Pointer polling only makes the edge destination reachable. It is never proof
of a file drag: only dragEnter/dragMove with local file URLs authorize light.
The entire configured approach band is an explicit file drop destination;
outside it the input window has no geometry and the drawing window is native
input-transparent. No general-purpose desktop-wide interception is installed.
"""
from __future__ import annotations

import weakref
import sys
from math import floor

from dataclasses import replace

from PySide6.QtCore import QMimeData, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QPainter,QRegion
from PySide6.QtWidgets import QApplication, QWidget

from edge_absorb import input_band
from platform_native import keep_tool_visible_when_inactive, raise_edge_target, remove_host_window_shadow
from qt_lifetime import later


def file_urls(mime):
    if not mime.hasUrls():
        return []
    # Do not open/stat files, read the clipboard, materialize a promised file,
    # decode a thumbnail, or query any cloud service during DragOver.
    return [url for url in mime.urls() if url.isLocalFile() and url.toLocalFile()]


def drop_position(widget,event):
    local=event.position()
    if sys.platform=='darwin':
        from dock_geometry import cocoa_screen_pointer
        return cocoa_screen_pointer(widget.mapToGlobal(QPoint(floor(local.x()),floor(local.y()))))
    return widget.mapToGlobal(local.toPoint())


class _FileDestination(QWidget):
    def __init__(self, owner):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowDoesNotAcceptFocus)
        self.owner = weakref.ref(owner)
        self.setObjectName('flickdockMagneticFileDestination')
        self.setWindowTitle('FlickDock file drop destination')
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAcceptDrops(True)
        keep_tool_visible_when_inactive(self)

    def paintEvent(self, event):
        # Alpha=1 is necessary for Windows layered-window OLE hit testing.
        # This window never draws the glow and never repaints per frame.
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 1))
        painter.end()

    def dragEnterEvent(self, event):
        owner = self.owner()
        if owner is not None:
            owner._native_move(event,drop_position(self,event),entering=True)
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        owner = self.owner()
        if owner is not None:
            owner._native_move(event,drop_position(self,event))
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        owner = self.owner()
        if owner is not None:
            owner._native_leave()
        event.accept()

    def dropEvent(self, event):
        owner = self.owner()
        if owner is not None:
            owner._native_drop(event,drop_position(self,event))
        else:
            event.ignore()


class EdgeFileDrag(QObject):
    def __init__(self, visual, on_drop, *, allowed=None, note=None, pointer=None,
                 movement_threshold=None,on_file_move=None,preview_regions=None,on_preview_drop=None):
        super().__init__()
        self.visual, self.on_drop = visual, on_drop
        self.allowed = allowed or (lambda: True)
        self.note = note or (lambda name: None)
        self.pointer = pointer or QCursor.pos
        self.on_file_move=on_file_move
        self.preview_regions=preview_regions or (lambda:())
        self.on_preview_drop=on_preview_drop
        self.coexisting_preview=on_file_move is not None
        self._input_masks={};self._covered_by_preview=False
        self.closed = False
        self._windows = {}
        self._prepared_space = None
        self._target = None
        self._anchor = None
        self._down = False
        self._moved = False
        self._seen_file = False
        self._cancelled_target = None
        self._release_pending = False
        self._epoch = 0
        self._last_native_position = None
        self._previous_pointer = None
        self._drop_committed = False
        self._drop_inflight = False
        self.movement_threshold = max(1, movement_threshold if movement_threshold is not None
                                      else QApplication.styleHints().startDragDistance())
        self._release_timer = QTimer(self)
        self._release_timer.setSingleShot(True)
        self._release_timer.timeout.connect(self._finish_release)
        self.visual.position_provider = self.validated_position
        self._app = QApplication.instance()
        self._app.aboutToQuit.connect(self.close)
        self._app.screenRemoved.connect(self.cancel)

    @property
    def armed(self):
        return self.prepared and (self._target is None or self._cancelled_target != self._target.signature())

    @property
    def prepared(self):
        """Native targets may stay ready after Leave; MIME proof is revoked."""
        return not self.closed and (self._covered_by_preview or any(window.isVisible() for window in self._windows.values()))

    @property
    def window(self):
        # Compatibility / QA access to the current edge. Polling properties
        # never call this, so an idle controller creates no native input HWND.
        edge=self._target.edge if self._target is not None else 'right'
        return self._window(edge)

    def _window(self,edge):
        if edge not in self._windows:self._windows[edge]=_FileDestination(self)
        return self._windows[edge]

    def _prepare(self,target):
        space=(target.area.getRect(),id(target.screen))
        if self._prepared_space!=space:
            for window in self._windows.values():window.hide()
            self._prepared_space=space
        regions=self.visual.input_regions(target)
        protected=tuple(self.preview_regions());self._covered_by_preview=False
        for edge,window in self._windows.items():
            if edge not in regions:window.hide()
        for edge,rect in regions.items():
            window=self._window(edge)
            if sys.platform=='darwin':
                from dock_geometry import cocoa_drop_window_rect
                rect=cocoa_drop_window_rect(rect,target.area,edge)
            if window.geometry()!=rect:window.setGeometry(rect)
            mask=QRegion(window.rect())
            for preview in protected:mask=mask.subtracted(QRegion(preview.intersected(rect).translated(-rect.topLeft())))
            if mask.isEmpty():
                self._covered_by_preview=True;window.hide();continue
            if self._input_masks.get(edge)!=mask:
                window.setMask(mask);self._input_masks[edge]=mask
            if not window.isVisible():
                window.show();remove_host_window_shadow(window)
                window.repaint();raise_edge_target(window)

    @property
    def validated(self):
        return self.armed and self._seen_file

    def update_pointer(self, position, down, *, eligible=True):
        if self.closed:
            return
        position, down = QPoint(position), bool(down)
        previous,self._previous_pointer = self._previous_pointer,QPoint(position)
        new_press = down and not self._down
        self._down = down
        if new_press:
            self._epoch += 1
            self._release_timer.stop()
            self._release_pending = False
            self._seen_file = False
            self._cancelled_target = None
            self._drop_committed = False
            # A fast drag can travel from the idle sample into the edge in one
            # polling interval. Do not reset away that first movement sample.
            self._anchor, self._moved = QPoint(previous if previous is not None else position), False
        if not eligible or not self.allowed():
            self._stop_destination()
            return
        if not down:
            self._anchor = None
            self._moved = False
            if self.prepared and not self._release_pending:
                # Qt documents that a native mouse-up poll may precede the
                # queued OLE Drop. Keep this real destination for a short,
                # bounded grace; no MIME pointer is retained during it.
                self._release_pending = True
                self._release_timer.start(self.visual.config.release_grace_ms)
                if self.visual.state != 'dropping':
                    self.visual.retreat()
            return
        if self._drop_committed:
            return  # An accepted Drop can never re-arm during this held press.
        if self._anchor is None:
            self._anchor = QPoint(position)
        if (position - self._anchor).manhattanLength() >= self.movement_threshold:
            self._moved = True
        if not self._moved:
            return  # A stationary long press cannot even arm the destination.
        target = self.visual.target_at(position,distance=self.visual.config.prearm_distance_px)
        if target is None:
            self._stop_destination()
            return
        if self._cancelled_target == target.signature():
            return
        if self._target is None or self._target.signature() != target.signature():
            self._seen_file = False
            self.visual.retreat()
            self._target = target
        # Keep all perimeter rectangles stationary. In particular, turning a
        # corner must not move an OLE target out from under a queued Drop.
        self._prepare(target)

    def validated_position(self):
        if not self.validated or self._release_pending or not self.allowed():
            return None
        position = QPoint(self.pointer())
        target = self.visual.target_at(position)
        if target is None:
            return None
        return position

    def _can_receive(self, event, position):
        if self.closed or not self.prepared or self._drop_committed or self._drop_inflight or not self.allowed():
            return False
        target = self.visual.target_at(position)
        if target is None or not any(window.isVisible() and window.geometry().contains(position)
                                     for window in self._windows.values()):
            return False
        # Accepting a proposed MoveAction can delete a source file. This
        # destination only advertises native Copy, never changes source art.
        return bool(event.possibleActions() & Qt.CopyAction) and bool(file_urls(event.mimeData()))

    def _native_move(self, event, position, *, entering=False):
        if not self._can_receive(event, position):
            event.ignore()
            if entering:
                self._cancelled_target = self._target.signature() if self._target else None
                self._defer_stop()
            return
        event.setDropAction(Qt.CopyAction)
        event.accept()
        self._observe_file(event,position,entering=entering)

    def observe_native_move(self,event,position,*,entering=False):
        """A real shelf drag event may sustain light through the preview's hole."""
        if (not self.coexisting_preview or self.closed or self._drop_committed or self._drop_inflight
                or not self.allowed() or not event.possibleActions()&Qt.CopyAction or not file_urls(event.mimeData())):return
        if self.visual.target_at(position) is None:return
        self._observe_file(event,position,entering=entering)

    def _observe_file(self,event,position,*,entering=False):
        first = not self._seen_file
        if entering or first:
            self._epoch += 1  # A re-enter invalidates the previous leave's queued hide.
        self._cancelled_target = None
        self._target = self.visual.target_at(position)
        self._last_native_position = QPoint(position)
        self._seen_file = True
        if first or entering:self.visual.prefetch_icons(file_urls(event.mimeData()))
        self.visual.follow(position)
        if self.on_file_move is not None:
            self.on_file_move(QPoint(position))
            if not self.closed:self._prepare(self._target)
        if first:
            self.note('edge.magnetic.native_file_enter')

    def _native_leave(self):
        if self.closed:
            return
        was_file = self._seen_file
        self._seen_file = False
        if self.visual.state != 'dropping':
            self.visual.retreat()
        if was_file:
            self.note('edge.magnetic.native_leave')
            # Leave revokes the proof (also on Escape), but is not proof that
            # the entire source drag ended: OLE/AppKit can leave/re-enter while
            # targets are reordered. Keep the fixed native targets prepared;
            # only a NEW native MIME event may re-arm the visual in this press.
            if self._target is not None and any(window.isVisible() and window.geometry().contains(self.pointer())
                                                for window in self._windows.values()):
                self._cancelled_target = self._target.signature()

    def drop_on_preview(self,event,position):
        if not self.coexisting_preview or not file_urls(event.mimeData()):return None
        if not any(rect.contains(position) for rect in self.preview_regions()):return None
        self._native_drop(event,position,preview=True)
        return event.isAccepted()

    def _native_drop(self, event, position,*,preview=False):
        preview=preview or bool(self.on_preview_drop and any(rect.contains(position) for rect in self.preview_regions()))
        valid_preview=(preview and self.on_preview_drop and not self.closed and not self._drop_committed
                       and not self._drop_inflight and self.allowed() and bool(event.possibleActions()&Qt.CopyAction)
                       and bool(file_urls(event.mimeData())))
        if not (valid_preview if preview else self._can_receive(event, position)):
            event.ignore()
            self._defer_stop()
            return
        # Copy just local URLs while the event owns its QMimeData. Never keep
        # an event wrapper, touch its image data, or trust an old probe result.
        owned = QMimeData()
        owned.setUrls(file_urls(event.mimeData()))
        urls=owned.urls()
        handled = False
        self._drop_inflight = True
        try:
            receiver=self.on_preview_drop if preview else self.on_drop
            handled = bool(receiver(owned, QPoint(position)))
        finally:
            self._drop_inflight = False
            owned.deleteLater()
            self._seen_file = False
            self._release_timer.stop()
            self._release_pending = False
            if not handled:
                event.ignore()
                self.visual.retreat()
                self._defer_stop()
        if self.closed:
            if handled:event.setDropAction(Qt.CopyAction);event.accept()
            else:event.ignore()
            return
        if handled:
            self._drop_committed = True
            event.setDropAction(Qt.CopyAction)
            event.accept()
            # Native Drop has been accepted. Schedule the purely visual copy
            # outside OLE/AppKit; no source file or event-owned MIME survives.
            if preview:
                self.visual.retreat();self.note('edge.magnetic.preview_drop_accepted')
            else:
                self.visual.field.drop()
                epoch=self._epoch;release_position=QPoint(position)
                later(0,self,lambda owner:owner._show_accepted_drop(release_position,urls,epoch))
                self.note('edge.magnetic.drop_accepted')
        else:
            event.ignore()
            self.visual.retreat()
            self.note('edge.magnetic.drop_rejected')
        # Finish native Drop before hiding its registered destination. This
        # avoids re-entering OLE/AppKit in the middle of their callback.
        self._defer_stop(preserve_drop=handled and not preview)

    def _show_accepted_drop(self,position,urls,epoch):
        if not self.closed and not self.visual.closed and epoch==self._epoch and self._drop_committed:
            self.visual.release(position,urls=urls)

    def _defer_stop(self, *, preserve_drop=False):
        epoch = self._epoch
        def finish(owner):
            if not owner.closed and epoch == owner._epoch:
                owner._stop_destination(preserve_drop=preserve_drop)
        later(0, self, finish)

    def _finish_release(self):
        self._stop_destination(preserve_drop=self.visual.state == 'dropping')

    def _stop_destination(self, *, preserve_drop=False):
        self._release_timer.stop()
        self._covered_by_preview=False
        self._release_pending = self._seen_file = False
        for window in self._windows.values():window.hide()
        if not preserve_drop and self.visual.state != 'dropping':
            self.visual.retreat()

    def cancel(self, *_):
        if self.closed:
            return
        self._epoch += 1
        self._down = self._moved = False
        self._anchor = self._cancelled_target = self._target = None
        self._previous_pointer = None
        self._drop_committed = False
        self._stop_destination()

    def close(self):
        if self.closed:
            return
        self._epoch += 1
        self.closed = True
        app, self._app = self._app, None
        if app is not None:
            app.aboutToQuit.disconnect(self.close)
            app.screenRemoved.disconnect(self.cancel)
        self._release_timer.stop()
        self._seen_file = False
        self.visual.position_provider = None
        for window in self._windows.values():
            window.hide();window.deleteLater()
        self._windows.clear()
        self.on_drop = self.allowed = self.note = self.pointer = None
