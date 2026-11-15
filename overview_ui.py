"""Overview navigation, file rows, selection and incremental layout."""
from __future__ import annotations
from collections import Counter

from PySide6.QtCore import QEvent
from PySide6.QtCore import QPoint
from PySide6.QtCore import QPointF
from PySide6.QtCore import QRectF
from PySide6.QtCore import QTimer
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtGui import QPainter
from PySide6.QtGui import QPen
from PySide6.QtGui import QRegion
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QFrame
from PySide6.QtWidgets import QGraphicsEffect
from PySide6.QtWidgets import QGridLayout
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QSizePolicy
from PySide6.QtWidgets import QSpacerItem
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from content_grid import GRID_CARD_HEIGHT_PX
from content_grid import GRID_CARD_MIN_WIDTH_PX
from content_grid import GRID_COLUMN_WIDTH_PX
from content_grid import IconItem
from drop_routing import OverviewDropRouter
from file_preview import ElidedLabel
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


class OverviewOutline(QWidget):
    """Paint the window edge above its opaque overview child surfaces."""
    def __init__(self,parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if sys.platform=='win32':
            # QVideoWidget can promote this otherwise alien overlay to a
            # native HWND. Windows does not honor Qt's mouse-transparent flag
            # for that HWND, so exclude the whole interior from its region.
            rect=self.rect()
            inner=rect.adjusted(4,4,-4,-4)
            self.setMask(QRegion(rect).subtracted(QRegion(inner)) if not inner.isEmpty() else QRegion(rect))

    def paintEvent(self,event):
        from native_ui_common import pixel_aligned_border
        painter=QPainter(self);painter.setRenderHint(QPainter.Antialiasing)
        rect,width=pixel_aligned_border(painter,QRectF(self.rect()))
        tint=QColor(255,255,255,20) if ui_theme.current_theme()=='dark' else QColor(0,0,0,23)
        painter.setPen(QPen(tint,width));painter.setBrush(Qt.NoBrush)
        radius=16 if sys.platform=='darwin' else 10
        painter.drawRoundedRect(rect,radius,radius);painter.end()


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
        css(self.selection_check,'QCheckBox::indicator{width:14px;height:14px;border:1px solid #45454B;border-radius:4px;background:transparent;} QCheckBox::indicator:checked{background:#0A84FF;border-color:#0A84FF;}',
            'QCheckBox::indicator{width:14px;height:14px;border:1px solid #CCCCCC;border-radius:4px;background:transparent;} QCheckBox::indicator:checked{background:#0A84FF;border-color:#0A84FF;}')
        row.addWidget(self.selection_check)
        self.icon_label=ThumbnailLabel(popup.shelf,path,self);self.icon_label.setAttribute(Qt.WA_TransparentForMouseEvents);row.addWidget(self.icon_label)
        names=QVBoxLayout();names.setSpacing(0)
        self.name_label=ElidedLabel(self);self.name_label.setText(popup.shelf.content.title(path));self.name_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        css(self.name_label,"color:#F5F5F7;background:transparent;border:0;font:600 12px 'FlickDock UI';","color:#1D1D1F;background:transparent;border:0;font:600 12px 'FlickDock UI';")
        self.detail_label=ElidedLabel(self);self.detail_label.setFixedHeight(17)
        from native_ui_common import colors
        css(self.detail_label,*[f"color:{colors(t)['file_description']};background:transparent;border:0;font:9px 'FlickDock UI';" for t in ('dark','light')])
        self.detail_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        names.addWidget(self.name_label);names.addWidget(self.detail_label);row.addLayout(names,1)
        self.remove_button=ToolbarButton('close',self,motion=popup.shelf.manager.motion);self.remove_button.setFixedSize(32,32);self.remove_button.setToolTip('从搁架移除');self.remove_button.setAccessibleName('从搁架移除 '+popup.shelf.content.title(path));self.remove_button.clicked.connect(lambda:popup.remove_path(self.path))
        self.remove_button.set_glyph_tint('#FF453A')
        policy=self.remove_button.sizePolicy();policy.setRetainSizeWhenHidden(True);self.remove_button.setSizePolicy(policy)
        row.addWidget(self.remove_button);self.remove_button.hide()
        self.refresh_cloud_status();self.refresh_upload_time()

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
        self.icon_label.update();self.refresh_cloud_status();self.update()

    def refresh_cloud_status(self):
        lookup=getattr(self.popup,'cloud_status',None)
        status=lookup(self.path) if callable(lookup) else cloud_status_for_ref(self.popup.shelf,self.path)
        self.icon_label.set_cloud_status(status)
        self.setToolTip(file_presence_label(status))
        self.setAccessibleDescription(file_presence_label(status))

    def refresh_upload_time(self):
        from reference_widgets import item_detail
        self.detail_label.setText(item_detail(self.popup.shelf,self.path)+' · '+self.popup.upload_caption(self.path))

    def paintEvent(self,event):
        from reference_widgets import palette,ink,outside_content_view
        from native_ui_common import pixel_aligned_border
        if outside_content_view(self):return
        c=palette();chosen=self.path in self.popup.selected
        self.selection_check.blockSignals(True);self.selection_check.setChecked(chosen);self.selection_check.blockSignals(False)
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        focused=self.hasFocus() and self._keyboard_focus
        p.setBrush(ink(c['file_card_selected_focus'] if chosen and focused else c['file_card_selected'] if chosen else c['file_card_hover'] if self.motion.values['highlight']>.3 or focused else c['file_card']))
        border,stroke=pixel_aligned_border(p,QRectF(self.rect()))
        p.setPen(QPen(ink(c['selected_border'] if chosen else c['border']),stroke))
        p.drawRoundedRect(border,c['card_radius'],c['card_radius'])
        p.end()

    def enterEvent(self, event):
        self.remove_button.show()
        self.popup.shelf.touch()
        self.motion.animate({"highlight": 1.0}, response=.14, damping=1)
        super().enterEvent(event)

    def leaveEvent(self, event):
        visible=(self.hasFocus() and self._keyboard_focus) or self.remove_button.hasFocus()
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
            self._pending_deselect=False;self.did_drag=False
            self.popup.select_item(self.path)
            self.popup.shelf.open_path(self.path);self.popup.shelf.touch()
            # QWidget's default double-click handler re-enters mousePressEvent,
            # which would arm deselection again before the final release.
            event.accept();return
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
            self.popup.shelf.open_path(self.path)
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
            visible=self.underMouse() or self.remove_button.hasFocus()
            self.remove_button.setVisible(visible)
        QTimer.singleShot(0,self,update_actions);self.update();super().focusOutEvent(event)


class ContentsPopup(QFrame):
    CATEGORIES=(("all","全部"),("images","图片"),("video","视频"),("audio","音频"),("text","文字"),("files","文件"),("links","网页"),("uploading","传输中"))
    def __init__(self,shelf,mode="permanent"):
        super().__init__(shelf.card)
        self.shelf,self.mode=shelf,"permanent"
        self.selected=set();self.primary_selection=None;self.category='all'
        self._cloud_statuses={}
        self._upload_metadata={}
        self.sort_key=None;self.sort_direction=0
        self._item_widgets={}
        self._content_dirty=False
        self._virtual_rows=False;self._virtual_ready=False;self._visible_queue=[]
        self.retirement=WidgetRetirementQueue(self)
        self._build_timer=QTimer(self);self._build_timer.setSingleShot(True)
        self._build_timer.timeout.connect(self._append_batch)
        from reference_widgets import build_overview
        build_overview(self,PreviewPane)
        self.scroll.verticalScrollBar().valueChanged.connect(self._queue_visible_rows)
        self.sidebar._selection_animation.setDuration(160)
        self.window_outline=OverviewOutline(self);self.window_outline.setGeometry(self.rect())
        self.window_outline.show();self.window_outline.raise_()
        from ui_transitions import PreviewWidthTransition
        self.preview_width_motion=PreviewWidthTransition(self.preview,lambda:getattr(self.shelf.manager,'motion',True))
        sync=getattr(self.shelf.manager,'sync',None)
        if sync is not None and hasattr(sync,'metadata_changed'):
            sync.metadata_changed.connect(self._upload_metadata_changed)
        from ui_transitions import EmptyStateEntrance
        self.empty_entrance=EmptyStateEntrance(self.empty_drop,lambda:self.shelf.manager.motion)
        self._empty_entry_epoch=0;self._body_had_refs=None
        self.preview_width_motion.animation.finished.connect(self._resume_deferred_work)
        self.empty_entrance.settled.connect(self._resume_deferred_work)
        self.sidebar._selection_animation.finished.connect(self._resume_deferred_work)
        self._responsive_timer=QTimer(self);self._responsive_timer.setSingleShot(True)
        self._responsive_timer.timeout.connect(self._apply_responsive_layout)
        self.scroll.viewport().installEventFilter(self)
        self.rebuild();self.update_transfer_status()
        self.drop_router=OverviewDropRouter(self)
        from transfer_entry import TransferEntryRouter
        self.transfer_entry=TransferEntryRouter(self,self.lan_nav,lambda mode,paths:self.shelf.manager.queue_transfer_paths(mode,paths))
        self.transfer_entry.active_changed.connect(self.empty_drop.set_transfer_choice_active)

    def eventFilter(self,watched,event):
        if watched is self.scroll.viewport() and event.type()==QEvent.Resize and self._virtual_rows and self._virtual_ready:
            QTimer.singleShot(0,self,self._queue_visible_rows)
        if (watched is self.scroll.viewport() and event.type()==QEvent.Resize
                and event.size().width()!=event.oldSize().width()):
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
        if hasattr(self,'window_outline'):
            self.window_outline.setGeometry(self.rect());self.window_outline.raise_()
        if not hasattr(self,'empty_state') or not hasattr(self,'scroll'):
            return
        self._apply_responsive_layout()

    def _grid_available_width(self):
        # Reserve the scrollbar gutter even while it is hidden. Adding enough
        # cards to make it appear must not change column count or card width.
        gutter=max(12,self.scroll.verticalScrollBar().sizeHint().width())
        maximum=min(self.scroll.maximumViewportSize().width(),self.scroll.contentsRect().width())-gutter
        viewport=self.scroll.viewport().width()
        # Native macOS scrollbars and a pending resize can make Qt's maximum
        # larger than the actual viewport during incremental cloud arrivals.
        width=min(maximum,viewport) if viewport>0 else maximum
        margins=self.rows_layout.contentsMargins()
        width-=margins.left()+margins.right()
        transition=getattr(self,'preview_width_motion',None)
        if transition is not None and transition.active and transition.target_visible:
            # Use the final viewport width throughout the resize; otherwise
            # cards are rebuilt and rearranged on every animation frame.
            width-=max(0,transition.target_width-self.preview.width())
        return max(1,width)

    def _grid_column_count(self):
        width=self._grid_available_width()
        available=max(GRID_CARD_MIN_WIDTH_PX,width)
        return max(1,min(4,(available+8)//(GRID_COLUMN_WIDTH_PX+8)))

    def _grid_card_width(self):
        width=self._grid_available_width()
        columns=self._grid_column_count()
        return max(1,(width-8*(columns-1))//columns)

    def _apply_responsive_layout(self):
        narrow=self.width()<780
        previous=self._narrow_layout;self._narrow_layout=narrow
        condensed=self.width()<980
        toolbar_width=self.middle.width();toolbar_full=toolbar_width>=OVERVIEW_FULL_TOOLBAR_MIN_WIDTH_PX
        toolbar_changed=getattr(self,'_responsive_toolbar_full',None)!=toolbar_full
        self._responsive_toolbar_full=toolbar_full
        self.category_segments.hide();self.category_picker.setVisible(narrow);self.bulk_more.setVisible(not toolbar_full)
        self.select_all_button.show()
        self.sidebar.setVisible(not narrow)
        self.sidebar.setFixedWidth(176 if condensed else 210)
        self.header_brand.setFixedWidth(48 if self.width()<480 else 176 if condensed else 210)
        self.title.setVisible(self.width()>=480)
        self.count_label.setVisible(not condensed)
        self.header_status.setFixedWidth(238 if narrow else 280 if condensed else 340)
        self.selection_navigation.setVisible(narrow);self.selection_bar.setFixedHeight(76 if narrow else 40)
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
        columns=self._grid_column_count()
        grid_changed=(self.shelf.manager.settings.view_mode=='icons' and bool(self._body_had_refs)
                      and (getattr(self,'_grid_columns',None)!=columns or getattr(self,'_grid_cell_px',None)!=self._grid_card_width()))
        if (previous!=narrow or grid_changed) and hasattr(self,'_render_signature'):
            self._render_signature=None;self.rebuild()

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
        show_preview=has_refs and (not self._narrow_layout or self._narrow_preview)
        transition=getattr(self,'preview_width_motion',None)
        if transition is not None and not self._narrow_layout:
            transition.request(show_preview,280 if self.width()<980 else 340,
                animate=previous is not None and previous!=has_refs and not self.shelf.content_transition_active())
        else:self.preview.setVisible(show_preview)

    def _position_nav_counts(self):
        for key,badge in self.nav_counts.items():
            control=self.category_buttons[key]
            width=max(20,min(60,badge.fontMetrics().horizontalAdvance(badge.text())+12))
            badge.setFixedSize(width,20);badge.setAlignment(Qt.AlignCenter)
            badge.move(max(0,control.width()-width-12),(control.height()-20)//2)

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
        previous_selected=set(self.selected)
        self.category='all';self.selected.clear();self.primary_selection=None
        self.preview.clear_preview()
        self._narrow_preview=False
        self.search_edit.blockSignals(True);self.search_edit.clear();self.search_edit.blockSignals(False)
        self.category_picker.setText('全部')
        self.scroll.verticalScrollBar().setValue(0);self._build_position=0
        self.rebuild()
        for key,control in self.category_buttons.items():control.setChecked(key=='all')
        for ref in previous_selected:
            item=self._item_widgets.get(ref)
            if isinstance(item,IconItem):item.set_selected(False)
            elif isinstance(item,FileRow):item.update()
        refs=self.visible_refs()
        self._sync_body_visibility(refs);self.update_selection_actions();self.update_transfer_status()

    def open_category_menu(self):
        menu=AnimatedMenu(self);style_menu(menu)
        for key,label in self.CATEGORIES:
            action=menu.addAction(label,lambda key=key:self.set_category(key))
            action.setCheckable(True);action.setChecked(key==self.category)
        self.shelf._menu_open=True
        try:menu.exec(self.category_picker.mapToGlobal(QPoint(0,self.category_picker.height()+4)))
        finally:self.shelf._menu_open=False;menu.deleteLater();self.shelf.touch()

    def open_bulk_menu(self):
        menu=AnimatedMenu(self);style_menu(menu)
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
        transfers=self.shelf.content.transfers
        query=self.search_edit.text().strip().casefold() if hasattr(self,'search_edit') else ''
        snapshot=getattr(self,'_category_snapshot',None)
        all_refs,categories=snapshot if snapshot is not None else (self.shelf.display_refs(),None)
        refs=[ref for ref in reversed(all_refs) if
            (ref in transfers if self.category=='uploading' else
             (self.category=='all' or (categories[ref] if categories is not None else self.shelf.content.category(ref))==self.category))
             and (not query or query in self.shelf.content.title(ref).casefold())]
        if self.sort_direction:
            if self.sort_key=='time':
                if self.sort_direction==1:refs.reverse()
            elif self.sort_key=='size':
                refs.sort(key=self._sort_size,reverse=self.sort_direction==2)
        return refs

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
        self.sort_key=None;self.sort_direction=0;self.rebuild();self.update_selection_actions();self.shelf.touch()

    def cycle_sort(self,key):
        if key not in ('time','size'):return
        self.sort_direction=(self.sort_direction+1)%3 if self.sort_key==key else 1
        self.sort_key=key if self.sort_direction else None
        if self.sort_key=='size' and hasattr(self.shelf,'file_metadata'):
            for ref in self.shelf.files['permanent']:
                if not self.shelf.content.is_text(ref) and not self.shelf.content.is_pending(ref):self.shelf.file_metadata.refresh(ref)
        self.rebuild();self.update_selection_actions();self.shelf.touch()

    def update_transfer_status(self):
        self.shelf.transfer_rates.observe(self.shelf.content.transfers)
        states=list(self.shelf.content.transfers.values())
        count=len(states)
        self.transfer_badge.setText('99+' if count>99 else str(count) if count else '')
        self._position_nav_counts()
        self.transfer_badge.setVisible(bool(count))
        self.category_buttons['uploading'].setAccessibleName(f'传输中的项目，共 {count} 项')
        self.transfer_summary.setVisible(bool(count) and self.category=='uploading')
        self._refresh_transfer_strip()
        if count:
            received=sum(v.get('received',0) for v in states);total=sum(v.get('total',0) for v in states)
            copying=sum(v.get('phase')=='copying' for v in states)
            downloading=sum(v.get('phase')=='downloading' for v in states)
            verifying=sum(v.get('phase')=='verifying' for v in states)
            retrying=sum(v.get('phase')=='retrying' for v in states)
            validating=sum(v.get('phase')=='validating' for v in states)
            uploading=count-copying-downloading-verifying-retrying-validating
            self.transfer_count.setText(' · '.join(f'{label} {amount} 项' for label,amount in
                (('正在复制',copying),('正在上传',uploading),('等待确认',verifying),
                 ('正在下载',downloading),('校验中',validating),('等待重试',retrying)) if amount))
            all_known=all(transfer_fraction(state) is not None for state in states)
            self.transfer_bar.setRange(0,1000 if all_known else 0)
            if all_known:self.transfer_bar.setValue(round(1000*min(1,received/total)))
            overall_state=dict(received=received,total=total if all_known else 0,phase='uploading')
            fraction=transfer_fraction(overall_state)
            progress=f'{round(fraction*100)}% · ' if fraction is not None else ''
            self.transfer_eta.setText(progress+self.shelf.transfer_rates.remaining_text('__aggregate__',overall_state))
        else:self.transfer_count.clear();self.transfer_eta.clear()
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
        if key not in self.category_buttons: return
        if key!=self.category and self.isVisible() and self.shelf.manager.motion:
            self.retirement.pause()
        # All filtering and sidebar counts in this synchronous update share one
        # classification pass, including potentially expensive .ts probing.
        previous=getattr(self,'_category_snapshot',None)
        refs=tuple(self.shelf.display_refs());categories={ref:self.shelf.content.category(ref) for ref in refs}
        self._category_snapshot=(refs,categories)
        try:
            if key!=self.category:self._file_entry_requested=True
            self.category=key;self._narrow_preview=False
            for name,button in self.sidebar_buttons.items():button.setChecked(name==key)
            self.category_picker.setText(next(label for item,label in self.CATEGORIES if item==key))
            self.transfer_summary.setVisible(bool(self.shelf.content.transfers) and key=='uploading')
            self.transfer_strip.setVisible(bool(self.shelf.content.transfers))
            self.selected.intersection_update(self.visible_refs())
            if self.primary_selection not in self.selected: self.primary_selection=None
            self.rebuild();self.update_transfer_status();self.shelf.touch()
        finally:
            self._category_snapshot=previous
            QTimer.singleShot(0,self,self._resume_deferred_work)

    def _layout_motion_active(self):
        """Give panel and navigation motion an uninterrupted frame budget."""
        from PySide6.QtCore import QAbstractAnimation
        return (self.preview_width_motion.active or self.empty_entrance.active
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

    def rebuild(self):
        refs=self.visible_refs()
        previous_signature=getattr(self,'_render_signature',None)
        content=self.shelf.content
        signature=(tuple(refs),self.category,self.shelf.manager.settings.view_mode,
                   self.shelf.manager.settings.theme,
                   tuple((ref,repr(content.metadata.get(ref,{})),
                          repr(content.pending.get(ref,{})),content.texts.get(ref),content.title(ref)) for ref in refs))
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
        category_counts=Counter(categories.values())
        position=self.scroll.verticalScrollBar().value()
        old_widgets=self._item_widgets
        row_type=IconItem if self.shelf.manager.settings.view_mode=='icons' else FileRow
        same_layout=bool(previous_signature and previous_signature[:4]==signature[:4]
                         and all(isinstance(w,row_type) for w in old_widgets.values()))
        previous_metadata={row[0]:row[1:] for row in previous_signature[4]} if previous_signature else {}
        changed={row[0] for row in signature[4] if previous_metadata.get(row[0])!=row[1:]}
        # Metadata/progress cannot retire the live tree or disturb a focused row.
        if not same_layout:
            self._build_timer.stop()
            self._virtual_ready=False
            self._build_epoch=getattr(self,'_build_epoch',0)+1
            self._rows_minimum=getattr(self,'_rows_minimum',self.rows_body.minimumHeight())
            self.rows_body.setMinimumHeight(max(self._rows_minimum,self.rows_body.height()))
            from item_transitions import ItemTransitions
            if not hasattr(self,'item_transitions'):
                self.item_transitions=ItemTransitions(self.scroll,self.rows_body)
                self.item_transitions.duration=.17
                self.item_transitions.settled.connect(self._resume_deferred_work)
            positions=self.item_transitions.capture(old_widgets)
            removed={ref:w for ref,w in old_widgets.items() if ref not in refs}
            animate=bool(previous_signature and self.isVisible() and self.shelf.manager.motion
                         and previous_signature[1:4]==signature[1:4])
            present=set(all_refs)
            self.item_transitions.remove(removed.values(),enabled=animate and all(ref in present for ref in removed))
            self._detach_rows_layout(self.rows_layout)
            self._item_widgets={ref:w for ref,w in old_widgets.items() if ref in refs and isinstance(w,row_type)}
            for ref,w in old_widgets.items():
                if self._item_widgets.get(ref) is not w:self.retirement.retire(w)
            self._layout_animation=(positions,animate)
            self._file_entry_pending=bool(refs and getattr(self,'_file_entry_requested',False) and self.isVisible()
                                          and not self.shelf.content_transition_active() and self.shelf.manager.motion)
            self._file_entry_requested=False
        self.selected.intersection_update(refs)
        if self.primary_selection not in self.selected: self.primary_selection=next((r for r in refs if r in self.selected),None)
        self.title.setText("FlickDock");self.count_label.setText(f"{len(all_refs)} 项")
        from reference_widgets import css,icon,palette
        for key,button in self.sidebar_buttons.items():
            label=next(name for name_key,name in self.CATEGORIES if name_key==key)
            if key=='uploading':count=len(self.shelf.content.transfers)
            elif key=='all':count=len(all_refs)
            else:count=category_counts[key]
            button.setText(label)
            self.nav_counts[key].setText(str(count) if count else '')
            self.nav_counts[key].setVisible(bool(count))
            selected=key==self.category
            background='transparent' if selected else palette()['control'] if key=='uploading' and count else 'transparent'
            css(self.nav_counts[key],f"background:{background};border:0;border-radius:10px;font:10px 'FlickDock UI';color:"+(palette()['on_action'] if selected else palette()['link'] if key=='uploading' else palette()['muted'])+';')
            glyph=button._reference_icon[0]
            button._reference_icon=(glyph,'on_action' if selected else 'secondary');button.setIcon(icon(glyph,palette()['on_action'] if selected else palette()['secondary']))
            button.setChecked(key==self.category)
        self.clear_button.setEnabled(self.shelf.has_files())
        self._position_nav_counts()
        self.category_buttons[self.category].setChecked(True)
        if same_layout:
            for ref in changed:
                if ref in self._item_widgets:self._item_widgets[ref].refresh_content()
        elif self.shelf.manager.settings.view_mode=='icons':
            grid=QGridLayout(); grid.setAlignment(Qt.AlignTop|Qt.AlignHCenter); grid.setContentsMargins(0,0,0,0); grid.setHorizontalSpacing(0); grid.setVerticalSpacing(8)
            self.rows_layout.addLayout(grid)
            columns=self._grid_column_count()
            self._grid_columns=columns
            cell=self._grid_cell_px=self._grid_card_width()
            # QGridLayout collapses normal spacing next to QSpacerItems. Give
            # gaps their own columns so incomplete/virtual rows never shift.
            for column in range(columns):
                grid.setColumnMinimumWidth(column*2,cell)
                if column<columns-1:grid.setColumnMinimumWidth(column*2+1,8)
            # Reserve the same fixed-width cells for incomplete rows.
            for index in range(len(refs),((len(refs)+columns-1)//columns)*columns):
                grid.addItem(QSpacerItem(cell,GRID_CARD_HEIGHT_PX,QSizePolicy.Fixed,QSizePolicy.Fixed),index//columns,(index%columns)*2)
            def append(index,ref):
                slot=self._build_slots.pop(index,None)
                if slot is not None:grid.removeItem(slot)
                widget=self._item_widget(ref,IconItem,changed)
                widget.setFixedWidth(cell)
                grid.addWidget(widget,index//columns,(index%columns)*2);widget.show()
            def remove(index,ref):
                widget=self._item_widgets.pop(ref,None)
                if widget is None:return
                grid.removeWidget(widget)
                slot=QSpacerItem(cell,GRID_CARD_HEIGHT_PX,QSizePolicy.Fixed,QSizePolicy.Fixed)
                grid.addItem(slot,index//columns,(index%columns)*2);self._build_slots[index]=slot
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
            self._virtual_rows=len(refs)>=VIRTUAL_FILE_THRESHOLD
            self._virtual_index={ref:index for index,ref in enumerate(refs)}
            self._build_position=position
            self._build_installed=set()
            self._build_slots={}
            # Move existing widgets in one layout transaction. Only new widget
            # construction is batched; the viewport never sees a short list.
            for index,ref in enumerate(refs):
                if ref in self._item_widgets:
                    append(index,ref);self._build_installed.add(ref)
                elif row_type is IconItem:
                    slot=QSpacerItem(cell,GRID_CARD_HEIGHT_PX,QSizePolicy.Fixed,QSizePolicy.Fixed)
                    grid.addItem(slot,index//columns,(index%columns)*2);self._build_slots[index]=slot
                else:
                    slot=QSpacerItem(0,FILE_ROW_HEIGHT_PX,QSizePolicy.Minimum,QSizePolicy.Fixed)
                    self.rows_layout.insertItem(index,slot);self._build_slots[index]=slot
            # A scroll widget cannot infer an accurate minimum from the
            # temporary spacers until all children exist. Reserve its final
            # height now; otherwise Qt overlaps hundreds of rows inside the
            # viewport and the compositor paints every thumbnail per slice.
            rows=(len(refs)+columns-1)//columns if row_type is IconItem else len(refs)
            height=24+rows*(GRID_CARD_HEIGHT_PX if row_type is IconItem else FILE_ROW_HEIGHT_PX)+max(0,rows-1)*(8 if row_type is IconItem else 6)
            self.rows_body.setMinimumHeight(max(getattr(self,'_rows_minimum',0),height))
            self.rows_layout.activate()
            positions,animate=self._layout_animation;self._layout_animation=None
            if self._file_entry_pending:
                self.item_transitions.layout_pending=False;self.item_transitions.finish()
                self._file_entry_pending=self.item_transitions.prepare_entrance(enabled=True)
            else:self.item_transitions.reflow(self._item_widgets,positions,enabled=animate)
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
            self.empty_title.setText('搁架是空的' if not self.shelf.has_files() else '没有这类内容')
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
        if self._virtual_rows:
            self._virtual_index={ref:index for index,ref in enumerate(self._build_refs)}
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
            columns=getattr(self,'_grid_columns',1) if self.shelf.manager.settings.view_mode=='icons' else 1
            row_height=(GRID_CARD_HEIGHT_PX+8 if self.shelf.manager.settings.view_mode=='icons' else FILE_ROW_HEIGHT_PX+6)
            visible=min(len(self._build_refs),(math.ceil(max(1,self.scroll.viewport().height())/row_height)+1)*columns)
            if self._build_cursor>=visible:
                self.item_transitions.enter();self._file_entry_pending=False
        if self._build_cursor<len(self._build_refs):
            if not self.shelf.content_transition_active() and not self._blocking_file_load_motion() and (getattr(self,'_file_entry_pending',False) or not self._interactive_motion_active()):
                self._build_timer.start(1)
        else:
            self.rows_body.setMinimumHeight(getattr(self,'_rows_minimum',0))
            self.rows_layout.activate()
            # The user may have scrolled while new widgets were constructed.
            # The range was held stable; do not restore an obsolete position.
            pending=getattr(self,'_layout_animation',None)
            if pending:
                self._layout_animation=None
                positions,animate=pending
                self.item_transitions.reflow(self._item_widgets,positions,enabled=animate)

    def _visible_file_indices(self):
        columns=getattr(self,'_grid_columns',1) if self.shelf.manager.settings.view_mode=='icons' else 1
        row_height=(GRID_CARD_HEIGHT_PX+8 if self.shelf.manager.settings.view_mode=='icons' else FILE_ROW_HEIGHT_PX+6)
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
                self.item_transitions.enter();self._file_entry_pending=False
        self._build_cursor=self._visible_queue[0] if self._visible_queue else len(self._build_refs)
        if self._visible_queue:
            if not self._blocking_file_load_motion() and (self._file_entry_pending or not self._interactive_motion_active()):self._build_timer.start(1)
            return
        # Bound retained widgets after a long scroll; selection lives in the
        # model, and fixed-size slots keep the scrollbar and card widths stable.
        limit=max(40,visible*3)
        if len(self._item_widgets)>limit:
            keep_start=max(0,start-visible);keep_end=min(len(self._build_refs),end+visible)
            candidates=sorted(((abs(index-(start+end)//2),index,ref) for ref,index in self._virtual_index.items()
                if (index<keep_start or index>=keep_end) and ref in self._item_widgets
                and not self._item_widgets[ref].hasFocus()),reverse=True)
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
        self.empty_entrance.finish()
        for section in (self,self.middle,self.scroll):
            layout=section.layout()
            if layout is not None:layout.activate()
        columns=getattr(self,'_grid_columns',1) if self.shelf.manager.settings.view_mode=='icons' else 1
        row_height=GRID_CARD_HEIGHT_PX+8 if columns>1 or self.shelf.manager.settings.view_mode=='icons' else FILE_ROW_HEIGHT_PX+6
        count=min(OVERVIEW_TRANSITION_MAX_ITEMS,(math.ceil(max(1,self.scroll.viewport().height())/row_height)+1)*columns,len(self._build_refs))
        self._build_timer.stop()
        for index in range(count):
            if self._build_refs[index] not in self._build_installed:
                self._build_append(index,self._build_refs[index]);self._build_installed.add(self._build_refs[index])
        if self._virtual_rows:self._queue_visible_rows(schedule=False)
        else:
            while self._build_cursor<len(self._build_refs) and self._build_refs[self._build_cursor] in self._build_installed:self._build_cursor+=1
        self.rows_layout.activate()

    def hideEvent(self,event):
        self._build_timer.stop()
        self.empty_entrance.finish()
        transitions=getattr(self,'item_transitions',None)
        if transitions is not None:transitions.finish()
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
        visible=self.visible_refs()
        self.select_all_button.setText('弃选' if visible and all(ref in self.selected for ref in visible) else '全选')
        from reference_widgets import css,colors
        deselect=bool(visible and all(ref in self.selected for ref in visible))
        css(self.select_all_button,*[f"QPushButton{{background:transparent;border:1px solid transparent;border-radius:8px;padding:4px 6px;font:12px 'FlickDock UI';color:{colors(t)['red'] if deselect else colors(t)['secondary']};}} QPushButton:hover{{background:{colors(t)['hover']};}} QPushButton[keyboardFocus=\"true\"]{{border-color:{colors(t)['link']};}}" for t in ('dark','light')])

    def change_view_mode(self,key):
        if key not in ('list','icons'):return
        if key!=self.shelf.manager.settings.view_mode:
            self._file_entry_requested=True
            if self.isVisible() and self.shelf.manager.motion:self.retirement.pause()
        self.shelf.manager.settings.update(view_mode=key)
        self.view_list.setChecked(key=='list')
        self.view_grid.setChecked(key=='icons')
        self.rebuild()
        QTimer.singleShot(0,self,self._resume_deferred_work)

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
            menu.addAction('打开文件',lambda:self.shelf.open_path(ref))
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
        item=self._item_widgets.get(ref)
        if item is not None:item.refresh_upload_time()
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

    def remove_path(self,ref): self.shelf.remove_path('permanent',ref)
