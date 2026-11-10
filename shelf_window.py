"""The one permanent native shelf and its existing surface transitions."""
from __future__ import annotations

from PySide6.QtCore import QPoint
from PySide6.QtCore import QRect
from PySide6.QtCore import QRectF
from PySide6.QtCore import QSize
from PySide6.QtCore import QThreadPool
from PySide6.QtCore import QTimer
from PySide6.QtCore import QUrl
from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor
from PySide6.QtGui import QDrag
from PySide6.QtGui import QGuiApplication
from PySide6.QtGui import QPainter
from PySide6.QtGui import QPixmap
from PySide6.QtGui import QTransform
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QFileDialog
from PySide6.QtWidgets import QFrame
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QLineEdit
from PySide6.QtWidgets import QMessageBox
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QWidget
from PySide6.QtWidgets import QWidgetAction
from app_version import APP_NAME
from async_images import ImageCache
from capture_files import CopyFilesJob
from confirm_clear import ClearConfirmOverlay
from confirm_clear import DeleteConfirmOverlay
from confirm_clear import confirmation_active
from content_grid import IconItem
from content_resolver import ContentResolver
from content_stack import StackTile
from contextlib import contextmanager
from dock_geometry import dock_hover_region
from dock_geometry import reveal_rect
from drag_artwork import drag_pixmap
from drag_probe import is_content_mime
from flickdock_icons import app_icon
from flickdock_motion import MotionShadow
from flickdock_motion import Spring
from fractions import Fraction
from native_ui_common import SHELF_HEADER_BUTTON_GAP_PX
from native_ui_common import SHELF_HEADER_BUTTON_SIZE_PX
from native_ui_common import SHELF_HEADER_RIGHT_MARGIN_PX
from overview_ui import ContentsPopup
from owned_files import delete_owned_copy
from owned_files import owned_copy_spec
from pathlib import Path
from platform_native import lower_shelf_after_reveal
from platform_native import raise_shelf_for_reveal
from platform_native import remove_host_window_shadow
from platform_native import shelf_window_flags
from reference_widgets import ToolbarButton
from selection_archive import ZipArchiveJob
from shelf_content import ContentRepository
from shelf_drop_zone import DropZone
from shelf_file_tools import unique_path
from shelf_ui_common import AnimatedMenu
from shelf_ui_common import CARD_H
from shelf_ui_common import CARD_W
from shelf_ui_common import EXPANDED_H
from shelf_ui_common import EXPANDED_W
from shelf_ui_common import MODES
from shelf_ui_common import OVERVIEW_SECTION_OFFSETS_PX
from shelf_ui_common import SHAKE_DISMISS_SECONDS
from shelf_ui_common import set_ui_style
from shelf_ui_common import style_menu
from surface_morph import SurfaceMorph
from surface_morph import SurfaceSnapshot
from sync_files import DiskWork
from sync_files import directory_size
from transfer_visuals import TransferIconLabel
from transfer_visuals import TransferRateTracker
from uuid import uuid4
import math
import os
import re
import shelf_ui_common
import shutil
import subprocess
import sys
import time
import ui_theme


class ShelfWindow(QWidget):
    def __init__(self, manager: "ShelfManager", number: int, data: dict | None = None):
        super().__init__()
        self.manager = manager
        self.number = number
        data = data or {}
        self.content = ContentRepository(data.get("texts"),data.get("metadata"))
        from file_metadata import FileMetadataCache
        self.file_metadata=FileMetadataCache(self);self.content.file_metadata=self.file_metadata
        self._missing_refs={};self._missing_timer=QTimer(self);self._missing_timer.setSingleShot(True)
        self._missing_timer.setInterval(500);self._missing_timer.timeout.connect(self._confirm_missing_refs)
        self.file_metadata.ready.connect(self._file_information_ready)
        self.transfer_rates=TransferRateTracker()
        self.image_cache=ImageCache(self)
        self.content.thumbnail_provider=self.image_cache.pixmap
        self._transition_preparing=False
        self._pending_image_paths=set()
        self._image_timer=QTimer(self);self._image_timer.setSingleShot(True)
        self._image_timer.timeout.connect(self._flush_images)
        self.image_cache.ready.connect(self._image_ready)
        refs = data.get("permanent", [])
        self.files = {"permanent": [ref for ref in refs if isinstance(ref, str)] if isinstance(refs, list) else []}

        self.preview_cache = {}
        self.preview_playing = False
        self.pinned = bool(data.get("pinned", False))
        self.last_activity = time.monotonic()
        self.dragging = False
        self.drag_origin: QPoint | None = None
        self.visible_state = "hidden"
        self.handle: DockHandle | None = None
        self.popup: ContentsPopup | None = None
        self.archive_job=None
        self._file_choosing=False;self._picker_return_until=0.0
        self._archive_choosing=False
        self._archive_refs=()
        self._archive_epoch=0
        self.copy_jobs=set()
        self._directory_jobs={};self._directory_sizes={}
        self._copy_pending={};self._delete_jobs=set()
        self._transfer_timer=QTimer(self);self._transfer_timer.setInterval(80)
        self._transfer_timer.timeout.connect(self._repaint_transfers)
        self._copy_epoch=0
        self.active_mode = "permanent"
        self._rename_mode = "permanent"
        self.setWindowFlags(shelf_window_flags())
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAcceptDrops(True)
        self.setMinimumSize(CARD_W + 28, CARD_H + 27)
        self.resize(CARD_W + 28, CARD_H + 27)
        self._build_ui()
        self.resolver=ContentResolver(self.content,lambda:self.manager.settings.capture_dir,lambda ref:ref in self.files['permanent'],on_ready=self._resolved_content,parent=self)
        self.refresh()
        QTimer.singleShot(0, self._resume_pending_content)
        pos = data.get("pos")
        if isinstance(pos, list) and len(pos) == 2:
            self.move(int(pos[0]), int(pos[1]))

    def _build_ui(self):
        self.setWindowTitle(APP_NAME)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.presentation_kind = "manual"
        self._menu_open = False
        self.incoming_drop = False
        self._intake_preserve_view=False
        self._intake_epoch = 0
        self._intake_stage = 'idle'
        self._intake_timer=QTimer(self)
        self._intake_timer.setInterval(16)
        self._intake_timer.timeout.connect(self._advance_intake)
        self._intake_tile=None
        self.active_drag = None
        self.expanded = False
        self.opened_at = 0.0
        self._dock_target = QPoint()
        self.card = QFrame(self)
        self.card.setObjectName("shelfCard")
        self.card.setGeometry(14, 7, CARD_W, CARD_H)
        set_ui_style(self.card, "QFrame#shelfCard {background:#ffffff;border:0;border-radius:24px;}")
        # The native host remains transparent; this is the card's rounded
        # shadow, confined to the reserved animation surface margins.
        self.surface_effect = MotionShadow(self.card, alpha=42,
            cache_while=lambda:self.expanded and self.presentation._timer.isActive() and not self.incoming_drop and not self.preview_playing)
        self.surface_effect.setBlurRadius(13)
        self.surface_effect.setOffset(0, 4)
        self.card.setGraphicsEffect(self.surface_effect)
        self.presentation = Spring(self, {"sx": 1, "sy": 1, "alpha": 1, "dx": 0, "dy": 0}, self._apply_presentation, motion=self.manager.motion)
        self.presentation.settled.connect(lambda:getattr(self.manager,'_present_update_notice',lambda:None)())
        self._morph_host_active=False
        self.geometry_morph=SurfaceMorph(self,self.geometry(),self._apply_geometry,motion=self.manager.motion,prepare=self._prepare_morph,settle=self._settle_morph)
        self._explicit_hiding = False
        self._native_revealed = False
        logo = QLabel(self.card)
        self.brand_logo=logo
        logo.setGeometry(16, 10, 28, 28)
        logo.setPixmap(app_icon().pixmap(QSize(28,28),self.devicePixelRatioF()))
        logo.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        logo.show()
        self.brand_title = QLabel(APP_NAME, self.card)
        self.brand_title.setGeometry(16, 12, 96, 24)
        set_ui_style(self.brand_title, "color:#282d35;font:600 13px 'HarmonyOS Sans SC';background:transparent;")
        self.brand_title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.shelf_label = QLabel("", self.card)
        self.shelf_label.setGeometry(56, 34, 144, 16)
        set_ui_style(self.shelf_label, "color:#7c8796;font:11px 'HarmonyOS Sans SC';background:transparent;")
        self.shelf_label.hide()
        self.shelf_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.menu_button = ToolbarButton("more", self.card, motion=self.manager.motion)
        self.menu_button.setGeometry(CARD_W - 80, 17, 30, 30)
        self.menu_button.clicked.connect(lambda: self.show_menu())
        self.close_button = ToolbarButton("dock", self.card, motion=self.manager.motion)
        self.close_button.setGeometry(CARD_W - 44, 17, 30, 30)
        self.close_button.clicked.connect(lambda: self.hide_animated(explicit=True))
        self.pin_button=ToolbarButton("pin-filled" if self.pinned else "pin",self.card,motion=self.manager.motion)
        self.pin_button.setCheckable(True); self.pin_button.setChecked(self.pinned)
        self.pin_button.clicked.connect(self.toggle_pin)
        self.pin_button.setToolTip("取消钉住" if self.pinned else "钉住搁架")
        self.pin_button.setAccessibleName(self.pin_button.toolTip())
        from reference_widgets import LanButton
        self.lan_button = LanButton('nearby', self.card, motion=self.manager.motion)
        self.lan_button.setToolTip('文件互传')
        self.lan_button.setAccessibleName('选择局域网或远程传输')
        self.lan_button.clicked.connect(self.show_transfer_menu)
        from transfer_entry import TransferEntryRouter
        self.transfer_entry=TransferEntryRouter(self,self.lan_button,lambda mode,paths:self.manager.queue_transfer_paths(mode,paths))
        from reference_widgets import AvatarButton
        self.account_button = AvatarButton('user', self.card, motion=self.manager.motion)
        self.account_button.setToolTip('账号与设置')
        self.account_button.setAccessibleName('打开账号与设置')
        self.account_button.clicked.connect(lambda:self.manager.show_settings(category='账号与设备'))
        self.expand_button = ToolbarButton("expand", self.card, motion=self.manager.motion)
        self.expand_button.setToolTip("展开内容")
        self.expand_button.clicked.connect(self.toggle_contents)
        self.zones = {mode: DropZone(self, mode) for mode in MODES}
        self.zones["permanent"].setGeometry(1, 49, 330, 268)
        self.status_label = QLabel(self.card)
        self.status_label.setGeometry(30, 242, CARD_W - 60, 18)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_ui_style(self.status_label, "color:#8490a0;font:11px 'HarmonyOS Sans SC';background:transparent;")
        self.rename_edit = QLineEdit(self.card)
        set_ui_style(self.rename_edit, "QLineEdit {background:white;color:#313741;border:1px solid #72aff3;border-radius:8px;padding:3px 6px;font:11px 'HarmonyOS Sans SC';}")
        self.rename_edit.returnPressed.connect(self.commit_rename)
        self.rename_edit.editingFinished.connect(self.commit_rename)
        self.rename_edit.hide()
        for button in (self.pin_button,self.expand_button,self.menu_button,self.close_button):
            button.setProperty('flatToolbar',True)
            button.setParent(self)
        for button in (self.lan_button,self.account_button):
            button.setProperty('flatToolbar',True)
            button.setParent(self)
        from reference_widgets import css,label,button
        css(self.card,'QFrame#shelfCard{background:#1C1C1E;border:1px solid rgba(255,255,255,20);border-radius:10px;}',
            'QFrame#shelfCard{background:#FFFFFF;border:1px solid rgba(0,0,0,23);border-radius:10px;}')
        if sys.platform=='darwin':
            css(self.card,'QFrame#shelfCard{background:#1C1C1E;border:1px solid rgba(255,255,255,20);border-radius:16px;}',
                'QFrame#shelfCard{background:#FFFFFF;border:1px solid rgba(0,0,0,23);border-radius:16px;}')
        self.header_line=QFrame(self.card);self.header_line.setGeometry(1,48,CARD_W-2,1)
        self.footer_line=QFrame(self.card);self.footer_line.setGeometry(1,269,CARD_W-2,1)
        for line in (self.header_line,self.footer_line):css(line,'background:rgba(255,255,255,15);border:0;','background:rgba(0,0,0,15);border:0;')
        self.account_caption=label('未登录',10,500,'secondary',self.card);self.account_caption.setGeometry(40,CARD_H-38,47,22)
        self.lan_caption=button('互传',parent=self.card);self.lan_caption.setGeometry(113,CARD_H-40,90,26);self.lan_caption.clicked.connect(self.show_transfer_menu)
        self.traffic_controls=QFrame(self.card);self.traffic_controls.setGeometry(16,18,56,12)
        for index,tint in enumerate(('#FF5F56','#FFBD2E','#27C93F')):
            control=QPushButton(self.traffic_controls);control.setGeometry(index*20,0,12,12);control.setStyleSheet(f'background:{tint};border:0;border-radius:6px;')
            control.clicked.connect((lambda:self.hide_animated(explicit=True)) if index==0 else self.show_contents if index==1 else self.pin_button.click)
        self._layout_header(CARD_W)

        from ui_transitions import ToolbarEntrance
        self._toolbar_entrance=ToolbarEntrance(self,lambda:[self.close_button,self.menu_button,self.expand_button,self.pin_button])

    def _visible_header_controls(self):
        if self.presentation_kind=='drag' and not self.expanded:return ()
        return (self.pin_button,self.expand_button,self.menu_button,self.close_button)

    def _layout_header(self,width):
        expanded=self.expanded
        self.brand_title.hide()
        self.brand_logo.setVisible(not expanded)
        center=QRectF(self.card.geometry()).center();effect=self.surface_effect
        transform=QTransform();transform.translate(center.x()+effect.dx,center.y()+effect.dy)
        transform.scale(effect.sx,effect.sy);transform.translate(-center.x(),-center.y())
        buttons=self._visible_header_controls()
        for item in (self.pin_button,self.expand_button,self.menu_button,self.close_button):
            if item not in buttons:item.hide()
        for index,item in enumerate(buttons):
            size=SHELF_HEADER_BUTTON_SIZE_PX;gap=SHELF_HEADER_BUTTON_GAP_PX
            x=width-SHELF_HEADER_RIGHT_MARGIN_PX-len(buttons)*(size+gap)+index*(size+gap)+gap
            point=transform.map(QRectF(self.card.x()+x,self.card.y()+8,size,size).center())
            item.setFixedSize(size,size);item.move(round(point.x()-size/2),round(point.y()-size/2));item.set_presentation_scale(effect.sx,effect.sy)
            item.show();item.raise_()
        self.account_button.hide()
        item=self.lan_button
        point=transform.map(QRectF(self.card.x()+10,self.card.y()+CARD_H-39,112,32).center())
        item.setFixedSize(112,32);item.move(round(point.x()-56),round(point.y()-16));item.set_presentation_scale(effect.sx,effect.sy)
        item.setVisible(not expanded);item.raise_()
        for attr in ('account_caption','lan_caption','header_line','footer_line','traffic_controls'):
            item=getattr(self,attr,None)
            if item is not None:item.setVisible(not expanded and attr in ('header_line','footer_line'))
        zone=self.zones.get('permanent')
        if zone:
            x,y=242,230
            zone.caption.move(x,y);zone.count_arrow.move(x+49,y+10)
            zone.clipboard_button.move(x-42,y);zone.clipboard_button.setVisible(not expanded)
            zone.capacity_button.move(x-78,y);zone.capacity_button.setVisible(not expanded)
            zone.hint.move(40,188)

    def show_cloud_menu(self):
        if confirmation_active(self):
            return
        self.touch()
        menu = AnimatedMenu(self)
        style_menu(menu)
        from app_settings import SettingsToggleSwitch
        from reference_widgets import label as source_label,css
        menu.setFixedWidth(304)
        for label, key in (("上传本机搁架到云端", "sync_upload_enabled"),
                           ("接收云端与其他设备内容", "sync_receive_enabled")):
            action=QWidgetAction(menu);action.setText(label)
            action.setCheckable(True)
            action.setChecked(getattr(self.manager.settings, key))
            holder=QWidget(menu);line=QHBoxLayout(holder);line.setContentsMargins(14,8,14,8);line.setSpacing(12)
            text=source_label(label,12,400,parent=holder);line.addWidget(text,1)
            switch=SettingsToggleSwitch('',holder);switch.dark=ui_theme.current_theme()=='dark'
            switch.setAccessibleName(label);switch.setFixedSize(44,32);switch.setChecked(action.isChecked())
            line.addWidget(switch)
            action.setDefaultWidget(holder);menu.addAction(action)
            switch.toggled.connect(action.setChecked);action.toggled.connect(switch.setChecked)
            action.toggled.connect(lambda checked, name=key, item=action:
                                   self._set_sync_direction(name, checked, item))
        self._menu_open = True
        try:
            anchor=self.menu_button
            menu.exec(anchor.mapToGlobal(QPoint(0, anchor.height()+6)))
        finally:
            self._menu_open = False
            menu.deleteLater()
            self.touch()

    def _set_sync_direction(self, name, checked, action):
        try:
            self.manager.settings.update(**{name: checked})
        except (OSError, ValueError) as error:
            blocked = action.blockSignals(True)
            action.setChecked(getattr(self.manager.settings, name))
            action.blockSignals(blocked)
            self.status_label.setText(str(error) or '云同步设置未能保存')

    def toggle_pin(self):
        self.pinned=self.pin_button.isChecked()
        self.pin_button.set_glyph('pin-filled' if self.pinned else 'pin')
        self.pin_button.setToolTip('取消钉住' if self.pinned else '钉住搁架')
        self.pin_button.setAccessibleName(self.pin_button.toolTip())
        self.touch(); self.manager.save()

    def _prepare_morph(self,origin,target):
        with self._surface_transaction():
            self._prepare_morph_frame(origin,target)

    @contextmanager
    def _surface_transaction(self):
        """Publish one complete presentation state, never a layout checkpoint."""
        enabled=self.updatesEnabled()
        if enabled:self.setUpdatesEnabled(False)
        try:yield
        finally:
            if enabled:
                self.setUpdatesEnabled(True)
                self.update()

    def _expansion_rect(self,current):
        area=(QGuiApplication.screenAt(current.center()) or QGuiApplication.primaryScreen()).availableGeometry()
        width,height=min(EXPANDED_W,area.width()-38),min(EXPANDED_H,area.height()-38)
        right=current.x()+current.width()
        x=max(area.left()+4,min(right-width-28,area.right()-width-20))
        y=max(area.top()+4,min(current.y(),area.bottom()-height-30))
        return QRect(x,y,width+28,height+27)

    def _reserved_surface_rect(self,origin,target=None):
        target=self._expansion_rect(origin) if target is None else target
        pad_x=max(16,math.ceil(abs(target.width()-origin.width())*.02)+8)
        pad_y=max(12,math.ceil(abs(target.height()-origin.height())*.02)+8)
        bounds=origin.united(target).adjusted(-pad_x,-pad_y,pad_x,pad_y)
        if sys.platform=='darwin':
            # AppKit constrains an oversized/above-menu-bar native canvas on
            # ordering. Reserve within the work area before showing it, so
            # Cocoa cannot rebase the canvas during a later morph.
            screen=QGuiApplication.screenAt(origin.center()) or QGuiApplication.primaryScreen()
            bounds=bounds.intersected(screen.availableGeometry())
        step=Fraction(self.devicePixelRatioF()).limit_denominator(8).denominator
        x=origin.x()+math.floor((bounds.x()-origin.x())/step)*step
        y=origin.y()+math.floor((bounds.y()-origin.y())/step)*step
        return QRect(x,y,bounds.right()-x+1,bounds.bottom()-y+1)

    def _prepare_morph_frame(self,origin,target):
        if sys.platform=='darwin':self.clearMask()
        snapshot=getattr(self,'_surface_snapshot',None)
        if snapshot is None:
            snapshot=self._surface_snapshot=SurfaceSnapshot(self)
        controls=(self.lan_button,self.pin_button,self.expand_button,self.menu_button,self.close_button)
        source=getattr(self,'_morph_from_image',None)
        if source is None: source=snapshot.capture_current(self.card,controls)
        self._morph_from_image=None
        self._morph_host_active=True
        # A single compositor owns both the moving and resting surface. Align
        # the host rebase to its physical pixel phase and keep it at completion.
        required=origin.united(target).adjusted(-16,-12,16,12)
        if not self.geometry().contains(required):
            self.setGeometry(self._reserved_surface_rect(origin,target))
        # The native host AND live widget tree stay at fixed endpoint bounds.
        # Only the compositor's rectangle moves. Moving the large effect source
        # on every tick invalidates Qt's backing store and causes resize stalls.
        self.card.setGeometry(target.x()-self.x()+14,target.y()-self.y()+7,target.width()-28,target.height()-27)
        self._layout_header(self.card.width())
        if self.expanded:self.popup.prepare_transition()
        target_image=snapshot.capture_live(self.card,controls)
        from surface_elements import capture as capture_elements
        source_elements=getattr(self,'_morph_from_elements',())
        self._morph_from_elements=()
        target_elements=capture_elements(self,target_image)
        snapshot.begin(source,target_image,origin,present=False,target_rect=target,
                       source_elements=source_elements,target_elements=target_elements)
        if self.expanded:
            for name,offset in OVERVIEW_SECTION_OFFSETS_PX:
                widget=getattr(self.popup,name)
                if widget.isVisible():
                    snapshot.target_sections.append((QRectF(widget.mapTo(self.card,QPoint()),widget.size()),QPoint(*offset)))
        self.surface_effect.begin_surface_morph(snapshot)
        self._apply_geometry(origin)

    def _settle_morph(self,rect):
        snapshot=getattr(self,'_surface_snapshot',None)
        self.surface_effect.end_surface_morph()
        if snapshot is not None: snapshot.finish()
        self._morph_from_image=None
        self._morph_from_elements=()
        self._morph_host_active=False
        self._transition_preparing=False
        if not self.geometry().contains(rect):self.setGeometry(rect)
        self.card.setGeometry(rect.x()-self.x()+14,rect.y()-self.y()+7,rect.width()-28,rect.height()-27)
        self._layout_header(self.card.width())
        if self.expanded and self.popup:
            # Remote metadata or a new drop may have completed while the live
            # tree was hidden behind its transition image.
            self.popup.rebuild()
            # Transfer notifications arriving during the morph intentionally
            # skip layout work. Reconcile the bottom strip at the settled frame
            # so the All view does not require a category round trip.
            self.popup.update_transfer_status()
            self.popup.refresh_cloud_status()
        self.card.show()
        if sys.platform=='darwin':
            from PySide6.QtGui import QRegion
            # Keep the reserved host for animation, but exclude its invisible
            # area from Cocoa hit testing while the compact shelf is idle.
            self.setMask(QRegion(self.card.geometry().adjusted(-10,-10,10,10)))
        if self.expanded and self.popup:
            QTimer.singleShot(16,self.popup.resume_content)
            QTimer.singleShot(16,lambda:getattr(self.manager,'_present_update_notice',lambda:None)())
        if self._pending_image_paths: self._image_timer.start(16)

    def content_transition_active(self):
        return (self._transition_preparing or self.geometry_morph.active
                or self._morph_host_active or self.visible_state=='hiding')

    def _image_ready(self,path):
        self._pending_image_paths.add(path)
        if not self.content_transition_active() and not self._image_timer.isActive():
            self._image_timer.start(16)

    def _flush_images(self):
        if self.content_transition_active(): return
        self.surface_effect.invalidate_source()
        ready=self._pending_image_paths;self._pending_image_paths=set()
        refs=[ref for ref in self.files['permanent'] if ref in ready or
              self.content.metadata.get(ref,{}).get('preview_image') in ready]
        if not refs:return
        self.zones['permanent'].refresh()
        if self.popup and self.popup.isVisible():
            for ref in refs:self.popup.update_item_icon(ref)
            if getattr(self.popup.preview,'_group_refs',()):
                chosen=self.popup.preview._group_refs
                self.popup.preview._group_refs=()
                self.popup.preview.show_group(chosen)

    def _apply_geometry(self,rect):
        snapshot=getattr(self,'_surface_snapshot',None)
        if self._morph_host_active and snapshot is not None and snapshot.active:
            # The live tree stays at its endpoint size under one compositor.
            snapshot.set_frame(rect,self.geometry_morph.progress)
            self._place_morph_controls(snapshot,rect)
            return
        if not self.geometry().contains(rect):self.setGeometry(rect)
        self.card.setGeometry(rect.x()-self.x()+14,rect.y()-self.y()+7,rect.width()-28,rect.height()-27)
        self._layout_header(self.card.width())

    def _place_morph_controls(self,snapshot,rect):
        transform=snapshot.presentation_transform(rect)
        buttons=self._visible_header_controls()
        for item in (self.pin_button,self.expand_button,self.menu_button,self.close_button):
            if item not in buttons:item.hide()
        size=32
        width=rect.width()-28
        for index,button in enumerate(buttons):
            x=width-16-len(buttons)*(size+6)+index*(size+6)+6
            center=transform.map(QRectF(rect.x()+14+x-self.x(),rect.y()+15-self.y(),size,size).center())
            button.move(round(center.x()-size/2),round(center.y()-size/2))
            button.set_presentation_scale(self.surface_effect.sx,self.surface_effect.sy)
            button.raise_()
        if not self.expanded:
            button=self.lan_button
            center=transform.map(QRectF(rect.x()+24-self.x(),rect.y()+7+CARD_H-39-self.y(),112,32).center())
            button.move(round(center.x()-56),round(center.y()-16))
            button.set_presentation_scale(self.surface_effect.sx,self.surface_effect.sy);button.raise_()

    def _capture_morph_source(self):
        if not self.geometry_morph.motion: return
        snapshot=getattr(self,'_surface_snapshot',None)
        if snapshot is None: snapshot=self._surface_snapshot=SurfaceSnapshot(self)
        self._morph_from_image=snapshot.capture_current(self.card,(self.lan_button,self.pin_button,self.expand_button,self.menu_button,self.close_button))
        from surface_elements import capture as capture_elements
        self._morph_from_elements=(snapshot.current_elements() if snapshot.active else
                                   capture_elements(self,self._morph_from_image))

    def visual_geometry(self): return QRect(self.geometry_morph.current)

    def item_pixmap(self,ref,size=90,stack=1,dpr=None):
        image=self.preview_cache.get(ref)
        if image is not None and not image.isNull():
            ratio=dpr or self.devicePixelRatioF()
            pm=QPixmap(round(size*ratio),round(size*ratio)); pm.setDevicePixelRatio(ratio); pm.fill(Qt.GlobalColor.transparent)
            painter=QPainter(pm); painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            scale=min(size*.84/image.width(),size*.84/image.height()); w,h=image.width()*scale,image.height()*scale
            painter.drawImage(QRectF((size-w)/2,(size-h)/2,w,h),image); painter.end()
            return pm
        return self.content.item_pixmap(ref,size,stack,dpr=dpr)

    def _resume_pending_content(self):
        changed = False
        for ref in tuple(self.files['permanent']):
            meta = self.content.metadata.get(ref, {})
            if not meta.get('loading') or ref in self.resolver.pending_refs:
                continue
            source = meta.get('source_url', '')
            if isinstance(source, str) and source:
                self.resolver.resolve(ref, source)
            else:
                # Older interrupted captures did not keep their data URI.
                # Leave a usable item rather than an endless loading state.
                meta.update(loading=False, error='上次解析未完成，请重新拖入此内容')
                changed = True
        if changed:
            self.refresh()
            self.manager.save()

    def _resolved_content(self,ref,result):
        if ref not in self.files['permanent']: return
        result=dict(result); result['loading']=False
        metadata=self.content.metadata.get(ref,{})
        if metadata.get('preview_only'):
            result['preview_resolved_source']=metadata.get('source_url','')
        if self.content.is_text(ref) and self.content.metadata.get(ref,{}).get('preview_only'):
            if result.get('path'):result['preview_image']=result.pop('path')
        from shortcut_preview import SUFFIXES
        if Path(ref).suffix.lower() in SUFFIXES and not self.content.is_text(ref):
            if result.get('path'):result['preview_image']=result.pop('path')
            result['title']=self.content.title(ref)
        if str(result.get('source_url','')).startswith('data:'):
            original=result.pop('source_url')
            result.pop('final_url',None)
            self.content.metadata.get(ref,{}).pop('source_url',None)
            self.content.metadata.get(ref,{}).pop('final_url',None)
            if result.get('error'): self.content.texts[ref]=original
        path=result.get('path')
        if path and Path(path).exists():
            refs=self.files['permanent']; index=refs.index(ref)
            if path in refs: refs.remove(ref)
            else: refs[index]=path
            self.content.metadata[path]=result
            if self.popup:
                if ref in self.popup.selected: self.popup.selected.remove(ref); self.popup.selected.add(path)
                if self.popup.primary_selection==ref: self.popup.primary_selection=path
        else: self.content.metadata.setdefault(ref,{}).update(result)
        self.refresh(); self.manager.save(); self.manager.update_handles()

    def queue_link_previews(self,refs=None):
        from shortcut_preview import shortcut_details
        for ref in refs or self.files['permanent']:
            meta=self.content.metadata.setdefault(ref,{})
            if ref in self.resolver.pending_refs:continue
            if meta.get('preview_image') and Path(meta['preview_image']).is_file():continue
            source=self.content.text(ref)
            if not (isinstance(source,str) and re.fullmatch(r'https?://\S+',source,re.IGNORECASE)):
                details=shortcut_details(ref);source=(details or {}).get('url')
            if not source or meta.get('error'):continue
            if meta.get('preview_resolved_source')==source:continue
            meta.update(loading=True,source_url=source,preview_only=True)
            self.resolver.resolve(ref,source)

    def confirm_clear_all(self):
        count=len(self.display_refs())
        if not count or confirmation_active(self): return False
        self.geometry_morph.complete()
        dialog=ClearConfirmOverlay(self.card,count,font_family=shelf_ui_common.FONT_FAMILY,theme=self.manager.settings.theme)
        self._clear_confirmation=dialog
        self.touch()
        try: confirmed=dialog.exec()
        finally:self._clear_confirmation=None;dialog.deleteLater();self.touch()
        if confirmed: self.remove_all('permanent')
        return confirmed

    def confirm_delete(self):
        refs=self.display_refs()
        if not refs or confirmation_active(self):return False
        selected=tuple(ref for ref in refs if self.popup is not None and ref in self.popup.selected)
        self.geometry_morph.complete()
        dialog=DeleteConfirmOverlay(self.card,len(refs),len(selected),font_family=shelf_ui_common.FONT_FAMILY,theme=self.manager.settings.theme)
        self._clear_confirmation=dialog;self.touch()
        try:
            confirmed=dialog.exec();scope=dialog.scope
        finally:self._clear_confirmation=None;dialog.deleteLater();self.touch()
        if confirmed:
            if scope=='all':self.remove_all('permanent')
            else:
                # Freeze the chosen identities before entering the event loop;
                # live sync may remove items or add new ones while it is open.
                current=set(self.display_refs())
                for ref in selected:
                    if ref in current:self.remove_path('permanent',ref)
        return confirmed

    def _apply_presentation(self, values):
        self.surface_effect.set_motion(sx=values["sx"], sy=values["sy"], dx=values["dx"], dy=values["dy"])
        snapshot=getattr(self,'_surface_snapshot',None)
        if snapshot is not None and snapshot.active:
            snapshot.refresh_presentation()
            self._place_morph_controls(snapshot,self.visual_geometry())
        elif hasattr(self,'pin_button'):self._layout_header(self.card.width())
        confirmation=getattr(self,'_clear_confirmation',None)
        if confirmation is not None and confirmation.isVisible():confirmation.raise_()
        self.setWindowOpacity(max(0.0, min(1.0, values["alpha"])))

    def toggle_contents(self):
        if self.property('flickdockConfirmationActive'): return
        if self.expanded:
            self.collapse_contents()
        else:
            mode = self.active_mode if self.files[self.active_mode] else next((m for m in MODES if self.files[m]), self.active_mode)
            self.show_contents(mode)

    def touch(self):
        self.last_activity = time.monotonic()

    def has_files(self) -> bool:
        return bool(self.display_refs())

    def display_refs(self):
        identities={self.content.metadata.get(ref,{}).get('_cloud_item_id') for ref in self.files['permanent']}
        refs=list(self.files['permanent'])+[ref for ref in self.content.pending
            if ref.removeprefix('flickdock:transfer:') not in identities]
        by_identity={};aliases={}
        for ref in refs:
            identity=(ref.removeprefix('flickdock:transfer:') if self.content.is_pending(ref)
                      else self.content.metadata.get(ref,{}).get('_cloud_item_id'))
            key=('cloud',identity) if identity else ('local',ref)
            # A conflict fork may temporarily retain its source's metadata.
            # Distinct local refs must never collapse into one painted item.
            if key in by_identity:key=('local',ref)
            by_identity[key]=ref;aliases[('local',ref)]=key
        order=list(dict.fromkeys(aliases.get(key,key) for key in getattr(self,'_display_order',())
                                 if aliases.get(key,key) in by_identity))
        known=set(order);order.extend(key for key in by_identity if key not in known)
        self._display_order=order
        return [by_identity[key] for key in order]

    def _repaint_transfers(self):
        if not self.content.transfers:self._transfer_timer.stop()
        if not self.isVisible() or self.content_transition_active():return
        for zone in self.zones.values():zone.icon.update()
        if self.popup and self.popup.isVisible():
            for item in self.popup.rows_body.findChildren(QWidget):
                if isinstance(item,(IconItem,TransferIconLabel)):item.update()
            self.popup.preview.update_transfer_status()

    def transfers_updated(self):
        self.surface_effect.invalidate_source()
        self.transfer_rates.observe(self.content.transfers)
        if self.content.transfers and not self._transfer_timer.isActive():self._transfer_timer.start()
        self.expand_button.setEnabled(True)
        self.zones['permanent'].refresh()
        if self.popup and self.popup.isVisible() and not self.content_transition_active():
            # Pending names/types may arrive after the first progress hint.
            titles={ref:state.get('name') for ref,state in self.content.transfers.items() if state.get('remote')}
            if (getattr(self.popup,'_pending_content',None)!=self.content.pending or
                    getattr(self.popup,'_transfer_titles',None)!=titles):
                self.popup._pending_content={r:dict(v) for r,v in self.content.pending.items()}
                self.popup._transfer_titles=titles
                self.popup.rebuild()
            self.popup.update_transfer_status()
            self.popup.refresh_cloud_status()
        elif self.popup:self.popup._content_dirty=True
        self.manager.update_handles()

    def cancel_transfer(self,ref):
        job=self._copy_pending.get(ref)
        if job:job.cancel();return
        state=self.content.transfers.get(ref,{})
        if self._receiving_transfer(state):
            self.manager.sync.cancel_download(state.get('id'));return
        if hasattr(self.manager,'sync'):self.manager.sync.cancel_upload(state.get('id') or ref)

    def _receiving_transfer(self,state):
        """Identify an owned receive job regardless of its current UI phase."""
        sync=getattr(self.manager,'sync',None)
        return bool(sync and state.get('remote') and
                    state.get('id') in getattr(sync,'download_transfers',{}))

    def _delete_owned(self,ref):
        metadata=self.content.metadata.get(ref,{})
        owned_ref=metadata.get('text_file',ref) if self.content.is_text(ref) else ref
        spec=owned_copy_spec(owned_ref,metadata,self.manager.settings.capture_dir)
        if spec is None:return
        if self.popup:
            if self.popup.preview.current_path==ref:
                self.popup.preview.clear_preview()
            elif self.popup.preview.media.path==ref:self.popup.preview.media.release_source()
        self.image_cache.forget(ref)
        job=DiskWork(delete_owned_copy,spec);self._delete_jobs.add(job)
        def done(result,error):
            self._delete_jobs.discard(job)
            if error and not self.manager._quitting:self.status_label.setText('本地副本未能删除，请检查文件是否被其他程序占用。')
        job.signals.done.connect(done);QThreadPool.globalInstance().start(job)

    def refresh(self):
        from reference_widgets import refresh_tree
        self.surface_effect.invalidate_source()
        theme=self.manager.settings.theme
        if getattr(self,'_tree_theme',None)!=theme:
            refresh_tree(self);self._tree_theme=theme
        self.refresh_account_identity()
        for mode in MODES:
            self.files[mode] = [p for p in self.files[mode] if self._display_ref_exists(p)]
            self.zones[mode].refresh()
        count = len(self.display_refs())
        self.status_label.setText('正在复制内容…' if self.copy_jobs else '')
        self.expand_button.setEnabled(True)
        if self.popup and self.popup.isVisible():
            if not self.content_transition_active():self.popup.rebuild()
            else:self.popup._content_dirty=True
        self.update()

    def refresh_account_identity(self):
        account=getattr(self.manager,'account',None)
        if hasattr(self,'account_caption'):
            self.account_caption.setText(str(account.account.get('username',''))[:8] if account and account.signed_in else '未登录')
            self.account_button.initial=(str(account.account.get('username',''))[:1].upper() if account and account.signed_in else '')
        if self.popup:
            self.popup.account_nav.set_profile(str(account.account.get('username','')) if account and account.signed_in else '',bool(account and account.signed_in))

    def request_directory_size(self,ref):
        now=time.monotonic();cached=self._directory_sizes.get(ref)
        if ref not in self._directory_jobs and (not cached or now-cached[0]>30):
            job=DiskWork(directory_size,ref);self._directory_jobs[ref]=job
            def done(result,error):
                self._directory_jobs.pop(ref,None)
                if error or ref not in self.files['permanent']:return
                self._directory_sizes[ref]=(time.monotonic(),result['logical_size'])
                self.content.metadata.setdefault(ref,{}).update(result)
                if not self.content_transition_active():
                    self.zones['permanent'].refresh()
                    if self.popup and self.popup.isVisible():self.popup.rebuild()
            job.signals.done.connect(done);QThreadPool.globalInstance().start(job)
        return cached[1] if cached else None

    def _display_ref_exists(self,ref):
        if self.content.is_text(ref) or self.content.is_pending(ref):return self.content.exists(ref)
        self.file_metadata.request(ref)
        return True

    def _confirm_missing_refs(self):
        now=time.monotonic();future=[]
        for ref,candidate in tuple(self._missing_refs.items()):
            if ref not in self.files['permanent']:self._missing_refs.pop(ref,None)
            elif not candidate['confirming']:
                if candidate['confirm_after']>now:future.append(candidate['confirm_after'])
                else:candidate['confirming']=True;self.file_metadata.refresh(ref)
        if future:self._missing_timer.start(max(1,int((min(future)-time.monotonic())*1000)+1))

    def _file_information_ready(self,ref):
        if ref not in self.files['permanent']:self._missing_refs.pop(ref,None);return
        self.surface_effect.invalidate_source()
        info=self.file_metadata.request(ref) or {}
        if not info.get('exists') and info.get('errno') in (2,20) and info.get('local_volume') is True:
            candidate=self._missing_refs.get(ref)
            if candidate and candidate['confirming'] and info['checked_at']>candidate['checked_at']:
                self._missing_refs.pop(ref,None)
                for mode in MODES:self.files[mode]=[value for value in self.files[mode] if value!=ref]
                self.refresh();return
            if candidate is None:
                self._missing_refs[ref]=dict(confirming=False,checked_at=info['checked_at'],confirm_after=time.monotonic()+.5)
                if not self._missing_timer.isActive():self._missing_timer.start()
        else:self._missing_refs.pop(ref,None)
        self.zones['permanent'].icon.update()
        if self.popup and self.popup.isVisible():
            from reference_widgets import item_detail
            item=self.popup._item_widgets.get(ref)
            if item is not None:
                if hasattr(item,'refresh_content'):item.refresh_content()
                item.update()
            if self.popup.preview.current_path==ref and self.popup.preview.info.text().startswith('正在读取'):
                self.popup.preview.info.setText(item_detail(self,ref))
            if self.popup.sort_key=='size' and self.popup.sort_direction:
                if not hasattr(self.popup,'_size_sort_timer'):
                    timer=QTimer(self.popup);timer.setSingleShot(True)
                    timer.timeout.connect(lambda:self.popup.rebuild() if self.popup.isVisible() and self.popup.sort_key=='size' else None)
                    self.popup._size_sort_timer=timer
                if not self.popup._size_sort_timer.isActive():self.popup._size_sort_timer.start(32)

    def add_paths(self, mode, refs, *, defer_cloud=False):
        for ref in refs:
            info=None
            if not self.content.is_text(ref):
                try:
                    info=Path(ref).stat()
                except OSError:pass
            value = ref if self.content.is_text(ref) else str(Path(ref).resolve())
            if self.content.exists(value):
                if info is not None:self.file_metadata.prime(value,info)
                if value in self.files[mode]: self.files[mode].remove(value)
                self.content.metadata.setdefault(value,{}).setdefault('added_at',time.time())
                self.files[mode].append(value)
        self.refresh()
        self.touch()
        if defer_cloud and hasattr(self.manager, 'save_after_intake'):
            self.manager.save_after_intake()
        else:
            self.manager.save()
        self.manager.update_handles()

    def import_paths(self,mode,refs,on_committed=None,on_failed=None,*,_checked=False,force_copy=False,_capacity_warning=False):
        """Cloud capacity never prevents keeping a file on the local shelf."""
        refs=list(refs)
        if not _checked and hasattr(self.manager,'check_cloud_capacity'):
            epoch=self._copy_epoch
            def checked(insufficient):
                if self.manager._quitting:return
                if insufficient is None or epoch!=self._copy_epoch:
                    if on_failed:on_failed('搁架已切换，本次放入已取消。')
                    return
                self.import_paths(mode,refs,on_committed,on_failed,_checked=True,force_copy=force_copy,_capacity_warning=insufficient)
            self.manager.check_cloud_capacity(refs,checked);return
        def committed(paths):
            if on_committed:on_committed()
            if _capacity_warning:
                sync=getattr(self.manager,'sync',None)
                if sync:
                    for ref in paths:
                        if sync.sync_status_for_ref(ref)!='synced':sync.cancel_upload(ref)
                self.manager.show_cloud_capacity_notice(paths);return
            offer=getattr(self.manager,'offer_large_intake',None)
            if offer:offer(paths)
        if not force_copy and self.manager.settings.capture_mode!='copy':
            self.add_paths(mode,refs,defer_cloud=on_committed is not None)
            committed(refs)
            return
        directory=self.manager.settings.capture_dir
        owned=os.path.normcase(os.path.abspath(directory))
        external=[]
        for ref in refs:
            if self.content.is_text(ref):continue
            key=os.path.normcase(os.path.abspath(ref))
            if key!=owned and not key.startswith(owned+os.sep):external.append(ref)
        if not external:
            self.add_paths(mode,refs,defer_cloud=on_committed is not None)
            committed(refs)
            return
        job=CopyFilesJob(external,directory,parent=self)
        epoch=self._copy_epoch
        self.copy_jobs.add(job)
        placeholders={}
        for source in external:
            ref='flickdock:transfer:'+uuid4().hex
            placeholders[source]=ref;self._copy_pending[ref]=job
            self.content.pending[ref]=dict(name=Path(source).name,kind='file')
            self.content.transfers[ref]=dict(phase='copying',received=0,total=0)
        def progress(value):
            ref=placeholders.get(value.get('source'))
            if ref in self.content.transfers:self.content.transfers[ref].update(received=value['received'],total=value['total'])
            self.transfer_rates.observe(self.content.transfers)
            if self.popup and self.popup.isVisible():self.popup.update_transfer_status()
        job.progress.connect(progress)
        self.transfers_updated();self.refresh()
        def done(paths=None,error=''):
            self.copy_jobs.discard(job)
            for ref in placeholders.values():
                self._copy_pending.pop(ref,None);self.content.pending.pop(ref,None);self.content.transfers.pop(ref,None)
            if paths and epoch==self._copy_epoch and not self.manager._quitting:
                replacement=dict(zip(external,paths))
                for old,new in replacement.items():
                    self.content.metadata[new]={'capture_mode':'copy','original_name':Path(old).name,'owned_root':str(Path(directory).absolute())}
                captured=[replacement.get(ref,ref) for ref in refs]
                self.add_paths(mode,captured,
                               defer_cloud=on_committed is not None)
                committed(captured)
                if not on_committed:self.zones[mode].pulse('drop')
            else:
                for path in paths or []:
                    self.content.metadata[path]={'capture_mode':'copy','owned_root':str(Path(directory).absolute())}
                    self._delete_owned(path);self.content.metadata.pop(path,None)
                if on_failed and epoch==self._copy_epoch:on_failed()
            if error and self.popup:self.popup.preview.info.setText('复制失败：'+error)
            self.status_label.setText('复制失败：'+error if error else '' if not self.copy_jobs else '正在复制内容…')
            job.deleteLater();self.touch();self.transfers_updated();self.refresh();self.manager.update_handles()
        job.finished.connect(lambda paths:done(paths))
        job.failed.connect(lambda error:done(error=error))
        job.cancelled.connect(done)
        self.status_label.setText('正在复制内容…')
        job.start();self.touch()

    def remove_path(self, mode: str, path: str, *, keep_local=False):
        transfer_state=self.content.transfers.get(path,{})
        downloading=self._receiving_transfer(transfer_state)
        if path in self.files[mode]:self.files[mode].remove(path)
        self.content.pending.pop(path,None)
        if downloading:self.manager.sync.cancel_download(transfer_state.get('id'),remove=True)
        if self.content.is_pending(path):
            if not downloading:self.cancel_transfer(path)
            return
        if not downloading and hasattr(self.manager,'sync'):self.manager.sync.cancel_upload(path,remove=True)
        if self.popup:
            if self.popup.preview.current_path==path:self.popup.preview.clear_preview()
            elif self.popup.preview.media.path==path:self.popup.preview.media.release_source()
        self.preview_cache.pop(path,None)
        self.image_cache.forget(path)
        if self.archive_job:
            related=path in self._archive_refs
            if not related and not self.content.is_text(path):
                removed=Path(path).resolve()
                for source in self.archive_job.paths:
                    source=Path(source).resolve()
                    if source==removed or source in removed.parents or removed in source.parents:
                        related=True;break
            if related:
                self._archive_epoch+=1;self.archive_job.cancel()
        self.resolver.cancel(path)
        if not keep_local:self._delete_owned(path)
        self.refresh()
        self.touch()
        self.manager.save()
        self.manager.update_handles()

    def remove_all(self, mode: str):
        self._cancel_intake()
        self._copy_epoch+=1
        for job in getattr(self,'_intake_limit_jobs',()):job.cancel.set()
        for job in tuple(self.copy_jobs):job.cancel()
        if self.popup:
            self.popup.preview.clear_preview()
        self.preview_cache.clear()
        self.image_cache.clear()
        self._pending_image_paths.clear()
        self._archive_epoch+=1
        if self.archive_job:self.archive_job.cancel()
        self.resolver.cancel_all()
        cancelled_downloads=set()
        for ref in tuple(self.files[mode]):
            if hasattr(self.manager,'sync'):
                state=self.content.transfers.get(ref,{})
                if self._receiving_transfer(state):
                    self.manager.sync.cancel_download(state['id'],remove=True)
                    cancelled_downloads.add(state['id'])
                else:self.manager.sync.cancel_upload(ref,remove=True)
            self._delete_owned(ref)
        for ref,state in tuple(self.content.transfers.items()):
            if (state.get('remote') and hasattr(self.manager,'sync')
                    and state.get('id') not in cancelled_downloads):
                if self._receiving_transfer(state):self.manager.sync.cancel_download(state.get('id'),remove=True)
                else:self.manager.sync.cancel_upload(state.get('id'))
        self.files[mode].clear()
        self.refresh()
        self.touch()
        self.manager.save()
        self.manager.update_handles()

    def receive_mime(self, mode, mime, *, source='drop'):
        if (self.manager.outgoing_drag or self.dragging or confirmation_active(self)
                or getattr(self.manager,'settings_open',lambda:False)()):
            return False
        remote=self.resolver.candidates(mime)
        if source=='paste' and mime.hasText() and not mime.hasUrls() and not mime.hasImage():
            remote=[]
        jobs=[]
        if remote and not any(url.isLocalFile() for url in mime.urls()) and not mime.hasImage():
            paths=[]
            for url in remote:
                placeholder='正在解析图片 · '+str(time.monotonic_ns()) if url.startswith('data:') else url
                ref=self.content.add_text(placeholder)
                # Persist the original source until resolution completes so an
                # interrupted download can resume after restarting the app.
                self.content.metadata[ref]={'source_url':url, 'loading':True, 'title':'网页图片' if url.startswith('data:') else url}
                paths.append(ref); jobs.append((ref,url))
        else:
            paths = self.content.capture_mime(mime, self.manager.settings.capture_dir)
        if not paths:
            return False
        if source=='paste':
            try:
                for ref in paths:
                    if self.content.is_text(ref) and not jobs:self.content.materialize_pasted_text(ref,self.manager.settings.capture_dir)
            except OSError as error:
                self.status_label.setText('文字文件未能保存：'+str(error));return False
        self._cancel_intake()
        epoch=self._intake_epoch
        self._intake_preserve_view=source=='paste' or self.expanded
        self.incoming_drop = True
        self.manager.just_dropped = time.monotonic()
        if self.visible_state=='hiding' or not self.isVisible():
            # On some native drags the mouse-up poll precedes OLE DropEvent.
            # That poll starts the transient shelf's hide animation. Retarget
            # it as soon as this real content drop is accepted, before a copy
            # job can leave its landing hidden for hundreds of milliseconds.
            self._show_at(self._dock_target,self.presentation_kind,QPoint())
        self.import_paths(mode,paths,
            on_committed=lambda:self._committed_intake(mode,epoch),
            on_failed=lambda:self._abort_intake(epoch))
        for ref,url in jobs: self.resolver.resolve(ref,url)
        return True

    def _cancel_intake(self):
        self._intake_epoch+=1
        self.incoming_drop=False
        self._intake_stage='idle'
        self._intake_preserve_view=False
        self._intake_timer.stop()
        for zone in self.zones.values():zone.icon.cancel_pulse()
        if self._intake_tile is not None:
            self._intake_tile.cancel_pulse();self._intake_tile.hide()

    def _abort_intake(self,epoch):
        if epoch==self._intake_epoch:self._cancel_intake()

    def _committed_intake(self,mode,epoch):
        """One accepted drop: commit, visible landing, then tuck once."""
        if epoch!=self._intake_epoch or not self.incoming_drop:return
        if not self.isVisible():
            self._show_at(self._dock_target,self.presentation_kind,QPoint())
        self._intake_mode=mode
        if self.expanded and not self.pinned and not self._intake_preserve_view:
            self._intake_stage='folding'
            self._intake_timer.start()
            # This completion belongs to the intake, not the interruptible
            # geometry animation. Reopening the overview cannot lose it.
            self.collapse_contents()
        else:self._land_intake()

    def _advance_intake(self):
        if not self.incoming_drop:
            self._intake_timer.stop();return
        if self._intake_stage=='folding' and not self.geometry_morph.active:
            self._land_intake()
        elif self._intake_stage=='waiting_copy' and not self.copy_jobs:
            self._land_intake()

    def _land_intake(self):
        epoch=self._intake_epoch;mode=self._intake_mode
        if not self.incoming_drop:return
        self._intake_timer.stop();self._intake_stage='landing'
        # A dock reveal may still be fading in when native Drop arrives.
        # Land on a fully visible surface; keep the content animation live.
        if self.visible_state=='hiding':self._show_at(self._dock_target,self.presentation_kind,QPoint())
        self.presentation.jump({'sx':1,'sy':1,'alpha':1,'dx':0,'dy':0})
        tile=self.zones[mode].icon
        if self.expanded:
            # A pinned/explicitly reopened overview keeps its selected preview.
            # Landing feedback is a transparent, non-interactive overlay.
            if self._intake_tile is None:self._intake_tile=StackTile(self,motion=self.manager.motion)
            tile=self._intake_tile
            tile.set_stack(self.content,self.files[mode],self.preview_cache)
            visual=self.visual_geometry()
            tile.setGeometry(visual.center().x()-self.x()-80,
                             visual.center().y()-self.y()-75,160,150)
            tile.show();tile.raise_()
        if self.manager.motion:
            tile.replay_landing()
            self.presentation.kick({'sx':-.45,'sy':.65})
        tile.pulse('drop',lambda:self._finish_intake(epoch))

    def accepts_content_drop(self, event):
        if self.manager.outgoing_drag or self.dragging or confirmation_active(self):
            return False
        source = event.source()
        if isinstance(source, QWidget) and (source is self or self.isAncestorOf(source)):
            return False
        return is_content_mime(event.mimeData())

    def _highlight_content_drop(self, active):
        zone = self.zones["permanent"]
        zone.drop_highlight = active
        zone.icon.set_hover(active)
        zone._apply_style()

    def dragEnterEvent(self, event):
        if self.accepts_content_drop(event):
            self.active_mode = "permanent"
            self._highlight_content_drop(True)
            self.touch()
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self.accepts_content_drop(event):
            self.touch()
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._highlight_content_drop(False)
        event.accept()

    def dropEvent(self, event):
        self._highlight_content_drop(False)
        if self.accepts_content_drop(event) and self.receive_mime("permanent", event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def _finish_intake(self,epoch=None):
        if not self.incoming_drop or (epoch is not None and epoch!=self._intake_epoch):
            return
        if self._intake_tile is not None:self._intake_tile.hide()
        if self.copy_jobs:
            # A later, faster copy must not tuck the shelf while an earlier
            # accepted import is still running in the background.
            self._intake_stage='waiting_copy';self._intake_timer.start();return
        self.incoming_drop = False
        self._intake_stage='idle';self._intake_timer.stop()
        # Completing a drop commits the tuck just like the hide control. The
        # pointer is still over the drop zone and must leave before reopening.
        if not self.pinned and self.presentation_kind!='dock' and not self._intake_preserve_view:self.hide_animated(explicit=True)
        else:self.touch()
        self._intake_preserve_view=False

    def drag_files(self, mode, paths):
        paths=[ref for ref in paths if not self.content.is_pending(ref)]
        if not paths:return Qt.DropAction.IgnoreAction
        paths = [p for p in paths if self.content.exists(p)]
        if not paths:
            return Qt.DropAction.IgnoreAction
        self.manager.drag_gate.reset_session()
        self.manager.outgoing_drag = True
        self.dragging = True
        self.touch()
        drag = QDrag(self)
        self.active_drag = drag
        mime = self.content.mime_for(paths)

        drag.setMimeData(mime)
        artwork=drag_pixmap(self.content,paths,preview_cache=self.preview_cache,dpr=self.devicePixelRatioF())
        drag.setPixmap(artwork)
        drag.setHotSpot(QPoint(round(artwork.deviceIndependentSize().width()/2),46))
        self.zones[mode].pulse("lift")
        result = Qt.DropAction.IgnoreAction
        try:
            result = drag.exec(Qt.DropAction.CopyAction, Qt.DropAction.CopyAction)
        finally:
            # QDrag owns native drag visuals. Dispose them on every exit,
            # including Escape/cancel and exceptions, not at app shutdown.
            drag.setPixmap(QPixmap())
            self.active_drag = None
            drag.deleteLater()
            self.dragging = False
            self.manager.outgoing_drag = False
            self.touch()
        self.complete_drag(mode, paths, result)
        return result

    def complete_drag(self, mode, paths, result):
        # A drag exports content; removal is always an explicit user action.
        self.zones["permanent"].pulse("return" if result == Qt.DropAction.IgnoreAction else "out")
        if self.popup:
            self.popup.rebuild()
        self.touch()

    def _show_at(self, target, kind, offset, reserve=None):
        was_visible = self.isVisible()
        was_hiding=self.visible_state=='hiding'
        self.manager.activate_shelf(self)
        self.presentation_kind = kind
        self.visible_state = "floating"
        self._explicit_hiding = False
        self.opened_at = time.monotonic()
        self._dock_target = QPoint(target)
        width, height = getattr(self, "_expanded_size", (EXPANDED_W, EXPANDED_H)) if self.expanded else (CARD_W, CARD_H)
        rect = QRect(target.x(), target.y(), width + 28, height + 27)
        if not was_visible:
            # Reserve the small and expanded envelopes before the native
            # surface becomes visible. Expansion then never moves its HWND
            # while Windows still holds the old compact backing-store image.
            self.setGeometry(self._reserved_surface_rect(rect,reserve))
        if not was_visible:
            # A new reveal owns fresh repository pixels, never an interrupted
            # compositor or a removal frame retained while the widget hid.
            self.geometry_morph.jump(rect)
            self.zones['permanent'].refresh()
            self.surface_effect.invalidate_source()
        elif self.geometry_morph.current != rect:
            self.geometry_morph.animate(rect)
        if not was_visible:
            self.presentation.jump({"sx": .95 if self.manager.motion else 1, "sy": .91 if self.manager.motion else 1,
                                    "alpha": 0, "dx": offset.x() if self.manager.motion else 0, "dy": offset.y() if self.manager.motion else 0})
        # A visible surface may still be morphing from a different width.
        # Keep controls on its current bounds until each geometry frame lands.
        if self._morph_host_active:
            self._apply_geometry(self.visual_geometry())
        else:
            self._layout_header(self.card.width())
        self.show()
        if kind=='dock' and not self.expanded and self.manager.motion and (not was_visible or was_hiding):
            self._toolbar_entrance.reveal(reset=not was_visible)
        remove_host_window_shadow(self)
        self._raise_for_reveal()
        self.touch()
        from ui_transitions import SHELF_REVEAL_RESPONSE_SECONDS,SHELF_REVEAL_DAMPING
        self.presentation.animate({"sx": 1, "sy": 1, "alpha": 1, "dx": 0, "dy": 0},response=SHELF_REVEAL_RESPONSE_SECONDS,damping=SHELF_REVEAL_DAMPING)
        self.manager.update_handles()

    def _raise_for_reveal(self):
        """Order a summoned shelf over the drag source without activation.

        The native level is released on hide or when a secondary panel takes
        focus, so another application's later activation can come forward.
        """
        if sys.platform!='darwin':self.raise_()
        self._native_revealed = raise_shelf_for_reveal(self) or self._native_revealed

    def _release_reveal_level(self):
        if self._native_revealed:
            self._native_revealed = not lower_shelf_after_reveal(self)

    def show_transient(self, cursor=None, reason="manual", velocity=None, reserve=None):
        cursor = cursor or QCursor.pos()
        screen = QGuiApplication.screenAt(cursor) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        width,height=(self.visual_geometry().width(),self.visual_geometry().height()) if self.expanded else (CARD_W+28,CARD_H+27)
        x = max(area.left() + 4, min(cursor.x() + 22, area.right() - width - 4))
        y = max(area.top() + 4, min(cursor.y() - 130, area.bottom() - height - 4))
        offset=QPoint(0,14)
        if velocity is not None:
            vx,vy=velocity
            offset=QPoint(round(max(-24,min(24,-vx*.012))),round(max(-18,min(18,-vy*.012))))
        self._show_at(QPoint(x,y),reason,offset,reserve=reserve)
        if reason=='drag':
            self._shake_outside_since=time.monotonic()
        if velocity is not None and self.manager.motion:
            # The newly revealed shelf settles against the actual flick,
            # with a small elastic impulse, rather than a generic fade alone.
            self.presentation.kick({'sx':.55,'sy':-.7})

    def _dock_edge(self):
        values=getattr(self.manager.edge,'values',None)
        return values()['dock_edge'] if callable(values) else self.manager.settings.dock_edge

    def _dock_target_rect(self):
        area=self.manager.edge.screen_area()
        size=QSize(*(getattr(self,'_expanded_size',(EXPANDED_W,EXPANDED_H)) if self.expanded else (CARD_W,CARD_H)))+QSize(28,27)
        return reveal_rect(area,self.manager.edge.geometry(),size,self._dock_edge())

    def shake_idle_expired(self,position,now,inside):
        if inside:self._shake_outside_since=None;return False
        if getattr(self,'_shake_outside_since',None) is None:self._shake_outside_since=now
        return now-self._shake_outside_since>=SHAKE_DISMISS_SECONDS

    def reveal_from_dock(self,*,expanded=False):
        if self.manager.edge_suppressed: return
        offsets={'right':QPoint(30,0),'left':QPoint(-30,0),'top':QPoint(0,-24),'bottom':QPoint(0,24)}
        if self.isVisible():
            if self.presentation_kind=='drag':
                self.manager.auto_shown=None
                self.hide_immediately()
            else:
                # A hover return interrupts the dock spring at its current anchor.
                self._show_at(self.visual_geometry().topLeft(),'dock',QPoint())
                return
        compact=self._dock_target_rect()
        rect=self._expansion_rect(compact) if expanded else compact
        with self._surface_transaction():
            if expanded:
                # Build the final view before ordering the native surface in.
                # There is no compact entrance followed by a second morph.
                self.setGeometry(self._reserved_surface_rect(compact,rect))
                self._show_contents('permanent',rect,animate=False)
            self._show_at(rect.topLeft(),'dock',offsets[self._dock_edge()],reserve=compact if expanded else None)

    def hide_animated(self, explicit=True):
        if not self.isVisible() or self.visible_state == "hiding" or (self.pinned and not explicit):
            return
        self._dismiss_clipboard_preview()
        self._cancel_intake()
        self._explicit_hiding = explicit
        if explicit:
            self.manager.edge_suppressed = True
            self.manager.suppressed_region = QRect(self.manager.edge.geometry())
        # Closing midway keeps the currently composited frame. Settling the
        # live tree here would replace it with a resized endpoint for one frame.
        self.geometry_morph.stop(settle=False)
        if self.popup: self.popup.preview.media.stop()
        self.visible_state = "hiding"
        if self.presentation_kind=='dock' and not self.expanded and self.manager.motion:self._toolbar_entrance.conceal()
        self.manager.update_handles()
        docked = self.presentation_kind == "dock"
        if self.manager.motion:
            self.presentation.kick({"sx": .35, "sy": -.55})
        def finish():
            if self.visible_state == "hiding":
                self.hide_immediately()
                self.manager.update_handles()
                self.manager.save()
        offset={'right':QPoint(32,0),'left':QPoint(-32,0),'top':QPoint(0,-24),'bottom':QPoint(0,24)}[self._dock_edge()] if docked else QPoint(0,8)
        self.presentation.animate({"dx": offset.x() if self.manager.motion else 0,
                                   "dy": offset.y() if self.manager.motion else 0,
                                   "sx": .96 if self.manager.motion else 1, "sy": .92 if self.manager.motion else 1, "alpha": 0},
                                  finish, response=.17, damping=.84)

    def hide_immediately(self):
        self._dismiss_clipboard_preview()
        self._cancel_intake()
        self.presentation.stop()
        self.geometry_morph.stop(settle=False)
        previous=self.visual_geometry()
        self.hide()
        # Restoring Spaces while a Cocoa window is still visible can move it
        # before the fade finishes. Restore native order only after ordering out.
        self._release_reveal_level()
        self.visible_state = "hidden"
        self.incoming_drop = False
        self.expanded = False
        self.expand_button.set_glyph("expand")
        self.status_label.show()
        self.zones["permanent"].show()
        if self.popup:
            self.popup.hide()
        self.geometry_morph.jump(QRect(previous.x(), previous.y(), CARD_W + 28, CARD_H + 27))
        self.presentation.jump({"sx": 1, "sy": 1, "alpha": 1, "dx": 0, "dy": 0})

    def hover_region_contains(self,point):
        if not self.isVisible(): return False
        dialog=getattr(self.manager,'clipboard_preview_dialog',None)
        if dialog is not None and dialog.isVisible() and dialog.frameGeometry().contains(point):return True
        capacities=[self.zones['permanent'].capacity_button]
        if self.popup is not None:capacities.append(self.popup.capacity_button)
        if any(button.popover.isVisible() and button.popover.frameGeometry().contains(point) for button in capacities):return True
        rect=self.visual_geometry()
        # Spring motion moves the visual bounds under a stationary pointer.
        # Keep the edge-to-shelf hover bridge tied to the settled target.
        if self.presentation_kind=='dock':
            trigger=self.manager.edge.geometry()
            # The expanded card can be anchored differently from its compact
            # edge target (notably at the macOS menu-bar edge). Include both
            # envelopes so neither a native enter/leave event nor a morph
            # frame can turn one stationary pointer into an outside pointer.
            from PySide6.QtGui import QRegion
            rect=dock_hover_region(rect.adjusted(-6,-6,6,6),trigger,self._dock_edge())|QRegion(self._dock_target_rect().adjusted(-6,-6,6,6))
        return rect.contains(point)

    def intake_blocks_overview(self):
        return self.presentation_kind=='drag' and (self.incoming_drop or self.visible_state=='hiding')

    def show_contents(self, mode="permanent", target=None, *, animate=True):
        if self.intake_blocks_overview():return
        with self._surface_transaction():
            self._show_contents(mode,target,animate=animate)
        if self.isVisible():
            self._raise_for_reveal()

    def _show_contents(self,mode,target,*,animate=True):
        if self.property('flickdockConfirmationActive'): return
        if not self.expanded:self._dismiss_clipboard_preview()
        if self.popup is not None:self.popup.preview.clear_preview()
        if animate:self._capture_morph_source()
        self._transition_preparing=animate
        self.visible_state = "floating"
        if self.popup is None:
            self.popup = ContentsPopup(self)
        self.refresh_account_identity()
        self.popup.reset_for_open()
        self.expanded = True
        self.zones["permanent"].hide()
        self.status_label.hide()
        self.expand_button.set_glyph("collapse")
        current=self.visual_geometry()
        default_target=self._expansion_rect(current)
        destination=default_target if target is None else QRect(target)
        width,height=destination.width()-28,destination.height()-27
        self._expanded_size = (width, height)
        self.popup.setGeometry(0, 0, width, height)
        self.popup.show()
        presentation={"sx": 1, "sy": 1, "alpha": 1, "dx": 0, "dy": 0}
        if animate:
            from ui_transitions import OVERVIEW_OPEN_DURATION_MS
            self.geometry_morph.animate(destination,duration=OVERVIEW_OPEN_DURATION_MS,elastic=True)
            self.presentation.animate(presentation,response=.19,damping=1)
        else:
            self.geometry_morph.jump(destination)
            self.presentation.jump(presentation)
        self.touch()

    def collapse_contents(self, target=None, finished=None):
        with self._surface_transaction():
            self._collapse_contents(target,finished)

    def _collapse_contents(self,target,finished):
        if self.property('flickdockConfirmationActive'): return
        if not self.expanded:
            return
        self._dismiss_clipboard_preview()
        self.popup.preview.media.stop()
        self._capture_morph_source()
        self._transition_preparing=True
        self.expanded = False
        if self.popup:
            self.popup.hide()
        self.zones["permanent"].show()
        self.status_label.show()
        self.expand_button.set_glyph("expand")
        right = self.geometry_morph.current.x() + self.geometry_morph.current.width()
        x = right - CARD_W - 28
        self._dock_target.setX(x)
        destination=QRect(x,self.visual_geometry().y(),CARD_W+28,CARD_H+27) if target is None else QRect(target)
        self.geometry_morph.animate(destination,finished=finished)
        self.touch()

    def picker_hold_active(self,now=None):
        return self._file_choosing or (time.monotonic() if now is None else now)<self._picker_return_until

    def _dismiss_clipboard_preview(self):
        dialog=getattr(self.manager,'clipboard_preview_dialog',None)
        if dialog is not None and dialog.isVisible():dialog.close()

    def pick_files(self, mode: str):
        if self._file_choosing:return
        self._file_choosing=True
        try:
            paths, _ = QFileDialog.getOpenFileNames(self, "选择文件")
            if paths:
                self.import_paths(mode, paths)
                self.zones[mode].pulse("drop")
        finally:
            # Native pickers can deactivate Qt and deliver the final pointer
            # event after their nested loop returns. Keep the existing mode
            # and allow a comfortable move back to either shelf surface.
            self._file_choosing=False;self._picker_return_until=time.monotonic()+1.5
            self.touch()

    def paste_clipboard(self, mode):
        return self.receive_mime(mode,QApplication.clipboard().mimeData(),source='paste')

    def copy_items(self, refs):
        refs=[ref for ref in refs if not self.content.is_pending(ref)]
        if not refs:return
        if not refs:
            return
        QApplication.clipboard().setMimeData(self.content.mime_for(refs))
        self.touch()

    def real_paths(self, mode):
        return [ref for ref in self.files[mode] if not self.content.is_text(ref) and self.content.exists(ref)]

    def show_transfer_menu(self):
        self.manager.show_transfer_dialog()

    def show_menu(self, mode=None, pos=None):
        if confirmation_active(self):return
        self.touch()
        menu = AnimatedMenu(self)
        style_menu(menu)
        self._menu_open = True
        self._add_mode_actions(menu, "permanent")
        menu.addSeparator()
        menu.addAction("云端同步…", self.show_cloud_menu)
        menu.addAction("设置…", self.manager.show_settings)
        menu.addAction("收起到屏幕边缘", lambda: self.hide_animated(explicit=True))
        try:
            menu.exec(pos or self.menu_button.mapToGlobal(QPoint(0, 39)))
        finally:
            self._menu_open = False
            menu.deleteLater()
            self.touch()

    def _add_mode_actions(self, menu, mode="permanent"):
        refs = self.files["permanent"]
        menu.addAction("添加文件…", lambda: self.pick_files("permanent"))
        menu.addAction("粘贴剪贴板内容", lambda: self.paste_clipboard("permanent"))
        if not refs:
            return
        menu.addSeparator()
        if not self.expanded:menu.addAction("内容总览", self.show_contents)
        only_text = all(self.content.is_text(ref) for ref in refs)
        menu.addAction("复制文字" if only_text else "复制内容", lambda: self.copy_files("permanent"))
        menu.addAction('打包全部为 ZIP…',lambda:self.compress_refs(list(self.files['permanent'])))
        menu.addSeparator()
        menu.addAction("清空搁架…", self.confirm_clear_all)

    def open_path(self, ref):
        if self.content.is_pending(ref):return
        if self.content.is_text(ref):
            if self.content.metadata.get(ref,{}).get('pasted_text'):
                path=self.content.materialize_pasted_text(ref,self.manager.settings.capture_dir)
                from PySide6.QtGui import QDesktopServices
                QDesktopServices.openUrl(QUrl.fromLocalFile(path));return
            if self.content.category(ref)=='links':
                from PySide6.QtGui import QDesktopServices
                QDesktopServices.openUrl(QUrl(self.content.text(ref))); return
            self.copy_items([ref])
            if self.popup:
                self.popup.preview.copy_button.set_glyph("check")
                QTimer.singleShot(850, lambda: self.popup.preview.copy_button.set_glyph("copy"))
        elif Path(ref).exists():
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(ref).resolve())))
        else:
            QMessageBox.warning(self, APP_NAME, f"找不到文件：\n{ref}")

    def reveal(self, mode):
        refs = self.real_paths(mode)
        if refs:
            subprocess.Popen(["explorer.exe", "/select,", refs[0]])

    def reveal_ref(self,ref):
        if not self.content.is_text(ref) and Path(ref).exists():
            subprocess.Popen(['explorer.exe','/select,',ref])

    def copy_files(self, mode):
        self.copy_items(self.files[mode])

    def compress(self, mode):
        refs=self.popup.chosen_refs() if self.expanded and self.popup else []
        self.compress_refs(refs or self.files[mode])

    def choose_zip_path(self):
        directory=self.manager.settings.capture_dir
        try:directory.mkdir(parents=True,exist_ok=True)
        except OSError as error:
            if self.popup:self.popup.preview.info.setText('无法打开保存位置：'+str(error))
            return ''
        dialog=QFileDialog(self,'将所选内容打包为 ZIP',str(directory/'文件打包.zip'))
        # Use Windows' own Save As dialog (Desktop/Quick access, full path,
        # localized system buttons and standard file selection feedback).
        dialog.setOption(QFileDialog.Option.DontConfirmOverwrite,True)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setNameFilter('ZIP 文件 (*.zip)');dialog.setDefaultSuffix('zip')
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        try:return dialog.selectedFiles()[0] if dialog.exec() and dialog.selectedFiles() else ''
        finally:dialog.deleteLater()

    def compress_refs(self,refs):
        refs=[ref for ref in refs if not self.content.is_pending(ref) and self.content.exists(ref)]
        if not refs or self.archive_job is not None or self._archive_choosing:return False
        self.geometry_morph.complete()
        self._archive_choosing=True
        if self.popup:
            self.popup.zip_button.setToolTip('选择保存位置…')
            self.popup.zip_button.setAccessibleName('选择保存位置…')
            self.popup.zip_button.setEnabled(False)
        try:chosen=self.choose_zip_path()
        finally:
            self._archive_choosing=False
            if self.popup:self.popup.update_selection_actions()
        if not chosen:return False
        paths=[ref for ref in refs if not self.content.is_text(ref)]
        texts=[(self.content.title(ref),self.content.text(ref)) for ref in refs if self.content.is_text(ref)]
        job=self.archive_job=ZipArchiveJob(paths,chosen,texts=texts,parent=self)
        self._archive_refs=tuple(refs)
        self._archive_epoch+=1;epoch=self._archive_epoch
        def done(path='',error=''):
            self.archive_job=None
            self._archive_refs=()
            published=path and not self.manager._quitting and epoch==self._archive_epoch
            if published:
                self.add_paths('permanent',[path])
                if self.popup and self.popup.isVisible():
                    self.popup.set_category('all')
                    self.popup.select_item(path)
                    self.popup.scroll.verticalScrollBar().setValue(0)
                    self.popup.preview.info.setText('已保存 · '+str(path))
            if self.popup:
                self.popup.update_selection_actions()
                if error:self.popup.preview.info.setText('打包失败：'+error)
            job.deleteLater();self.touch()
        job.finished.connect(lambda path:done(path));job.failed.connect(lambda error:done(error=error));job.cancelled.connect(done)
        if self.popup:self.popup.update_selection_actions()
        job.start();self.touch();return True

    def move_files(self, mode, refs=None):
        folder = QFileDialog.getExistingDirectory(self, "选择目标文件夹")
        if not folder:
            return
        try:
            for ref in list(self.files[mode] if refs is None else refs):
                if ref not in self.files[mode]:continue
                if self.content.is_text(ref):
                    continue
                source = Path(ref)
                target = unique_path(Path(folder) / source.name)
                if self.popup and self.popup.preview.media.path==ref:self.popup.preview.media.release_source()
                result=shutil.move(str(source),str(target))
                self.files[mode][self.files[mode].index(ref)]=result
                self.image_cache.forget(ref);self.preview_cache.pop(ref,None)
                if ref in self.content.metadata:self.content.metadata[result]=self.content.metadata.pop(ref)
                if self.popup:
                    if ref in self.popup.selected:self.popup.selected.remove(ref);self.popup.selected.add(result)
                    if self.popup.primary_selection==ref:self.popup.primary_selection=result
                    self.popup.preview.current_path=None
            self.refresh()
            self.manager.save()
        except Exception as ex:
            self.manager.save()
            QMessageBox.warning(self, APP_NAME, f"移动失败：{ex}")

    def start_rename(self, mode: str):
        if len(self.files[mode]) != 1 or self.content.is_text(self.files[mode][0]):
            return
        self.rename_ref(self.files[mode][0])

    def rename_ref(self,ref):
        if ref not in self.files['permanent'] or self.content.is_text(ref):return
        self._rename_mode='permanent';self._rename_ref=ref
        self.geometry_morph.complete()
        if self.expanded and self.popup:
            self.popup.select_item(ref)
        if not getattr(self,'_rename_dialog',None):
            from reference_widgets import RenameDialog
            self._rename_dialog=RenameDialog(self)
        dialog=self._rename_dialog
        from reference_widgets import refresh_tree
        refresh_tree(dialog)
        dialog.move(self.visual_geometry().center()-QPoint(dialog.width()//2,dialog.height()//2))
        self.rename_edit.setText(Path(ref).name)
        dialog.show();dialog.raise_();dialog.activateWindow()
        self.rename_edit.show()
        self.rename_edit.raise_()
        self.rename_edit.setFocus()
        self.rename_edit.setSelection(0,len(Path(ref).stem))
        self.touch()

    def commit_rename(self):
        ref=getattr(self,'_rename_ref',None)
        if not self.rename_edit.isVisible() or ref not in self.files['permanent']:
            return False
        new_name = self.rename_edit.text().strip()
        if not new_name or new_name in ('.','..') or new_name.endswith('.') or any(c in new_name for c in '<>:"/\\|?*') or any(ord(c)<32 for c in new_name):
            QMessageBox.warning(self,APP_NAME,'请输入有效的完整文件名。');return False
        src = Path(ref)
        target = src.with_name(new_name)
        if target == src:
            self.rename_edit.hide();return True
        if target.exists() and not (src.name.casefold()==target.name.casefold() and os.path.samefile(src,target)):
            QMessageBox.warning(self, APP_NAME, "目标文件名已经存在")
            return False
        try:
            if self.popup and self.popup.preview.media.path==ref:self.popup.preview.media.release_source()
            src.rename(target)
        except OSError as ex:
            QMessageBox.warning(self,APP_NAME,f'重命名失败：{ex}');return False
        self.rename_edit.hide()
        self.files['permanent']=[str(target) if old==ref else old for old in self.files['permanent']]
        if ref in self.content.metadata:self.content.metadata[str(target)]=self.content.metadata.pop(ref)
        self.image_cache.forget(ref);self.preview_cache.pop(ref,None)
        if self.popup:
            if ref in self.popup.selected:self.popup.selected.remove(ref);self.popup.selected.add(str(target))
            if self.popup.primary_selection==ref:self.popup.primary_selection=str(target)
            self.popup.preview.current_path=None
        self.refresh()
        self.manager.save()
        return True

    def mousePressEvent(self, event):
        self.touch()
        if event.button()==Qt.MouseButton.LeftButton:
            # Cocoa can deliver a toolbar-edge press to the frameless host.
            # Reserve the entire painted button target before title dragging.
            for button in (self.pin_button,self.expand_button,self.menu_button,self.close_button):
                if button.isVisible() and button.geometry().contains(event.position().toPoint()):
                    self.drag_origin=None
                    if button.isEnabled():
                        self._header_pressed_button=button;button.setDown(True)
                    event.accept();return
        local=self.card.mapFrom(self,event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and self.card.rect().contains(local) and local.y()<60:
            self.geometry_morph.stop()
            self.drag_origin = event.globalPosition().toPoint() - self.visual_geometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        self.touch()
        button=getattr(self,'_header_pressed_button',None)
        if button is not None:
            button.setDown(button.geometry().contains(event.position().toPoint()))
            event.accept();return
        if self.drag_origin and event.buttons() & Qt.MouseButton.LeftButton:
            target = event.globalPosition().toPoint() - self.drag_origin
            self.geometry_morph.jump(QRect(target, self.visual_geometry().size()))
            self.presentation_kind = "manual"
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        button=getattr(self,'_header_pressed_button',None)
        if button is not None:
            self._header_pressed_button=None;button.setDown(False)
            inside=button.isVisible() and button.isEnabled() and button.geometry().contains(event.position().toPoint())
            event.accept()
            if inside:button.click()
            return
        if self.drag_origin:
            self.drag_origin = None
            self.manager.save()
        self.touch()
        super().mouseReleaseEvent(event)

    def enterEvent(self, event):
        self.touch()
        super().enterEvent(event)

    def contextMenuEvent(self, event):
        self.show_menu(pos=event.globalPos())
        event.accept()

    def keyPressEvent(self, event):
        if confirmation_active(self):event.accept();return
        if event.key()==Qt.Key.Key_A and event.modifiers() & Qt.KeyboardModifier.ControlModifier and self.expanded and self.popup:
            refs=self.popup.visible_refs();self.popup.selected=set(refs)
            if refs:self.popup.select_item(refs[0])
            event.accept();return
        if event.key() == Qt.Key.Key_Escape:
            self.hide_animated(explicit=True)
            event.accept()
            return
        if event.key() == Qt.Key.Key_V and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.paste_clipboard(self.active_mode)
            event.accept()
            return
        if event.key() == Qt.Key.Key_C and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if self.expanded and self.popup:
                refs = [ref for ref in self.files[self.popup.mode] if ref in self.popup.selected]
                if not refs:
                    current = self.popup.preview.current_path
                    refs = [current] if current in self.files[self.popup.mode] else self.files[self.popup.mode]
            else:
                refs = self.files[self.active_mode]
            self.copy_items(refs)
            event.accept()
            return
        super().keyPressEvent(event)
