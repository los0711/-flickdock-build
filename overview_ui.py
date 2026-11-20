"""Overview navigation, file rows, selection and incremental layout."""
from __future__ import annotations
import os
import sys
import time
from collections import Counter

from PySide6.QtCore import QEvent
from PySide6.QtCore import QObject
from PySide6.QtCore import QPoint
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtCore import QTimer
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap, QColor
from PySide6.QtGui import QPainter
from native_ui_common import font_css_stack
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QFrame
from PySide6.QtWidgets import QGraphicsEffect
from PySide6.QtWidgets import QGridLayout
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QSizePolicy
from PySide6.QtWidgets import QSpacerItem
from PySide6.QtWidgets import QStyle
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from content_grid import GRID_CARD_HEIGHT_PX
from content_grid import GRID_CARD_MIN_WIDTH_PX
from content_grid import GRID_COLUMN_WIDTH_PX
from content_grid import IconItem
from drop_routing import OverviewDropRouter
from file_preview import ElidedLabel
from file_preview import MetadataLabel
from file_preview import PreviewPane
from flickdock_icons import glyph_pixmap
from flickdock_motion import Spring
from pathlib import Path
from reference_widgets import ToolbarButton
from shelf_ui_common import AnimatedMenu
from shelf_ui_common import FILE_DETAIL_FONT_SIZE_PX
from shelf_ui_common import FILE_ROW_HEIGHT_PX
from shelf_ui_common import OVERVIEW_FULL_TOOLBAR_MIN_WIDTH_PX
from shelf_ui_common import OVERVIEW_TRANSITION_MAX_ITEMS
from shelf_ui_common import style_menu
from transfer_visuals import CLOUD_STATUS_LABELS
from transfer_visuals import cloud_status_for_ref
from transfer_visuals import file_presence_label
from transfer_visuals import is_local_upload_transfer
from transfer_visuals import transfer_fraction
from transfer_visuals import transfer_phase_label
from widget_retirement import WidgetRetirementQueue
import math
import shelf_ui_common
import sys
import time
import ui_theme

VIRTUAL_FILE_THRESHOLD=64

FILE_ROW_LEFT_INSET_PX = 9
FILE_ROW_ITEM_GAP_PX = 8


class _SidebarCollapse(QObject):
    RAIL_WIDTH=64

    def __init__(self,view):
        super().__init__(view)
        self.view=view
        self.open=True
        self.expanded_width=210
        self._toggle_alignment=None
        self.rows=(*view.scope_buttons.values(),view.translator_nav,view.video_nav,
                   view.lan_nav,view.settings_nav,view.account_nav)
        self.labels=Spring(self,dict.fromkeys(range(len(self.rows)),1.),self._paint_labels)
        self.width=Spring(self,{'width':210.},self._resize)
        self.arrow=Spring(self,{'angle':0.},self._rotate)
        self.delays=[]
        for i,row in enumerate(self.rows):
            row.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
            row.setMinimumWidth(0)
            timer=QTimer(self);timer.setSingleShot(True)
            timer.timeout.connect(lambda i=i:self.labels.animate_physical({i:1.},300,26))
            self.delays.append(timer)
            if row.text():row.setAccessibleName(row.text())
        self.width.settled.connect(self._finish)
        view.sidebar_toggle.clicked.connect(self.toggle)
        view.sidebar.configure_vertical_spring(380,28)
        self._announce()

    def _announce(self):
        control=self.view.sidebar_toggle
        title='收起侧栏' if self.open else '展开侧栏'
        control.setChecked(not self.open)
        control.setAccessibleName(title);control.setToolTip(title)

    def update_size(self,width,visible):
        changed=width!=self.expanded_width
        self.expanded_width=width
        if not visible:self.finish_immediately()
        elif self.width._timer.isActive():
            if changed and self.open:self.width.animate_physical({'width':float(width)},260,24)
        else:
            self.width.jump({'width':float(width if self.open else self.RAIL_WIDTH)})

    def toggle(self):
        from ui_transitions import motion_allowed
        self.open=not self.open
        self._announce()
        for row in self.rows:
            if row.text():row.setToolTip('' if self.open else row.text())
        if not self.view.shelf.manager.motion or not motion_allowed():
            self.finish_immediately()
            self._finish()
            return
        for timer in self.delays:timer.stop()
        if self.open:
            for i,timer in enumerate(self.delays):timer.start(60+40*i)
        else:self.labels.animate_physical(dict.fromkeys(range(len(self.rows)),0.),300,26)
        self.arrow.animate_physical({'angle':0. if self.open else 180.},300,22)
        self.width.animate_physical({'width':float(self.expanded_width if self.open else self.RAIL_WIDTH)},260,24)

    def finish_immediately(self):
        for timer in self.delays:timer.stop()
        self.width.jump({'width':float(self.expanded_width if self.open else self.RAIL_WIDTH)})
        self.labels.jump(dict.fromkeys(range(len(self.rows)),float(self.open)))
        self.arrow.jump({'angle':0. if self.open else 180.})

    def _finish(self):
        if self.view.isVisible():self.view._responsive_timer.start(0)

    def _resize(self,values):
        rail=self.view.sidebar
        width=max(self.RAIL_WIDTH,round(values['width']))
        if rail.width()!=width or rail.maximumWidth()!=width:rail.setFixedWidth(width)
        alignment=Qt.AlignHCenter if width<=72 else Qt.AlignRight
        if alignment!=self._toggle_alignment:
            self._toggle_alignment=alignment
            rail.layout().setAlignment(self.view.sidebar_toggle,alignment)
        self._paint_labels(self.labels.values)

    def _paint_labels(self,values):
        width=self.view.sidebar.width()
        compact=max(0.,min(1.,(108-width)/(108-self.RAIL_WIDTH)))
        for i,row in enumerate(self.rows):
            row._sidebar_label_alpha=max(0.,min(1.,values[i]))
            row._sidebar_compact_progress=compact
            row.update()
        self.view._position_nav_counts()

    def _rotate(self,values):
        self.view.sidebar_toggle.angle=values['angle']
        self.view.sidebar_toggle.update()


class _RowPressEffect(QGraphicsEffect):
    """Scale only the painted row; layout and pointer bounds stay fixed."""
    def __init__(self, row):
        super().__init__(row);self.row=row

    def draw(self, painter):
        scale=1-.025*max(0.,min(1.,self.row.motion.values['down']))
        bounds=self.sourceBoundingRect(Qt.LogicalCoordinates)
        center=bounds.center()
        offset=QPoint()
        source=self.sourcePixmap(Qt.LogicalCoordinates,offset,QGraphicsEffect.PixmapPadMode.NoPad)
        painter.save();painter.translate(center);painter.scale(scale,scale);painter.translate(-center)
        painter.drawPixmap(QPointF(offset),source);painter.restore()


_SCROLL_PROFILE = os.environ.get('FLICKDOCK_PROFILE_SCROLL') == '1'
_SCROLL_SAMPLES = []


def _record_row_paint(seconds):
    _SCROLL_SAMPLES.append(seconds * 1000)
    if len(_SCROLL_SAMPLES) % 120:
        return
    ordered = sorted(_SCROLL_SAMPLES)
    print('scroll-paint rows=%d p50=%.2fms p95=%.2fms' % (
        len(ordered), ordered[len(ordered) // 2], ordered[int(len(ordered) * .95)]),
        file=sys.stderr, flush=True)
    _SCROLL_SAMPLES.clear()


class FileRow(QFrame):
    def __init__(self,popup,path):
        super().__init__(popup.rows_body)
        from reference_widgets import ThumbnailLabel,label,button,item_detail
        from reference_widgets import SelectionCheck
        self.popup,self.path=popup,path;self.setAcceptDrops(True);self.origin=QPoint();self.did_drag=False
        self.setFixedHeight(FILE_ROW_HEIGHT_PX);self.setMouseTracking(True);self.setCursor(Qt.OpenHandCursor);self.setFocusPolicy(Qt.StrongFocus)
        self._keyboard_focus=False
        self._highlighted=False
        self.motion=Spring(self,{'highlight':0,'down':0},self._hover_frame,motion=popup.shelf.manager.motion)
        self.motion.settled.connect(self._finish_press)
        row=QHBoxLayout(self);row.setContentsMargins(FILE_ROW_LEFT_INSET_PX,8,8,8);row.setSpacing(FILE_ROW_ITEM_GAP_PX)
        self.selection_check=SelectionCheck(self);self.selection_check.setFixedSize(40,40);self.selection_check.setChecked(path in popup.selected)
        self.selection_check.setAccessibleName('选择 '+popup.shelf.content.title(path))
        self.selection_check.clicked.connect(lambda _:popup.select_item(self.path,toggle=True))
        from reference_widgets import css
        row.addWidget(self.selection_check)
        self.icon_label=ThumbnailLabel(popup.shelf,path,self);self.icon_label.setProperty('suppressPinBadge',True);self.icon_label.setAttribute(Qt.WA_TransparentForMouseEvents);row.addWidget(self.icon_label)
        names=QVBoxLayout();names.setSpacing(0)
        self.name_label=ElidedLabel(self);self.name_label.setText(popup.shelf.content.title(path));self.name_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        css(self.name_label,"color:#F5F5F7;background:transparent;border:0;font:600 12px 'FlickDock UI';","color:#1D1D1F;background:transparent;border:0;font:600 12px 'FlickDock UI';")
        self.detail_label=MetadataLabel(self);self.detail_label.setFixedHeight(17)
        from native_ui_common import colors
        css(self.detail_label,*[f"color:{colors(t)['file_description']};background:transparent;border:0;font:9px {font_css_stack()};" for t in ('dark','light')])
        self.detail_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        title=QHBoxLayout();title.setContentsMargins(0,0,0,0);title.setSpacing(4)
        self.pin_button=ToolbarButton('star',self,motion=popup.shelf.manager.motion);self.pin_button.setFixedSize(20,20);self.pin_button.setProperty('glyphSize',14)
        self.pin_button.setProperty('shelfPin',True)
        self.pin_button.clicked.connect(lambda:self.popup.toggle_pin(self.path));title.addWidget(self.pin_button);title.addWidget(self.name_label,1)
        names.addLayout(title);names.addWidget(self.detail_label);row.addLayout(names,1)
        self.remove_button=ToolbarButton('close',self,motion=popup.shelf.manager.motion);self.remove_button.setFixedSize(32,32);self.remove_button.setToolTip('从搁架移除');self.remove_button.setAccessibleName('从搁架移除 '+popup.shelf.content.title(path));self.remove_button.clicked.connect(lambda:popup.remove_path(self.path))
        self.remove_button.set_glyph_tint('#FF453A')
        policy=self.remove_button.sizePolicy();policy.setRetainSizeWhenHidden(True);self.remove_button.setSizePolicy(policy)
        row.addWidget(self.remove_button);self.remove_button.hide()
        self.refresh_cloud_status();self.refresh_upload_time();self.refresh_pin()

    def refresh_pin(self):
        pinned=getattr(self.popup,'is_pinned',lambda ref:False)(self.path)
        self.pin_button.set_glyph('star-filled' if pinned else 'star')
        self.pin_button.setToolTip('取消星标置顶' if pinned else '星标置顶');self.pin_button.setAccessibleName(self.pin_button.toolTip()+' '+self.popup.shelf.content.title(self.path))
        self.icon_label.update()

    def _hover_frame(self,values):
        highlighted=values['highlight']>.3
        if highlighted!=self._highlighted:
            self._highlighted=highlighted;self.popup.shelf.surface_effect.invalidate_source();self.update()
        effect=self.graphicsEffect()
        if isinstance(effect,_RowPressEffect):effect.update()

    def _finish_press(self):
        if self.motion.values['down']==0 and isinstance(self.graphicsEffect(),_RowPressEffect):
            self.setGraphicsEffect(None)

    def refresh_content(self):
        from reference_widgets import invalidate_thumbnail,item_detail
        invalidate_thumbnail(self.popup.shelf,self.path)
        title=self.popup.shelf.content.title(self.path)
        self.name_label.setText(title)
        self.refresh_upload_time()
        self.selection_check.setAccessibleName('选择 '+title)
        self.remove_button.setAccessibleName('从搁架移除 '+title)
        self.icon_label.update();self.refresh_cloud_status();self.refresh_pin();self.update()

    def refresh_cloud_status(self):
        lookup=getattr(self.popup,'cloud_status',None)
        status=lookup(self.path) if callable(lookup) else cloud_status_for_ref(self.popup.shelf,self.path)
        self.icon_label.set_cloud_status(status)
        self.setToolTip(file_presence_label(status))
        self.setAccessibleDescription(file_presence_label(status))

    def refresh_upload_time(self):
        from reference_widgets import item_detail
        self.detail_label.set_parts(item_detail(self.popup.shelf,self.path),self.popup.upload_caption(self.path))

    def paintEvent(self,event):
        from reference_widgets import palette,ink,outside_content_view
        from native_ui_common import paint_rounded_surface
        if _SCROLL_PROFILE:
            started=time.perf_counter()
            try:return self._paint_row(event,palette,ink,outside_content_view,paint_rounded_surface)
            finally:_record_row_paint(time.perf_counter()-started)
        if outside_content_view(self):return
        self._paint_row(event,palette,ink,outside_content_view,paint_rounded_surface)

    def _paint_row(self,event,palette,ink,outside_content_view,paint_rounded_surface):
        if outside_content_view(self):return
        c=palette();chosen=self.path in self.popup.selected
        self.selection_check.blockSignals(True);self.selection_check.setChecked(chosen);self.selection_check.blockSignals(False)
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        fill=ink(self._fill_role(chosen,c))
        paint_rounded_surface(p,self.rect(),c['card_radius'],fill,ink(c['selected_border'] if chosen else c['border']))
        p.end()

    def _fill_role(self,chosen,colors):
        focused=self.hasFocus() and self._keyboard_focus
        return colors['file_card_selected_focus' if chosen and focused else 'file_card_selected' if chosen
                      else 'file_card_hover' if self.motion.values['highlight']>.3 or focused else 'file_card']

    def transition_pose(self,viewport):
        from PySide6.QtGui import QPalette
        from item_transitions import FilePose, SharedText, control_decoration
        from native_ui_common import technical_font
        from reference_widgets import ink, palette, thumbnail

        def bounds(widget):
            return QRectF(widget.mapTo(viewport,QPoint()),widget.size())
        c=palette();chosen=self.path in self.popup.selected
        art_rect=bounds(self.icon_label);badge=QRectF(art_rect.right()-18,art_rect.bottom()-18,18,18)
        detail=self.detail_label;metrics,_=detail._metrics()
        prefix=detail._description+(' · ' if detail._timestamp else '')
        meta=bounds(detail)
        prefix_width=min(meta.width(),metrics.horizontalAdvance(prefix))
        summary=QRectF(meta.left(),meta.top(),prefix_width,meta.height())
        stamp=meta.adjusted(prefix_width,0,0,0)
        title_ink=self.name_label.palette().color(QPalette.WindowText)
        detail_ink=ink(c['file_description'])
        decorations=tuple(decoration for control in (self.pin_button,self.remove_button)
                          for decoration in control_decoration(control,viewport))
        return FilePose(bounds(self),art_rect,badge,
                        thumbnail(self.popup.shelf,self.path,40,self.devicePixelRatioF()),
                        self.icon_label.cloud_status,self.popup.shelf.content.transfers.get(self.path),
                        SharedText(bounds(self.name_label),self.name_label.full_text,self.name_label.font(),title_ink),
                        SharedText(summary,prefix,detail.font(),detail_ink),
                        SharedText(stamp,detail._timestamp,technical_font(detail.font()),detail_ink),
                        ink(self._fill_role(chosen,c)),ink(c['selected_border' if chosen else 'border']),c['card_radius'],
                        self.selection_check.selection_pose(QPointF(self.selection_check.mapTo(viewport,QPoint()))),
                        decorations)

    def enterEvent(self, event):
        self.remove_button.show()
        self.popup.shelf.touch()
        self.motion.animate({"highlight": 1.0}, response=.14, damping=1)
        super().enterEvent(event)

    def leaveEvent(self, event):
        visible=(self.hasFocus() and self._keyboard_focus) or self.remove_button.hasFocus() or self.pin_button.hasFocus()
        self.remove_button.setVisible(visible)
        self.motion.animate({"highlight": 0.0}, response=.14, damping=1)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self._keyboard_focus=False
        self.popup.shelf.touch()
        if event.button() == Qt.MouseButton.LeftButton:
            self.origin = event.position().toPoint()
            self.did_drag = False
            self._pending_deselect=(self.path in self.popup.selected and not event.modifiers() &
                (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier))
            if not self._pending_deselect:
                self.popup.select_item(self.path, bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier),bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
            self.popup.rows_body.update()
            for child in self.popup.rows_body.findChildren(FileRow):
                child.update()
            if self.graphicsEffect() is None:self.setGraphicsEffect(_RowPressEffect(self))
            self.motion.animate({"highlight": 1.0,"down":1.0}, response=.12, damping=1)
            event.accept();return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and not self.did_drag and (event.position().toPoint() - self.origin).manhattanLength() > 8:
            self.did_drag = True
            self.motion.animate({'down':0},response=.18,damping=.9)
            chosen = [p for p in self.popup.shelf.files[self.popup.mode] if p in self.popup.selected]
            self.popup.shelf.drag_files(self.popup.mode, chosen or [self.path])
            QTimer.singleShot(0, self.popup.rebuild)
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button()==Qt.LeftButton:
            # Treat the second click as another ordinary selection press.
            self.mousePressEvent(event);return
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton and not self.did_drag and getattr(self,'_pending_deselect',False):
            self.popup.select_item(self.path,toggle=True)
        self._pending_deselect=False
        self.motion.animate({'down':0},response=.20,damping=.8)
        if event.button()==Qt.LeftButton:event.accept();return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self,event):
        self.popup.show_item_menu(self.path,event.globalPos());event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_C and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            refs = [ref for ref in self.popup.shelf.files[self.popup.mode] if ref in self.popup.selected]
            self.popup.shelf.copy_items(refs or [self.path])
            event.accept()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.popup.select_item(self.path,toggle=True)
            event.accept()
        elif event.key() == Qt.Key.Key_Space:
            self.popup.select_item(self.path,toggle=True);event.accept()
        elif event.key() == Qt.Key.Key_Delete:
            self.popup.remove_path(self.path)
            event.accept()
        else:
            super().keyPressEvent(event)

    def focusInEvent(self,event):
        self._keyboard_focus=event.reason() in (Qt.TabFocusReason,Qt.BacktabFocusReason,Qt.ShortcutFocusReason)
        if self._keyboard_focus:self.remove_button.show()
        self.update();super().focusInEvent(event)

    def focusOutEvent(self,event):
        self._keyboard_focus=False
        def update_actions():
            visible=self.underMouse() or self.remove_button.hasFocus() or self.pin_button.hasFocus()
            self.remove_button.setVisible(visible)
        QTimer.singleShot(0,self,update_actions);self.update();super().focusOutEvent(event)


class ContentsPopup(QFrame):
    CATEGORIES=(("all","全部"),("images","图片"),("video","视频"),("audio","音频"),("text","文字"),("files","文件"),("links","网页"),("uploading","传输中"))
    SCOPES=(("all","全部搁架"),("pinned","星标置顶"),("uploading","传输中"))
    TYPES=(("all","全部"),("files","文件"),("links","网页"),("text","文字"),("images","图片"),("video","视频"),("audio","音频"))
    def __init__(self,shelf,mode="permanent"):
        super().__init__(shelf.card)
        self.shelf,self.mode=shelf,"permanent"
        self.selected=set();self.primary_selection=None;self.category='all';self.scope='all';self.type_filter='all'
        if not hasattr(shelf,'file_pins'):
            from file_pins import FilePins
            shelf.file_pins=FilePins(shelf.manager.settings.base,shelf)
        self.pins=shelf.file_pins
        self._cloud_statuses={}
        self._upload_metadata={}
        self.sort_key=None;self.sort_direction=0
        self._item_widgets={}
        self._content_dirty=False
        self._virtual_rows=False;self._virtual_ready=False;self._visible_queue=[]
        self._positioned_rows=False
        self._pending_view_mode=None;self._view_writer=None
        self.retirement=WidgetRetirementQueue(self)
        self._build_timer=QTimer(self);self._build_timer.setSingleShot(True)
        self._build_timer.timeout.connect(self._append_batch)
        from reference_widgets import build_overview
        build_overview(self,PreviewPane)
        self.pins.changed.connect(self._pins_changed)
        self.pins.error.connect(self._pin_error)
        scrollbar=self.scroll.verticalScrollBar()
        scrollbar.valueChanged.connect(self._queue_visible_rows)
        scrollbar.rangeChanged.connect(self._update_scroll_gutter)
        self._update_scroll_gutter(scrollbar.minimum(),scrollbar.maximum())
        self.sidebar._selection_animation.setDuration(110)
        from ui_transitions import PreviewWidthTransition
        self.preview_width_motion=PreviewWidthTransition(self.preview,lambda:getattr(self.shelf.manager,'motion',True))
        from display_frames import DisplayAnimation
        from ui_transitions import exit_curve
        self._grid_shift=DisplayAnimation(self);self._grid_shift.setDuration(185)
        self._grid_shift.setEasingCurve(exit_curve())
        self._grid_shift.valueChanged.connect(self._position_live_rows)
        self._grid_shift.finished.connect(self._finish_grid_shift)
        self._grid_origins={};self._grid_fraction=1.
        sync=getattr(self.shelf.manager,'sync',None)
        if sync is not None and hasattr(sync,'metadata_changed'):
            sync.metadata_changed.connect(self._upload_metadata_changed)
        from ui_transitions import EmptyStateEntrance
        self.empty_entrance=EmptyStateEntrance(self.empty_drop,lambda:self.shelf.manager.motion)
        self._empty_entry_epoch=0;self._body_had_refs=None
        self.preview_width_motion.animation.finished.connect(self._resume_deferred_work)
        self.empty_entrance.settled.connect(self._resume_deferred_work)
        self.sidebar._selection_animation.finished.connect(self._resume_deferred_work)
        self.type_body._selection_animation.settled.connect(self._resume_deferred_work)
        self.type_body._right_edge.settled.connect(self._resume_deferred_work)
        self._responsive_timer=QTimer(self);self._responsive_timer.setSingleShot(True)
        self._responsive_timer.timeout.connect(self._apply_responsive_layout)
        self._sidebar_collapse=_SidebarCollapse(self)
        for row in self.scope_buttons.values():row.installEventFilter(self)
        self.scroll.viewport().installEventFilter(self)
        self.rebuild();self.update_transfer_status()
        self.drop_router=OverviewDropRouter(self)
        from transfer_entry import TransferEntryRouter
        self.transfer_entry=TransferEntryRouter(self,self.lan_nav,lambda mode,paths:self.shelf.manager.queue_transfer_paths(mode,paths))
        self.transfer_entry.active_changed.connect(self.empty_drop.set_transfer_choice_active)

    def eventFilter(self,watched,event):
        if event.type()==QEvent.Resize and watched in self.scope_buttons.values():
            self._position_nav_counts()
        if watched is self.scroll.viewport() and event.type()==QEvent.Resize and self._virtual_ready:
            if self._positioned_rows and event.size().width()!=event.oldSize().width():
                self._resize_positioned_rows()
            if self._virtual_rows:self._queue_visible_rows()
        if (watched is self.scroll.viewport() and event.type()==QEvent.Resize
                and event.size().width()!=event.oldSize().width()):
            sidebar=getattr(self,'_sidebar_collapse',None)
            if sidebar is not None and sidebar.width._timer.isActive():return False
            transition=getattr(self,'preview_width_motion',None)
            if transition is not None and transition.active:
                # The middle layout itself moves the search and controls.
                # Recompute the wider window only at an actual breakpoint,
                # rather than reapplying every sidebar and header property on
                # each intermediate pixel of the preview animation.
                width=self.middle.width()
                full=width>=OVERVIEW_FULL_TOOLBAR_MIN_WIDTH_PX
                sort_visible=width>=430
                if (getattr(self,'_responsive_toolbar_full',None)!=full
                        or (not self.sort_segment.isHidden())!=sort_visible):
                    self._responsive_timer.start(0)
            else:self._responsive_timer.start(0)
        return super().eventFilter(watched,event)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if not hasattr(self,'empty_state') or not hasattr(self,'scroll'):
            return
        self._apply_responsive_layout()

    def _scrollbar_gutter(self):
        bar=self.scroll.verticalScrollBar()
        style=bar.style()
        if style.styleHint(QStyle.SH_ScrollBar_Transient,None,bar):return 0
        return max(bar.sizeHint().width(),style.pixelMetric(QStyle.PM_ScrollBarExtent,None,bar))

    def _update_scroll_gutter(self, minimum, maximum):
        # Keep the viewport the same width when a non-overlay scrollbar
        # appears. The hidden bar is replaced by a blank gutter of equal size.
        gutter=self._scrollbar_gutter() if minimum==maximum else 0
        if self.scroll.viewportMargins().right()!=gutter:
            self.scroll.setViewportMargins(0,0,gutter,0)

    def _grid_available_width(self):
        margins=self.rows_layout.contentsMargins()
        viewport_width=self.scroll.viewport().width()
        reserved_width=self.scroll.contentsRect().width()-self._scrollbar_gutter()
        width=min(viewport_width,reserved_width)-margins.left()-margins.right()
        transition=getattr(self,'preview_width_motion',None)
        if transition is not None and transition.active and transition.target_visible:
            # Use the final viewport width throughout the resize; otherwise
            # cards are rebuilt and rearranged on every animation frame.
            width-=max(0,transition.target_width-self.preview.width())
        return max(1,width)

    def _grid_column_count(self):
        width=self._grid_available_width()
        available=max(GRID_CARD_MIN_WIDTH_PX,width)
        return max(1,min(3,(available+8)//(GRID_COLUMN_WIDTH_PX+8)))

    def _grid_card_width(self):
        width=self._grid_available_width()
        columns=self._grid_column_count()
        return max(1,(width-8*(columns-1))//columns)

    def _grid_horizontal_inset(self,columns,width):
        margins=self.rows_layout.contentsMargins()
        occupied=columns*width+(columns-1)*8
        gap=self.scroll.viewport().width()-margins.left()-margins.right()-occupied
        return max(0,gap)//2

    @property
    def view_mode(self):
        return self._pending_view_mode or self.shelf.manager.settings.view_mode

    def _reserve_rows_height(self):
        grid=self.view_mode=='icons'
        columns=self._grid_columns if grid else 1
        rows=(len(self._build_refs)+columns-1)//columns
        height,gap=(GRID_CARD_HEIGHT_PX,8) if grid else (FILE_ROW_HEIGHT_PX,6)
        margins=self.rows_layout.contentsMargins()
        total=margins.top()+margins.bottom()+rows*height+max(0,rows-1)*gap
        self.rows_body.setMinimumHeight(max(getattr(self,'_rows_minimum',0),total))

    def _place_file(self,index,widget):
        grid=isinstance(widget,IconItem)
        columns=self._grid_columns if grid else 1
        row,column=divmod(index,columns)
        margins=self.rows_layout.contentsMargins()
        x=margins.left()+self._grid_inset+column*(self._grid_cell_px+8)
        y=margins.top()+row*(GRID_CARD_HEIGHT_PX+8 if grid else FILE_ROW_HEIGHT_PX+6)
        origin=self._grid_origins.get(widget.path)
        if origin is not None:
            fraction=self._grid_fraction
            x=round(origin.x()+(x-origin.x())*fraction)
            y=round(origin.y()+(y-origin.y())*fraction)
        if widget.width()!=self._grid_cell_px:widget.setFixedWidth(self._grid_cell_px)
        if x!=widget.x() or y!=widget.y():widget.move(x,y)

    def _position_live_rows(self,fraction):
        self._grid_fraction=float(fraction)
        for ref,widget in self._item_widgets.items():
            self._place_file(self._virtual_index[ref],widget)

    def _finish_grid_shift(self):
        self._grid_shift.stop()
        self._grid_origins.clear();self._grid_fraction=1.
        if self._virtual_ready and self._positioned_rows:self._position_live_rows(1.)

    def _resize_positioned_rows(self):
        grid=self.view_mode=='icons'
        columns=self._grid_column_count() if grid else 1
        width=self._grid_card_width() if grid else self._grid_available_width()
        inset=self._grid_horizontal_inset(columns,width) if grid else 0
        if (self._grid_columns,self._grid_cell_px,self._grid_inset)==(columns,width,inset):return
        changed_columns=columns!=self._grid_columns
        from ui_transitions import motion_allowed
        animate=(changed_columns and self.isVisible() and self.shelf.manager.motion and motion_allowed()
                 and not self.shelf.content_transition_active())
        if animate:
            self._grid_origins={ref:widget.pos() for ref,widget in self._item_widgets.items()}
            self._grid_shift.stop();self._grid_fraction=0.
        self._grid_columns=columns;self._grid_cell_px=width;self._grid_inset=inset
        if changed_columns:self._reserve_rows_height()
        self._position_live_rows(self._grid_fraction)
        if animate and self._grid_origins:
            self._grid_shift.setStartValue(0.);self._grid_shift.setEndValue(1.);self._grid_shift.start()

    def prepare_open_layout(self):
        """Commit hidden-window breakpoints before taking the first snapshot."""
        self._responsive_timer.stop()
        self.layout().activate()
        self._apply_responsive_layout(animate_items=False)
        self.layout().activate()
        self.middle_layout.activate()
        self._apply_responsive_layout(animate_items=False)
        self._responsive_timer.stop()

    def _apply_responsive_layout(self, *, animate_items=True):
        narrow=self.width()<780
        previous=self._narrow_layout;self._narrow_layout=narrow
        condensed=self.width()<980
        toolbar_width=self.middle.width();toolbar_full=toolbar_width>=OVERVIEW_FULL_TOOLBAR_MIN_WIDTH_PX
        toolbar_changed=getattr(self,'_responsive_toolbar_full',None)!=toolbar_full
        self._responsive_toolbar_full=toolbar_full
        self.category_segments.hide();self.category_picker.setVisible(narrow);self.bulk_more.setVisible(not toolbar_full)
        self.select_all_button.show()
        self.sidebar.setVisible(not narrow)
        if hasattr(self,'_sidebar_collapse'):self._sidebar_collapse.update_size(176 if condensed else 210,not narrow)
        else:self.sidebar.setFixedWidth(176 if condensed else 210)
        self.header_brand.setFixedWidth(48 if self.width()<480 else 176 if condensed else 210)
        self.title.setVisible(self.width()>=480)
        self.count_label.setVisible(not condensed)
        self.header_status.setFixedWidth(238 if narrow else 280 if condensed else 340)
        self.selection_navigation.setVisible(narrow);self.selection_bar.setFixedHeight(92 if narrow else 56)
        self.search_edit.show();self.view_segment.show();self.sort_segment.setVisible(toolbar_width>=430)
        self.zip_all_button.setVisible(toolbar_full)
        self._position_nav_counts()
        if narrow:
            if hasattr(self,'preview_width_motion'):self.preview_width_motion.stop()
            if self.body_layout.indexOf(self.preview)>=0:
                self.body_layout.removeWidget(self.preview);self.middle_layout.insertWidget(3,self.preview,1)
            self.preview.setMinimumWidth(0);self.preview.setMaximumWidth(16777215)
        else:
            if self.middle_layout.indexOf(self.preview)>=0:
                self.middle_layout.removeWidget(self.preview);self.body_layout.addWidget(self.preview)
            if not getattr(getattr(self,'preview_width_motion',None),'active',False):self.preview.setFixedWidth(280 if condensed else 340)
        self.scroll.setMinimumWidth(0);self.scroll.setMaximumWidth(16777215)
        known=getattr(self,'_body_had_refs',None)
        self._sync_body_visibility(None if known is None else (True,) if known else ())
        if toolbar_changed or previous!=narrow:self.update_selection_actions()
        if previous!=narrow and hasattr(self,'_render_signature'):
            self._render_signature=None;self.rebuild(animate_items=animate_items)
        elif self._positioned_rows and self._virtual_ready:self._resize_positioned_rows()

    def _sync_body_visibility(self,refs=None):
        has_refs=bool(self.visible_refs() if refs is None else refs)
        previous=getattr(self,'_body_had_refs',None);self._body_had_refs=has_refs
        self.empty_state.setVisible(not has_refs)
        if hasattr(self,'empty_entrance') and previous!=has_refs:
            self._empty_entry_epoch+=1;epoch=self._empty_entry_epoch
            if has_refs:self.empty_entrance.finish()
            elif previous is True and self.isVisible() and not self.shelf.content_transition_active():
                def enter_empty():
                    if epoch==self._empty_entry_epoch and not self._body_had_refs and not self.shelf.content_transition_active():
                        self.empty_state.layout().activate();self.empty_entrance.begin()
                QTimer.singleShot(0,self,enter_empty)
        self.narrow_view_button.setVisible(self._narrow_layout and has_refs)
        self.narrow_view_button.setText('返回内容' if self._narrow_preview else '预览')
        self.narrow_view_button.setAccessibleName('返回内容列表' if self._narrow_preview else '查看选中内容的预览')
        self.scroll.setVisible(has_refs and (not self._narrow_layout or not self._narrow_preview))
        show_preview=not self._narrow_layout or (has_refs and self._narrow_preview)
        transition=getattr(self,'preview_width_motion',None)
        if transition is not None and not self._narrow_layout:
            transition.request(show_preview,280 if self.width()<980 else 340,animate=False)
        else:self.preview.setVisible(show_preview)

    def _position_nav_counts(self):
        for key,badge in self.nav_counts.items():
            control=self.scope_buttons[key]
            width=max(20,min(60,badge.fontMetrics().horizontalAdvance(badge.text())+12))
            left=control.width()-width-12
            label_end=12+control.iconSize().width()+8+control.fontMetrics().horizontalAdvance(control.text())
            visible=(self._sidebar_collapse.open and self.sidebar.width()>=124 and left>=label_end+4
                     and getattr(control,'_sidebar_label_alpha',1.)>=.85)
            if not visible:
                if not badge.isHidden():badge.hide()
                continue
            if badge.width()!=width:badge.setFixedSize(width,20)
            if badge.x()!=left:badge.move(left,(control.height()-20)//2)
            if badge.isHidden():badge.show()

    def toggle_narrow_preview(self):
        if not self.visible_refs():return
        self._narrow_preview=not self._narrow_preview
        if self._narrow_preview:
            self.restore_selected_preview()
        self._sync_body_visibility()
        if self._narrow_preview:self.preview.resume_preview()
        self.shelf.touch()

    def reset_for_open(self):
        """Reset navigation and selection without rebuilding unchanged items."""
        self._finish_file_motion()
        previous_selected=set(self.selected)
        self.category='all';self.scope='all';self.type_filter='all';self.selected.clear();self.primary_selection=None
        self.preview.clear_preview()
        self._narrow_preview=False
        self.search_edit.blockSignals(True);self.search_edit.clear();self.search_edit.blockSignals(False)
        self.category_picker.setText('全部')
        self.scroll.verticalScrollBar().setValue(0);self._build_position=0
        self.rebuild(animate_items=False)
        for key,control in self.scope_buttons.items():control.setChecked(key=='all')
        for key,control in self.type_buttons.items():control.setChecked(key=='all')
        for ref in previous_selected:
            item=self._item_widgets.get(ref)
            if isinstance(item,IconItem):item.set_selected(False)
            elif isinstance(item,FileRow):item.update()
        refs=self.visible_refs()
        self._sync_body_visibility(refs);self.update_selection_actions();self.update_transfer_status()

    def open_category_menu(self):
        menu=AnimatedMenu(self);style_menu(menu)
        menu._dropdown_anchor=self.category_picker
        for key,label in self.SCOPES:
            action=menu.addAction(label,lambda key=key:self.set_scope(key))
            action.setCheckable(True);action.setChecked(key==self.scope)
        menu.addSeparator()
        for key,label in self.TYPES:
            if key=='all':continue
            action=menu.addAction(label,lambda key=key:self.set_type(key))
            action.setCheckable(True);action.setChecked(key==self.type_filter)
        self.shelf._menu_open=True
        try:menu.exec(self.category_picker.mapToGlobal(QPoint(0,self.category_picker.height()+4)))
        finally:self.shelf._menu_open=False;menu.deleteLater();self.shelf.touch()

    def open_bulk_menu(self):
        menu=AnimatedMenu(self);style_menu(menu)
        menu._dropdown_anchor=self.bulk_more
        if not self.sort_segment.isVisible():
            menu.addAction('按时间排序',lambda:self.cycle_sort('time'))
            menu.addAction('按大小排序',lambda:self.cycle_sort('size'))
        menu.addSeparator()
        chosen=menu.addAction('打包所选…',self.compress_selected)
        chosen.setEnabled(self.zip_button.isEnabled())
        all_action=menu.addAction('打包全部…',lambda:self.shelf.compress_refs(list(self.shelf.files['permanent'])))
        all_action.setEnabled(self.zip_all_button.isEnabled())
        menu.addSeparator()
        clear=menu.addAction('删除项目…',self.shelf.confirm_delete)
        clear.setEnabled(self.clear_button.isEnabled())
        self.shelf._menu_open=True
        try:menu.exec(self.bulk_more.mapToGlobal(QPoint(0,self.bulk_more.height()+4)))
        finally:self.shelf._menu_open=False;menu.deleteLater();self.shelf.touch()

    def send_item(self,ref):
        self.send_refs([ref])

    def send_refs(self,refs,mode='lan'):
        paths=[]
        for ref in tuple(refs):
            path=self.preview.file_path_for(ref)
            if path is None:
                if self.preview.group_view.isVisible():self.preview.group_title.setText('部分所选文件暂不可用')
                return
            paths.append(path)
        if paths:self.shelf.manager.queue_transfer_paths(mode,paths)

    def remove_selected(self):
        for ref in tuple(self.chosen_refs()):self.shelf.remove_path('permanent',ref)

    def visible_refs(self):
        active=getattr(self,'_render_refs',None)
        if active is not None:return active
        transfers=self.shelf.content.transfers
        query=self.search_edit.text().strip().casefold() if hasattr(self,'search_edit') else ''
        snapshot=getattr(self,'_category_snapshot',None)
        all_refs,categories=snapshot if snapshot is not None else (self.shelf.display_refs(),None)
        refs=[ref for ref in reversed(all_refs) if
            (self.scope!='uploading' or ref in transfers) and (self.scope!='pinned' or self.is_pinned(ref)) and
            (self.type_filter=='all' or (categories[ref] if categories is not None else self.shelf.content.category(ref))==self.type_filter)
             and (not query or query in self.shelf.content.title(ref).casefold())]
        if self.sort_direction:
            if self.sort_key=='time':
                if self.sort_direction==1:refs.reverse()
            elif self.sort_key=='size':
                refs.sort(key=self._sort_size,reverse=self.sort_direction==2)
        refs.sort(key=lambda ref:not self.is_pinned(ref))
        return refs

    def is_pinned(self,ref):return self.pins.contains(ref)

    def toggle_pin(self,ref):self.pins.set(ref,not self.is_pinned(ref));self.shelf.touch()

    def _pins_changed(self):
        if not hasattr(self,'rows_body'):return
        self.rebuild()
        for item in self._item_widgets.values():
            if hasattr(item,'refresh_pin'):item.refresh_pin()
            item.update()

    def _pin_error(self,message):
        self.pin_error.setText(message);self.pin_error.setVisible(bool(message))

    def _sort_size(self,ref):
        content=self.shelf.content
        if content.is_text(ref):return len((content.text(ref) or '').encode('utf8'))
        pending=content.pending.get(ref,{})
        if 'size' in pending:return max(0,int(pending.get('size') or 0))
        cache=getattr(self.shelf,'file_metadata',None)
        if cache is not None:
            info=cache.request(ref)
            if info and info['is_dir']:return max(0,int(self.shelf.request_directory_size(ref) or 0))
            return max(0,int((info or {}).get('size') or content.metadata.get(ref,{}).get('size') or 0))
        try:
            path=Path(ref)
            if path.is_dir():
                value=self.shelf.request_directory_size(ref)
                return max(0,int(value or 0))
            return path.stat().st_size
        except OSError:return max(0,int(content.metadata.get(ref,{}).get('size') or 0))

    def reset_sort(self):
        self.sort_key=None;self.sort_direction=0
        self._rebuild_sorted()

    def cycle_sort(self,key):
        if key not in ('time','size'):return
        self.sort_direction=(self.sort_direction+1)%3 if self.sort_key==key else 1
        self.sort_key=key if self.sort_direction else None
        if self.sort_key=='size' and hasattr(self.shelf,'file_metadata'):
            for ref in self.shelf.files['permanent']:
                if not self.shelf.content.is_text(ref) and not self.shelf.content.is_pending(ref):self.shelf.file_metadata.refresh(ref)
        self._rebuild_sorted()

    def _rebuild_sorted(self):
        self._sort_change=True
        try:self.rebuild()
        finally:self._sort_change=False
        self.update_selection_actions()
        self.shelf.touch()

    def update_transfer_status(self):
        self.shelf.transfer_rates.observe(self.shelf.content.transfers)
        count=len(self.shelf.content.transfers)
        self.transfer_badge.setText('99+' if count>99 else str(count))
        self._position_nav_counts()
        self.category_buttons['uploading'].setAccessibleName(f'传输中的项目，共 {count} 项')
        self._refresh_transfer_strip()
        self.preview.update_transfer_status()
        if tuple(self.visible_refs()) != tuple(getattr(self,'_build_refs',())):self.rebuild()

    def _refresh_transfer_strip(self):
        transfers=self.shelf.content.transfers
        refs=[ref for ref in reversed(self.shelf.display_refs()) if ref in transfers]
        uploads=[ref for ref in refs if is_local_upload_transfer(transfers[ref])]
        self._strip_refs=(uploads or refs)[:1]
        self.transfer_strip.setVisible(bool(refs))
        if not refs:return
        self.strip_title.setText(f'传输中 · {len(refs)} 项')
        self.strip_more.hide()
        for index,(row,name,percent,progress) in enumerate(self.strip_rows):
            active=index<len(self._strip_refs)
            row.setVisible(active)
            if not active:continue
            ref=self._strip_refs[index];state=transfers[ref]
            status=transfer_phase_label(state)
            title=self.shelf.content.title(ref)
            if len(uploads)>1:
                status=('正在上传' if any(transfers[item].get('phase') in ('uploading','verifying') for item in uploads) else '等待上传')
                title=f'{len(uploads)} 个文件'
            name.set_transfer(status,title)
            name.setAccessibleName(f'{status} {title}，查看传输详情')
            aggregate=self._aggregate_upload_state(uploads) if uploads else state
            total=max(0,int(aggregate.get('total') or 0));received=max(0,int(aggregate.get('received') or 0))
            value=round(1000*min(1,received/total)) if total else 0
            progress.setRange(0,1000);progress.setValue(value)
            fraction=transfer_fraction(aggregate)
            self.transfer_strip.set_progress(fraction)
            percent.setToolTip(self.shelf.transfer_rates.remaining_text('__aggregate__' if uploads else ref,aggregate))
        # Children change text and width together. Recompose after layout so a
        # first update cannot leave a clipped percentage in the window cache.
        self.transfer_strip.layout().activate()
        self.shelf.surface_effect.invalidate_source();self.shelf.card.update()

    def _aggregate_upload_state(self,refs):
        """Weight concurrent uploads by known bytes; never invent unknown totals."""
        received=total=0;known=True
        for ref in refs:
            state=self.shelf.content.transfers[ref]
            size=max(0,int(state.get('total') or 0))
            if not size and state.get('phase') in ('queued','validating','preparing'):
                if self.shelf.content.is_text(ref):
                    size=len((self.shelf.content.text(ref) or '').encode('utf8'))
                else:
                    cache=getattr(self.shelf,'file_metadata',None)
                    info=cache.request(ref) if cache is not None else None
                    if info and info.get('exists') and not info.get('is_dir'):size=max(0,int(info.get('size') or 0))
            if not size:known=False
            total+=size;received+=min(size,max(0,int(state.get('received') or 0))) if size else 0
        return dict(phase='uploading',received=received,total=total if known else 0)

    def _open_strip_transfer(self,index):
        if index>=len(self._strip_refs):return
        ref=self._strip_refs[index]
        self.set_category('uploading')
        if ref in self.visible_refs():self.select_item(ref)

    def set_mode(self,mode="permanent"): self.rebuild()

    def set_category(self,key):
        # Existing callers (transfer strip, tests and shortcuts) retain their
        # category API. The visible UI has independent scope/type controls.
        if key not in dict(self.CATEGORIES):return
        self._set_filters('uploading' if key=='uploading' else 'all','all' if key=='uploading' else key)

    def set_scope(self,key):
        if key in dict(self.SCOPES):self._set_filters(key,'all')

    def set_type(self,key):
        if key in dict(self.TYPES):self._set_filters(self.scope,key)

    def _set_filters(self,scope,kind):
        changed=(scope,kind)!=(self.scope,self.type_filter)
        if changed and self.isVisible() and self.shelf.manager.motion:
            self.retirement.pause()
        # All filtering and sidebar counts in this synchronous update share one
        # classification pass, including potentially expensive .ts probing.
        previous=getattr(self,'_category_snapshot',None)
        refs=tuple(self.shelf.display_refs());categories={ref:self.shelf.content.category(ref) for ref in refs}
        self._category_snapshot=(refs,categories)
        try:
            if changed:self._file_entry_requested=True
            self.scope=scope;self.type_filter=kind;self.category='uploading' if scope=='uploading' else kind;self._narrow_preview=False
            for name,button in self.scope_buttons.items():button.setChecked(name==scope)
            for name,button in self.type_buttons.items():button.setChecked(name==kind)
            self.category_picker.setText(dict(self.TYPES)[kind] if kind!='all' else dict(self.SCOPES)[scope])
            self.transfer_strip.setVisible(bool(self.shelf.content.transfers))
            # One paint for the whole swap: rebuilding row by row with updates
            # enabled is what made large categories feel like a stall.
            self.setUpdatesEnabled(False)
            try:self.rebuild();self.update_transfer_status()
            finally:self.setUpdatesEnabled(True)
            self.update();self.shelf.touch()
        finally:
            self._category_snapshot=previous
            QTimer.singleShot(0,self,self._resume_deferred_work)

    def _layout_motion_active(self):
        """Give panel and navigation motion an uninterrupted frame budget."""
        from PySide6.QtCore import QAbstractAnimation
        return (self.preview_width_motion.active or self.empty_entrance.active
                or self.type_body._selection_animation._timer.isActive() or self.type_body._right_edge._timer.isActive()
                or self.sidebar._selection_animation.state()==QAbstractAnimation.Running)

    def _blocking_file_load_motion(self):
        # The navigation indicator can run alongside the shared file entrance.
        # Only a changing viewport width or empty-state capture delays creation.
        return self.preview_width_motion.active or self.empty_entrance.active

    def _interactive_motion_active(self):
        """Keep expensive offscreen row work out of the active frame budget."""
        items=getattr(self,'item_transitions',None)
        return (self._layout_motion_active() or bool(items and (items.entry_started is not None
                or items.entry_pending or items.timer.isActive())))

    def _resume_deferred_work(self):
        if not self.isVisible() or self.shelf.content_transition_active():return
        if self._blocking_file_load_motion():return
        if getattr(self,'_file_entry_pending',False):
            if self._build_cursor<len(self._build_refs) and not self._build_timer.isActive():self._build_timer.start(0)
            return
        if self._interactive_motion_active():return
        self.retirement.resume()
        if getattr(self,'_build_cursor',0)<len(getattr(self,'_build_refs',())) and not self._build_timer.isActive():
            self._build_timer.start(0)

    def _clear_layout(self,layout):
        while layout.count():
            item=layout.takeAt(0)
            if item.widget(): self.retirement.retire(item.widget())
            elif item.layout(): self._clear_layout(item.layout()); item.layout().deleteLater()

    def rebuild(self, *, animate_items=True):
        if getattr(self,'_render_refs',None) is not None:return
        if getattr(self,'_category_snapshot',None) is None:
            all_refs=self.shelf.display_refs()
            self._category_snapshot=(all_refs,None)
            temporary=True
        else:temporary=False
        try:
            self._render_refs=self.visible_refs()
            self._rebuild_files(self._render_refs,animate_items)
        finally:
            self._render_refs=None
            if temporary:self._category_snapshot=None

    def _rebuild_files(self,refs,animate_items):
        animate_items=animate_items and not self.shelf.content_transition_active()
        previous_signature=getattr(self,'_render_signature',None)
        content=self.shelf.content
        signature=(tuple(refs),(self.scope,self.type_filter),self.view_mode,
                   self.shelf.manager.settings.theme,
                   tuple((ref,repr(content.metadata.get(ref,{})),
                          repr(content.pending.get(ref,{})),content.texts.get(ref),content.title(ref),self.is_pinned(ref)) for ref in refs))
        if getattr(self,'_render_signature',None)==signature:
            return
        self._read_cloud_statuses(refs)
        self._render_signature=signature
        # Start preview resizing before measuring grid columns so they can use
        # the destination width from the very first file frame.
        self._sync_body_visibility(refs)
        snapshot=getattr(self,'_category_snapshot',None)
        all_refs,categories=(snapshot if snapshot is not None else
            (self.shelf.display_refs(),None))
        if categories is None:categories={ref:content.category(ref) for ref in all_refs}
        scope_refs=[ref for ref in all_refs if (self.scope!='pinned' or self.is_pinned(ref)) and (self.scope!='uploading' or ref in content.transfers)]
        category_counts=Counter(categories[ref] for ref in scope_refs)
        position=self.scroll.verticalScrollBar().value()
        old_widgets=self._item_widgets
        row_type=IconItem if self.view_mode=='icons' else FileRow
        same_layout=bool(previous_signature and previous_signature[:4]==signature[:4]
                         and all(isinstance(w,row_type) for w in old_widgets.values()))
        previous_metadata={row[0]:row[1:] for row in previous_signature[4]} if previous_signature else {}
        changed={row[0] for row in signature[4] if previous_metadata.get(row[0])!=row[1:]}
        # Metadata/progress cannot retire the live tree or disturb a focused row.
        if not same_layout:
            if (getattr(self,'_sort_change',False) and animate_items and previous_signature
                    and previous_signature[0]!=signature[0] and previous_signature[1:4]==signature[1:4]):
                if self._capture_file_motion('sort'):self._file_entry_requested=True
            self._build_timer.stop()
            self._virtual_ready=False
            self._finish_grid_shift()
            self._build_epoch=getattr(self,'_build_epoch',0)+1
            self._rows_minimum=getattr(self,'_rows_minimum',self.rows_body.minimumHeight())
            self.rows_body.setMinimumHeight(max(self._rows_minimum,self.rows_body.height()))
            from item_transitions import ItemTransitions
            if not hasattr(self,'item_transitions'):
                self.item_transitions=ItemTransitions(self.scroll,self.rows_body)
                self.item_transitions.duration=.17
                self.item_transitions.settled.connect(self._resume_deferred_work)
            positions=self.item_transitions.capture(old_widgets)
            wanted=set(refs)
            removed={ref:w for ref,w in old_widgets.items() if ref not in wanted}
            animate=bool(animate_items and previous_signature and self.isVisible() and self.shelf.manager.motion
                         and previous_signature[1:4]==signature[1:4])
            present=set(all_refs)
            self.item_transitions.remove(removed.values(),enabled=animate and all(ref in present for ref in removed))
            self._detach_rows_layout(self.rows_layout)
            self._item_widgets={ref:w for ref,w in old_widgets.items() if ref in wanted and isinstance(w,row_type)}
            for ref,w in old_widgets.items():
                if self._item_widgets.get(ref) is not w:self.retirement.retire(w)
            self._layout_animation=(positions,animate)
            # A batch deletion settles the live rows before the entry effect.
            # Moving several surviving widgets through the old rows would
            # make the visible order and hit areas disagree during the update.
            batch_deleted=animate and len(removed)>1 and removed.keys().isdisjoint(present)
            self._file_entry_pending=bool(animate_items and refs and (getattr(self,'_file_entry_requested',False) or batch_deleted)
                                          and self.isVisible() and self.shelf.manager.motion)
            self._file_entry_requested=False
        self.selected.intersection_update(refs)
        if self.primary_selection not in self.selected: self.primary_selection=next((r for r in refs if r in self.selected),None)
        self.title.setText("FlickDock");self.count_label.setText(f"{len(all_refs)} 项")
        from reference_widgets import css,icon,palette
        for key,button in self.scope_buttons.items():
            label=dict(self.SCOPES)[key]
            if key=='uploading':count=len(self.shelf.content.transfers)
            elif key=='all':count=len(all_refs)
            else:count=sum(self.is_pinned(ref) for ref in all_refs)
            button.setText(label)
            self.nav_counts[key].setText(str(count))
            selected=key==self.scope
            from native_ui_common import colors as theme_colors
            background='transparent' if selected else palette()['rail_item'] if key=='uploading' and count else 'transparent'
            self.nav_counts[key].set_background(background)
            css(self.nav_counts[key],*[f"background:transparent;border:0;font:500 10px 'FlickDock UI';color:{theme_colors(t)['rail_active_ink'] if selected else theme_colors(t)['brand_accent'] if (key=='uploading' and count) else theme_colors(t)['rail_item_ink']};" for t in ('dark','light')])
            glyph=button._reference_icon[0]
            icon_role='rail_active_ink' if selected else 'rail_item_ink'
            button._reference_icon=(glyph,icon_role);button.setIcon(icon(glyph,palette()[icon_role]))
            button.setChecked(key==self.scope)
        for key,title in self.TYPES:
            control=self.type_buttons[key];count=len(scope_refs) if key=='all' else category_counts[key]
            control.setText(f'{title} {count}');control.setFixedWidth(control.fontMetrics().horizontalAdvance(control.text())+16)
            control.setChecked(key==self.type_filter);control.setAccessibleName(f'{title}，{count} 项')
        self.clear_button.setEnabled(self.shelf.has_files())
        self._position_nav_counts()
        virtual=len(refs)>=VIRTUAL_FILE_THRESHOLD
        positioned=virtual or row_type is IconItem
        if same_layout:
            for ref in changed:
                if ref in self._item_widgets:self._item_widgets[ref].refresh_content()
        elif positioned:
            columns=self._grid_columns=self._grid_column_count() if row_type is IconItem else 1
            self._grid_cell_px=self._grid_card_width() if row_type is IconItem else self._grid_available_width()
            self._grid_inset=self._grid_horizontal_inset(columns,self._grid_cell_px) if row_type is IconItem else 0
            def append(index,ref):
                widget=self._item_widget(ref,row_type,changed)
                self._place_file(index,widget);widget.show()
            def remove(index,ref):
                widget=self._item_widgets.pop(ref,None)
                if widget is None:return
                self._build_installed.discard(ref);self.retirement.retire(widget)
        else:
            def append(index,ref):
                slot=self._build_slots.pop(index,None)
                if slot is not None:self.rows_layout.removeItem(slot)
                widget=self._item_widget(ref,FileRow,changed)
                self.rows_layout.insertWidget(min(index,self.rows_layout.count()-1),widget);widget.show()
            def remove(index,ref):
                widget=self._item_widgets.pop(ref,None)
                if widget is None:return
                self.rows_layout.removeWidget(widget)
                slot=QSpacerItem(0,FILE_ROW_HEIGHT_PX,QSizePolicy.Minimum,QSizePolicy.Fixed)
                self.rows_layout.insertItem(index,slot);self._build_slots[index]=slot
                self._build_installed.discard(ref);self.retirement.retire(widget)
        if not same_layout:
            self.rows_layout.addStretch()
            self._build_refs=refs;self._build_cursor=0;self._build_append=append;self._build_remove=remove
            self._virtual_rows=virtual;self._positioned_rows=positioned
            self._virtual_index={ref:index for index,ref in enumerate(refs)}
            self._build_position=position
            self._build_installed=set()
            self._build_slots={}
            # Positioned rows reserve the scrollbar range arithmetically.
            # The only widgets we lay out are the ones actually materialized.
            if positioned:
                for ref in tuple(self._item_widgets):
                    append(self._virtual_index[ref],ref);self._build_installed.add(ref)
            else:
                for index,ref in enumerate(refs):
                    if ref in self._item_widgets:
                        append(index,ref);self._build_installed.add(ref)
                    else:
                        slot=QSpacerItem(0,FILE_ROW_HEIGHT_PX,QSizePolicy.Minimum,QSizePolicy.Fixed)
                        self.rows_layout.insertItem(index,slot);self._build_slots[index]=slot
            # A scroll widget cannot infer an accurate minimum from the
            # temporary spacers until all children exist. Reserve its final
            # height now; otherwise Qt overlaps hundreds of rows inside the
            # viewport and the compositor paints every thumbnail per slice.
            self._reserve_rows_height()
            self.rows_layout.activate()
            positions,animate=self._layout_animation;self._layout_animation=None
            if self._file_entry_pending:
                self.item_transitions.layout_pending=False;self.item_transitions.finish()
                self._file_entry_pending=self.item_transitions.prepare_entrance(
                    enabled=True,mode_source=getattr(self,'_mode_source',None),
                    mode=getattr(self,'_file_motion_kind','view'))
            else:self.item_transitions.reflow(self._item_widgets,positions,enabled=animate)
            self._mode_source=None
            self._virtual_ready=True
            if self._virtual_rows:self._queue_visible_rows(schedule=False)
        # Four initial cards are enough to establish the endpoint. Remaining
        # widgets wait until the transition ends, with a small per-tick budget.
            self._append_batch(initial=True)
        if refs:
            current=self.primary_selection
            if len(self.chosen_refs())==1 and not (self._narrow_layout and not self._narrow_preview):
                # The preview owns media handles. A name/size update must not
                # reopen it; a changed cover or text payload may refresh it.
                old_meta=getattr(self,'_preview_metadata',{})
                new_meta={key:content.metadata.get(current,{}).get(key) for key in ('preview_image','text_file')}
                new_meta['text']=content.texts.get(current)
                self.preview.show_path(current,force=current==self.preview.current_path and old_meta!=new_meta)
                self._preview_metadata=new_meta
                if current in changed:
                    self.preview.name.setText(content.title(current))
        else:
            self.empty_drop.set_idle_title('搁架是空的' if not self.shelf.has_files() else '没有这类内容')
            self.empty_hint.setVisible(not self.shelf.has_files())
            self.empty_symbol.setPixmap(glyph_pixmap('shelf',42,ui_theme.color('secondary'),dpr=self.devicePixelRatioF()))
        self.restore_selected_preview()
        self._sync_body_visibility(refs)
        self.scroll.verticalScrollBar().setValue(position)
        self.update_selection_actions()

    def _detach_rows_layout(self,layout):
        while layout.count():
            item=layout.takeAt(0)
            if item.layout():
                self._detach_rows_layout(item.layout());item.layout().deleteLater()

    def remap_items(self,mapping):
        self.pins.remap(mapping,notify=False)
        # A remote reservation becomes a verified local path without becoming
        # a new UI item. Retarget the live widget before capturing transitions;
        # otherwise the same file briefly fades out over its replacement.
        for old,new in mapping.items():
            if old==new or old not in self._item_widgets or new in self._item_widgets:continue
            widget=self._item_widgets.pop(old);self._item_widgets[new]=widget
            widget.path=new
            if hasattr(widget,'ref'):widget.ref=new
            if hasattr(widget,'icon_label'):widget.icon_label.ref=new
        if getattr(self,'_render_signature',None):
            refs,category,mode,theme,metadata=self._render_signature
            self._render_signature=(tuple(mapping.get(ref,ref) for ref in refs),category,mode,theme,
                tuple((mapping.get(row[0],row[0]),*row[1:]) for row in metadata))
        self._build_refs=[mapping.get(ref,ref) for ref in self._build_refs]
        self._build_installed={mapping.get(ref,ref) for ref in self._build_installed}
        self._virtual_index={ref:index for index,ref in enumerate(self._build_refs)}
        if self._virtual_rows:
            self._queue_visible_rows()

    def _item_widget(self,ref,kind,changed):
        widget=self._item_widgets.get(ref)
        if widget is None:
            widget=kind(self,ref);self._item_widgets[ref]=widget
        elif ref in changed:widget.refresh_content()
        return widget

    def _append_batch(self,initial=False):
        if not initial and (self.shelf.content_transition_active() or not self.isVisible()): return
        # Constructing thumbnails, styling controls and activating their
        # layouts competes with every preview-width frame. Finish that short
        # transition first, then build the visible files in bounded slices.
        if getattr(self,'_file_entry_pending',False) and self._blocking_file_load_motion():return
        if self._virtual_rows:
            self._append_visible_batch(initial)
            return
        # Build enough for the visible viewport first.  Once its shared entry
        # starts, wait for the panel and navigation motion to finish before
        # constructing rows below the fold.
        if not initial and not getattr(self,'_file_entry_pending',False) and self._interactive_motion_active():return
        start=time.perf_counter(); count=0
        while self._build_cursor<len(self._build_refs):
            index=self._build_cursor
            ref=self._build_refs[index];self._build_cursor+=1
            if ref in self._build_installed:continue
            self._build_append(index,ref);self._build_installed.add(ref);count+=1
            # One bounded construction slice per paint, instead of repainting
            # the complete composited surface after every one or two rows.
            building_visible=getattr(self,'_file_entry_pending',False)
            if count>=(4 if initial else 3 if building_visible else 6) or time.perf_counter()-start>=(.003 if initial or building_visible else .008): break
        if getattr(self,'_file_entry_pending',False):
            self.rows_layout.activate()
            columns=getattr(self,'_grid_columns',1) if self.view_mode=='icons' else 1
            row_height=(GRID_CARD_HEIGHT_PX+8 if self.view_mode=='icons' else FILE_ROW_HEIGHT_PX+6)
            visible=min(len(self._build_refs),(math.ceil(max(1,self.scroll.viewport().height())/row_height)+1)*columns)
            if self._build_cursor>=visible:
                self.item_transitions.enter(self._item_widgets);self._file_entry_pending=False
        if self._build_cursor<len(self._build_refs):
            if not self.shelf.content_transition_active() and not self._blocking_file_load_motion() and (getattr(self,'_file_entry_pending',False) or not self._interactive_motion_active()):
                self._build_timer.start(1)
        else:
            if not self._positioned_rows:self.rows_body.setMinimumHeight(getattr(self,'_rows_minimum',0))
            self.rows_layout.activate()
            # The user may have scrolled while new widgets were constructed.
            # The range was held stable; do not restore an obsolete position.
            pending=getattr(self,'_layout_animation',None)
            if pending:
                self._layout_animation=None
                positions,animate=pending
                self.item_transitions.reflow(self._item_widgets,positions,enabled=animate)

    def _visible_file_indices(self):
        columns=getattr(self,'_grid_columns',1) if self.view_mode=='icons' else 1
        row_height=(GRID_CARD_HEIGHT_PX+8 if self.view_mode=='icons' else FILE_ROW_HEIGHT_PX+6)
        top=max(0,self.scroll.verticalScrollBar().value()//row_height-1)
        rows=math.ceil(max(1,self.scroll.viewport().height())/row_height)+3
        end=min(len(self._build_refs),(top+rows)*columns)
        return top*columns,end,rows*columns

    def _queue_visible_rows(self,*_,schedule=True):
        if not self._virtual_rows or not self._virtual_ready:return
        start,end,visible=self._visible_file_indices();count=len(self._build_refs)
        # Keep the visible range first, followed by one viewport of prefetch
        # in either direction. No file content is needed to create the slots.
        order=list(range(start,end))+list(range(end,min(count,end+visible)))+list(range(max(0,start-visible),start))
        self._visible_queue=[index for index in order if self._build_refs[index] not in self._build_installed]
        self._build_cursor=self._visible_queue[0] if self._visible_queue else count
        if schedule and self._visible_queue and self.isVisible() and not self._blocking_file_load_motion() and not self._build_timer.isActive():
            self._build_timer.start(0)

    def _append_visible_batch(self,initial=False):
        if not initial and not getattr(self,'_file_entry_pending',False) and self._interactive_motion_active():return
        started=time.perf_counter();count=0
        while self._visible_queue:
            index=self._visible_queue.pop(0);ref=self._build_refs[index]
            if ref in self._build_installed:continue
            self._build_append(index,ref);self._build_installed.add(ref);count+=1
            if count>=3 or time.perf_counter()-started>=.003:break
        start,end,visible=self._visible_file_indices()
        if getattr(self,'_file_entry_pending',False):
            self.rows_layout.activate()
            if all(self._build_refs[i] in self._build_installed for i in range(start,end)):
                self.item_transitions.enter(self._item_widgets);self._file_entry_pending=False
        self._build_cursor=self._visible_queue[0] if self._visible_queue else len(self._build_refs)
        if self._visible_queue:
            if not self._blocking_file_load_motion() and (self._file_entry_pending or not self._interactive_motion_active()):self._build_timer.start(1)
            return
        # Bound retained widgets after a long scroll; selection lives in the
        # model, and fixed-size slots keep the scrollbar and card widths stable.
        limit=max(40,visible*3)
        if len(self._item_widgets)>limit:
            keep_start=max(0,start-visible);keep_end=min(len(self._build_refs),end+visible)
            candidates=[]
            for ref,widget in self._item_widgets.items():
                index=self._virtual_index[ref]
                if not keep_start<=index<keep_end and not widget.hasFocus():
                    candidates.append((abs(index-(start+end)//2),index,ref))
            candidates.sort(reverse=True)
            for _,index,ref in candidates[:min(3,len(self._item_widgets)-limit)]:self._build_remove(index,ref)
            if len(self._item_widgets)>limit and candidates:self._build_timer.start(8)

    def resume_content(self):
        if self.shelf.content_transition_active() or not self.isVisible(): return
        if self._content_dirty:
            self._content_dirty=False;self.rebuild();self.update_transfer_status();self.refresh_cloud_status()
        if self._build_cursor<len(self._build_refs): self._build_timer.start(1)
        self.preview.resume_preview()

    def prepare_transition(self):
        """Capture one complete viewport; defer only rows outside that viewport."""
        self._finish_file_motion()
        self.empty_entrance.finish()
        for section in (self,self.middle,self.scroll):
            layout=section.layout()
            if layout is not None:layout.activate()
        columns=getattr(self,'_grid_columns',1) if self.view_mode=='icons' else 1
        row_height=GRID_CARD_HEIGHT_PX+8 if self.view_mode=='icons' else FILE_ROW_HEIGHT_PX+6
        count=min(OVERVIEW_TRANSITION_MAX_ITEMS,(math.ceil(max(1,self.scroll.viewport().height())/row_height)+1)*columns,len(self._build_refs))
        self._build_timer.stop()
        for index in range(count):
            if self._build_refs[index] not in self._build_installed:
                self._build_append(index,self._build_refs[index]);self._build_installed.add(self._build_refs[index])
        if self._virtual_rows:self._queue_visible_rows(schedule=False)
        else:
            while self._build_cursor<len(self._build_refs) and self._build_refs[self._build_cursor] in self._build_installed:self._build_cursor+=1
        self.rows_layout.activate()

    def _finish_file_motion(self):
        self._file_entry_requested=self._file_entry_pending=False
        self._finish_grid_shift()
        transitions=getattr(self,'item_transitions',None)
        if transitions is not None:transitions.finish()

    def hideEvent(self,event):
        self._build_timer.stop()
        self._sidebar_collapse.finish_immediately()
        self.empty_entrance.finish()
        self._finish_file_motion()
        self.retirement.resume()
        super().hideEvent(event)

    def chosen_refs(self):
        return [ref for ref in self.shelf.files['permanent'] if ref in self.selected]

    def update_selection_actions(self):
        self.shelf.surface_effect.invalidate_source()
        count=len(self.chosen_refs());busy=self.shelf.archive_job is not None
        self.preview.update_action_states()
        choosing=self.shelf._archive_choosing
        self.zip_button.setVisible(self.middle.width()>=OVERVIEW_FULL_TOOLBAR_MIN_WIDTH_PX)
        self.zip_button.setText('')
        self.zip_button.setAccessibleName('打包中…' if busy else '选择保存位置…' if choosing else '打包所选')
        self.zip_button.setEnabled(bool(count) and not busy and not choosing)
        self.zip_all_button.setEnabled(bool(self.shelf.files['permanent']) and not busy and not choosing)
        self.zip_button.setToolTip('正在打包…' if busy else '选择保存位置…' if choosing else f'将选中的 {count} 项打包为 ZIP')
        for key,control in self.sort_buttons.items():
            direction=self.sort_direction if self.sort_key==key else 0
            control.setText('时间' if key=='time' else '大小');control.set_direction(direction)
            control.setChecked(bool(direction))
            control.setToolTip(('正序' if direction==1 else '倒序' if direction==2 else '默认：最新添加在前')+' · 点击切换')
        if not self.sort_direction:self.sort_segment.clear_selection()
        visible=self.visible_refs()
        self.select_all_button.setText('弃选' if visible and all(ref in self.selected for ref in visible) else '全选')
        from reference_widgets import css,colors
        deselect=bool(visible and all(ref in self.selected for ref in visible))
        css(self.select_all_button,*[f"QPushButton{{background:transparent;border:0;padding:5px 7px;font:12px 'FlickDock UI';color:{colors(t)['red'] if deselect else colors(t)['secondary']};}}" for t in ('dark','light')])

    def change_view_mode(self,key):
        if key not in ('list','icons') or key==self.view_mode:return
        self._file_entry_requested=True
        self._capture_file_motion('view')
        self._pending_view_mode=key
        if self._view_writer is None:
            self._view_writer=self.shelf.manager.settings.preference_writer()
            self._view_writer.completed.connect(self._view_preferences_saved)
        self._view_writer.request(view_mode=key)
        self.sync_view_mode()
        QTimer.singleShot(0,self,self._resume_deferred_work)

    def _capture_file_motion(self,kind):
        from ui_transitions import motion_allowed
        transitions=getattr(self,'item_transitions',None)
        if (transitions is None or not self.isVisible() or not self.shelf.manager.motion
                or self.shelf.content_transition_active() or not motion_allowed()):return False
        self._mode_source=transitions.capture_view_mode(self._item_widgets)
        self._file_motion_kind=kind
        if self._mode_source:self.retirement.pause()
        return bool(self._mode_source)

    def _view_preferences_saved(self,keys,error):
        if self._pending_view_mode is None or self._view_writer.busy:return
        if 'view_mode' in keys and error:self._pin_error('视图设置未能保存：'+error)
        self._pending_view_mode=None
        self.sync_view_mode()

    def sync_view_mode(self):
        self.view_list.setChecked(self.view_mode=='list')
        self.view_grid.setChecked(self.view_mode=='icons')
        self.rebuild()

    def toggle_select_all_visible(self):
        refs=self.visible_refs()
        if refs and all(ref in self.selected for ref in refs):self.selected.difference_update(refs)
        else:self.selected.update(refs)
        self.primary_selection=next((r for r in refs if r in self.selected),None)
        for item in self.rows_body.findChildren(FileRow):item.update()
        self.restore_selected_preview()
        self.update_selection_actions()

    def select_item(self,ref,toggle=False,extend=False):
        refs=self.visible_refs()
        if extend and self.primary_selection in refs and ref in refs:
            start,end=sorted((refs.index(self.primary_selection),refs.index(ref)))
            self.selected.update(refs[start:end+1])
        elif toggle:
            if ref in self.selected: self.selected.remove(ref)
            else: self.selected.add(ref)
        elif ref not in self.selected: self.selected={ref}
        self.primary_selection=ref if ref in self.selected else next((r for r in self.visible_refs() if r in self.selected),None)
        self.restore_selected_preview()
        for item in self.rows_body.findChildren(QWidget):
            if hasattr(item,'set_selected'): item.set_selected(item.path in self.selected)
            elif isinstance(item,FileRow): item.update()
        self.update_selection_actions()

    def restore_selected_preview(self):
        chosen=self.chosen_refs()
        if self.primary_selection not in self.visible_refs():
            self.preview.clear_preview();self._preview_metadata={};return
        if self._narrow_layout and not self._narrow_preview:return
        if len(chosen)>1:self.preview.show_group(chosen)
        else:self.preview.show_path(self.primary_selection)

    def compress_selected(self):
        self.shelf.compress_refs(self.chosen_refs())

    def show_item_menu(self,ref,position):
        if ref not in self.selected:self.select_item(ref)
        refs=self.chosen_refs() or [ref]
        menu=AnimatedMenu(self);style_menu(menu)
        if hasattr(self.shelf.manager,'sync') and self.shelf.manager.sync.is_upload_paused(ref):
            menu.addAction('上传至云端',lambda:self.shelf.manager.sync.retry_upload(ref))
        if ref in self.shelf.content.transfers:
            phase=self.shelf.content.transfers[ref].get('phase')
            menu.addAction('取消复制' if phase=='copying' else '取消下载' if phase=='downloading' else '取消上传',lambda:self.shelf.cancel_transfer(ref))
        if self.shelf.content.is_pending(ref):
            self.shelf._menu_open=True
            try:menu.exec(position)
            finally:self.shelf._menu_open=False;menu.deleteLater();self.shelf.touch()
            return
        if not self.shelf.content.is_text(ref):
            menu.addAction('在文件夹中显示',lambda:self.shelf.reveal_ref(ref))
            menu.addAction('复制文件路径',lambda:QApplication.clipboard().setText(ref))
        menu.addAction('复制所选内容',lambda:self.shelf.copy_items(refs))
        ready=all(not self.shelf.content.is_pending(item) for item in refs)
        menu.addAction('使用局域网传输',lambda:self.send_refs(refs,'lan')).setEnabled(ready)
        menu.addAction('使用远程传输',lambda:self.send_refs(refs,'wan')).setEnabled(ready)
        action=menu.addAction('将所选内容打包为 ZIP…',lambda:self.shelf.compress_refs(refs))
        action.setEnabled(self.shelf.archive_job is None)
        if len(refs)==1 and not self.shelf.content.is_text(ref):
            menu.addAction('重命名文件…',lambda:self.shelf.rename_ref(ref))
        if any(not self.shelf.content.is_text(item) for item in refs):
            menu.addAction('移动所选文件到…',lambda:self.shelf.move_files('permanent',refs))
        menu.addAction('从搁架移除此项',lambda:self.remove_path(ref))
        self.shelf._menu_open=True
        try:menu.exec(position)
        finally:self.shelf._menu_open=False;menu.deleteLater();self.shelf.touch()

    def update_item_icon(self,ref):
        from reference_widgets import invalidate_thumbnail
        invalidate_thumbnail(self.shelf,ref)
        item=self._item_widgets.get(ref)
        if isinstance(item,IconItem):item.update()
        elif isinstance(item,FileRow):item.icon_label.update()

    def _read_cloud_statuses(self,refs):
        sync=getattr(self.shelf.manager,'sync',None)
        batch=getattr(sync,'sync_statuses_for_refs',None)
        self._cloud_statuses=(batch(refs) if callable(batch) else
                              {ref:cloud_status_for_ref(self.shelf,ref) for ref in refs})
        metadata=getattr(sync,'upload_metadata_for_refs',None)
        self._upload_metadata=metadata(refs) if callable(metadata) else {}

    def upload_caption(self,ref,*,compact=False):
        from shelf_content import shelf_time_info
        label,stamp=shelf_time_info(self.shelf.content,ref,compact=compact)
        return ('记录于 '+stamp) if label=='记录时间' else stamp

    def _upload_metadata_changed(self,ref):
        sync=getattr(self.shelf.manager,'sync',None);batch=getattr(sync,'upload_metadata_for_refs',None)
        if callable(batch):self._upload_metadata.update(batch((ref,)))
        # A cloud-copy operation changes the status as well as metadata.
        # Keep this O(1) per ref: a stale icon must never turn a delete click
        # into an upload through the action handler.
        if ref in self._cloud_statuses:
            self._cloud_statuses[ref]=cloud_status_for_ref(self.shelf,ref)
        item=self._item_widgets.get(ref)
        if item is not None:
            item.refresh_upload_time();item.refresh_cloud_status()
        if self.preview.current_path==ref:self.preview.refresh_cloud_status()
        elif ref in getattr(self.preview,'_group_refs',()):self.preview.update_action_states()

    def cloud_status(self,ref):
        if ref in self._cloud_statuses:return self._cloud_statuses[ref]
        return cloud_status_for_ref(self.shelf,ref)

    def refresh_cloud_status(self):
        previous=self._cloud_statuses
        previous_metadata=self._upload_metadata
        self._read_cloud_statuses(self.visible_refs())
        if previous!=self._cloud_statuses or previous_metadata!=self._upload_metadata:
            for item in self.rows_body.findChildren(QWidget):
                if isinstance(item,(IconItem,FileRow)):
                    item.refresh_cloud_status();item.refresh_upload_time()
        self.preview.refresh_cloud_status()

    def remove_path(self,ref):
        self.shelf.remove_path('permanent',ref)
        if ref not in self.shelf.files['permanent'] and self.is_pinned(ref):self.pins.set(ref,False)
