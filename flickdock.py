"""Application composition, utility windows and desktop interaction coordination."""
from __future__ import annotations

import sys

if __name__=='__main__' and '--video-worker' in sys.argv:
    from video_worker import run
    raise SystemExit(run())

if __name__=='__main__' and '--virtual-clipboard-worker' in sys.argv:
    from virtual_clipboard import run
    raise SystemExit(run(sys.argv[sys.argv.index('--virtual-clipboard-worker')+1]))

from PySide6.QtCore import QPoint
from PySide6.QtCore import QRect
from PySide6.QtCore import QThreadPool
from PySide6.QtCore import QTimer
from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor
from PySide6.QtGui import QDrag
from PySide6.QtGui import QGuiApplication
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QMessageBox
from PySide6.QtWidgets import QSystemTrayIcon
from account_panel import AccountPanel
from app_settings import SettingsDialog
from app_settings import SettingsStore
from app_version import APP_NAME
from app_version import APP_VERSION
from confirm_clear import confirmation_active
from dataclasses import replace
from dock_handle import DockHandle
from dock_preview import DockPreview
from drag_probe import ContentDragGate
from drag_probe import native_window_move_active
from flickdock_icons import app_icon
from flickdock_icons import menu_bar_icon
from global_hotkeys import HotkeyManager
from instance_guard import InstanceGuard
from lan_panel import LanDialog
from overview_ui import ContentsPopup
from pathlib import Path
from platform_native import data_directory
from platform_native import mouse_buttons_mask
from platform_native import native_input_available
from pointer_paste import PointerPasteController
from pointer_paste import pointer_over_shelf
from settings_edge_hint import SettingsEdgeHint
from shake_detector import ShakeDetector
from shake_detector import config_for_sensitivity
from shelf_content import ContentRepository
from shelf_content import merge_shelves
from shelf_ui_common import AnimatedMenu
from shelf_ui_common import CARD_H
from shelf_ui_common import CARD_W
from shelf_ui_common import EXPANDED_H
from shelf_ui_common import EXPANDED_W
from shelf_ui_common import IDLE_SECONDS
from shelf_ui_common import load_font
from shelf_ui_common import motion_enabled
from shelf_ui_common import style_menu
from shelf_window import ShelfWindow
from sync_adapter import ShelfAdapter
from sync_runtime import CloudSyncRuntime
from transfer_runtime import ManualTransferRuntime
from sync_files import DiskWork
from sync_files import oversized_intake
from update_controller import UpdateController
from update_notice import UpdateNotice
import json
import os
import shelf_ui_common
import shutil
import time
import ui_theme


BASE = data_directory(APP_NAME)


STATE_FILE = BASE / "shelves.json"


OLD_STATE_FILE = Path(os.getenv("APPDATA", str(Path.home()))) / "DoudouShelf" / "shelves.json"


class ShelfManager:
    def __init__(self, app, *, cocoa_smoke=False):
        self.app = app
        self.cocoa_smoke = bool(cocoa_smoke)
        self.motion = motion_enabled()
        self.settings = SettingsStore(BASE)
        if not self.cocoa_smoke and app.platformName() not in ('offscreen','minimal'):
            from runtime_cleanup import schedule_cleanup
            schedule_cleanup(BASE,APP_VERSION)
        self.startup_error = None
        if not self.cocoa_smoke:
            try:
                self.settings.reconcile_startup()
            except OSError:
                self.startup_error = "自启动位置未能更新。可在设置中关闭后重新开启自启动。"
        ui_theme.set_theme(self.settings.theme)
        ui_theme.apply_application(app, shelf_ui_common.FONT_FAMILY)
        self.settings_dialog = None
        self.settings_edge_hint=SettingsEdgeHint(self)
        self.lan_dialog = None
        self.video_dialog = None
        self.wan_dialog = None
        self.updates = UpdateController(APP_VERSION, BASE, settings=self.settings,
                                        quit_callback=self.quit, is_busy=self._update_busy)
        self.update_notice = None
        self.update_dialog = None
        self.updates.notification_shown.connect(self._show_update_notice)
        self.updates.state_changed.connect(self._update_notice_state_changed)
        self._dock_preview_values=None
        self._shake_preview_value=None
        self._quitting=False
        self.dock_preview=DockPreview()
        self.edge_suppressed = False
        self.suppressed_region = QRect()
        self.shelves = []
        self.active_shelf = None
        self.outgoing_drag = False
        self.just_dropped = 0.0
        self.was_down = False
        self._button_mask=0
        self._app_was_active = app.applicationState() == Qt.ApplicationState.ApplicationActive
        self.shake_detector=ShakeDetector(config_for_sensitivity(self.settings.shake_sensitivity))
        self._drag_probe_detector=ShakeDetector(replace(config_for_sensitivity(100),reversals=1))
        self._drag_content_ready=False
        self._content_gesture_at=None
        self.shake_anchor_x = 0
        self.shake_direction = 0
        self.shake_reversals = 0
        self.shake_last_change = 0.0
        self.drag_started_inside = False
        self.auto_shown = None
        self.edges={}
        self._refresh_displays()
        app.screenAdded.connect(self._refresh_displays);app.screenRemoved.connect(self._refresh_displays)
        app.primaryScreenChanged.connect(self._refresh_displays)
        from drop_bridge import DropBridge
        self.drop_bridge=DropBridge(self)
        self.drag_gate = ContentDragGate(self._content_type_ready,self._probe_drop,lambda:self.outgoing_drag)
        self.tray = QSystemTrayIcon(menu_bar_icon() if sys.platform=='darwin' else app_icon(), app)
        self.tray.setToolTip(f"FlickDock {APP_VERSION} · 文件搁架")
        menu = AnimatedMenu()
        style_menu(menu)
        menu.addAction("显示搁架", self.show_first)
        menu.addAction("粘贴到搁架", self.paste_new)
        menu.addAction("设置…", self.show_settings)
        menu.addAction("检查更新…", self.check_updates)
        if sys.platform=='win32':
            menu.addAction("修复网络访问…",self.repair_network_access)
        elif sys.platform=='darwin':menu.addAction('Mac 权限设置…',self.show_mac_permissions)
        menu.addSeparator()
        menu.addAction("退出 FlickDock", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.tray_activated)
        if not self.cocoa_smoke:
            self.tray.show()
        self.load()
        if not self.shelves:
            self._create_shelf({})
        if self.active_shelf is None:
            self.active_shelf = next((item for item in self.shelves if item.has_files()), self.shelves[0])
        if not self.cocoa_smoke:
            self.update_handles()
        self.cloud = CloudSyncRuntime(BASE, self.app, ShelfAdapter(self),
            cocoa_smoke=self.cocoa_smoke, clipboard_enabled=self._clipboard_runtime_enabled(),
            upload_enabled=self.settings.sync_upload_enabled, receive_enabled=self.settings.sync_receive_enabled)
        self.account = self.cloud.account
        self.sync_scheduler = self.cloud.scheduler
        self.sync = self.cloud.shelf
        self.events = self.cloud.events
        self.clipboard_sync = self.cloud.clipboard
        self.sync.capacity_exceeded.connect(self.show_cloud_capacity_notice)
        for zone in self.shelf.zones.values():zone.capacity_button.bind_sync(self.sync)
        self.sync.metadata_changed.connect(lambda ref:self.shelf.popup.preview.refresh_cloud_status()
            if self.shelf.popup is not None and self.shelf.popup.preview.current_path==ref else None)
        self._intake_sync_timer=QTimer(self.app)
        self._intake_sync_timer.setSingleShot(True)
        self._intake_sync_timer.timeout.connect(self._flush_intake_sync)
        self.clipboard_sync.copy_result.connect(self._clipboard_copy_result)
        self.clipboard_sync.view_changed.connect(self._clipboard_notice_state)
        self.clipboard_sync.copy_started.connect(self._clipboard_copy_started)
        self.clipboard_sync.native_access_changed.connect(self._refresh_paste_permission)
        from clipboard_preview import ClipboardPresenter
        self.clipboard_presenter=ClipboardPresenter(self.clipboard_sync,self.app)
        self.clipboard_preview_dialog=None
        for zone in self.shelf.zones.values():zone.clipboard_button.refresh()
        self._clipboard_intro=None
        self._sync_transfer_refs=set()
        self.sync.transfers_changed.connect(self._transfers_changed)
        self.manual = ManualTransferRuntime(BASE, self.app, self.account,
            enabled=not self.cocoa_smoke and self.app.platformName() not in ('offscreen','minimal'))
        self.lan = self.manual.lan
        self.wan = self.manual.wan
        self.lan_account = self.manual.authorizer
        self.lan.offer_received.connect(self._lan_offer)
        self.wan.peers_changed.connect(self._cloud_device_connections_changed)
        from video_controller import VideoController
        from video_prompt import VideoLinkWatcher
        self.video_controller=VideoController(self);self.video_watcher=VideoLinkWatcher(self)
        self.clipboard_sync.activity_changed.connect(self.video_controller.set_priority)
        self.wan.offer_received.connect(self._wan_offer)
        from receive_notice import FileTransferNotices
        self.receive_notice=FileTransferNotices(self,self.wan)
        self.lan_receive_notice=FileTransferNotices(self,self.lan)
        self.account.account_changed.connect(self._lan_identity)
        self.account.devices_changed.connect(self._lan_devices)
        self.manual.start()
        self._sync_status = ''
        self.sync.status_changed.connect(self._sync_status_changed)
        if self.cocoa_smoke:
            from global_hotkeys import _NoopBackend
            self.hotkeys = HotkeyManager(app, self.show_shortcut, self.show_overview,
                                         backend=_NoopBackend())
        else:
            self.hotkeys = HotkeyManager(app, self.show_shortcut, self.show_overview)
        self.shortcut_error = self.hotkeys.apply(self.settings.shelf_shortcut, self.settings.overview_shortcut)
        self.pointer_paste=PointerPasteController(app,self.paste_target_at_pointer,self.paste_into_target,native=not self.cocoa_smoke)
        self.pointer_paste.permission_changed.connect(self._refresh_paste_permission)
        self._applied_settings = dict(self.settings._data)
        self.settings.changed.connect(self.apply_settings)
        QTimer.singleShot(0,self.warm_overview)
        self.timer = QTimer(self.app)
        self.timer.timeout.connect(self.poll)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        if not self.cocoa_smoke:
            self.timer.start(20)
        self.app.setQuitOnLastWindowClosed(False)
        if not self.cocoa_smoke and self.app.platformName() not in ('offscreen','minimal'):
            QTimer.singleShot(0,self.account.resume)
        self.update_timer = QTimer(self.app)
        self.update_timer.timeout.connect(self.updates.check_automatically)
        # Development, release verification and offscreen tests never schedule
        # background network traffic or installer execution.
        if (not self.cocoa_smoke and getattr(sys, 'frozen', False)
                and self.app.platformName() not in ('offscreen', 'minimal')):
            self.update_timer.start(2 * 60 * 1000)
            QTimer.singleShot(0, self.updates.check_automatically)
        if (not self.cocoa_smoke and sys.platform == 'darwin'
                and getattr(sys, 'frozen', False)):
            QTimer.singleShot(1000, self._show_previous_mac_update_failure)

    def _show_previous_mac_update_failure(self):
        if self._quitting:
            return
        from mac_updates import consume_update_failure
        error = consume_update_failure(BASE,current_version=APP_VERSION)
        if error:
            self._previous_update_error=error
            self.updates.open_error(error)

    def _update_busy(self):
        return (getattr(self, '_quitting', False) or getattr(self, 'outgoing_drag', False)
                or any(transfer.active for service in (getattr(self,'lan',None),getattr(self,'wan',None)) if service for transfer in getattr(service,'_transfers',{}).values())
                or bool(QApplication.mouseButtons())
                or QApplication.activeModalWidget() is not None
                or QApplication.activePopupWidget() is not None
                or any(s.dragging or s.incoming_drop or confirmation_active(s)
                       for s in getattr(self, 'shelves', ())))

    def check_updates(self):
        self.show_update_dialog()
        self.updates.open(manual=True)

    def _show_update_notice(self, version):
        # Automatic checks defer the signal until drag and modal work ends.
        # Recheck the current state in case a newer result replaced that offer.
        if (self._quitting or self.updates.state not in ('available','ready')
                or self.updates.state_payload.get('version') != version):
            return
        self._present_update_notice()

    def _clipboard_runtime_enabled(self):
        return (self.settings.clipboard_sync_enabled and self.settings.clipboard_notice_seen
                and not self.cocoa_smoke and self.app.platformName() not in ('offscreen','minimal'))

    def _clipboard_copy_started(self):
        if hasattr(self,'clipboard_notice'):self.clipboard_notice.supersede()

    def _clipboard_copy_result(self,ok,context):
        if self._quitting or not self._clipboard_runtime_enabled():return
        if not self.settings.clipboard_success_notice:return
        if not hasattr(self,'clipboard_notice'):
            from intake_notice import ClipboardNotice
            self.clipboard_notice=ClipboardNotice(self)
        self.clipboard_notice.schedule_result(ok,context)

    def _clipboard_notice_state(self,state):
        if self._quitting:return
        if not self.settings.clipboard_success_notice or not self._clipboard_runtime_enabled():
            if hasattr(self,'clipboard_notice'):self.clipboard_notice.supersede()
            return
        if not hasattr(self,'clipboard_notice'):
            if not state.get('local_copy') or state.get('phase')!='uploading':return
            from intake_notice import ClipboardNotice
            self.clipboard_notice=ClipboardNotice(self)
        self.clipboard_notice.update_delivery(state)

    def show_clipboard_preview(self):
        if self._quitting:return
        dialog=self.clipboard_preview_dialog
        if dialog is not None and dialog.isVisible() and dialog._window_fade.pending is None:
            dialog.close();return
        if self.clipboard_presenter.state.get('permission_required'):
            self.show_mac_permissions();return
        if self.clipboard_preview_dialog is None:
            from clipboard_preview import ClipboardPreview
            self.clipboard_preview_dialog=ClipboardPreview(self)
        self.clipboard_preview_dialog.refresh();self.clipboard_preview_dialog.show()
        self.clipboard_preview_dialog.raise_();self.clipboard_preview_dialog.activateWindow()

    def show_mac_permissions(self):
        if self._quitting or sys.platform!='darwin' or not native_input_available():return
        from mac_permissions_ui import MacPermissionsDialog
        if getattr(self,'mac_permissions_dialog',None) is None:
            self.mac_permissions_dialog=MacPermissionsDialog(self,self.settings_dialog if self.settings_open() else self.shelf)
        self._center_dialog(self.mac_permissions_dialog);self.mac_permissions_dialog.show()
        self.mac_permissions_dialog.raise_();self.mac_permissions_dialog.activateWindow()

    def _present_clipboard_intro(self):
        if self.settings.clipboard_notice_seen:return False
        shelf=self.shelf
        if (self.cocoa_smoke or self.app.platformName() in ('offscreen','minimal')
                or not shelf.isVisible() or not shelf.expanded or self.settings_open()
                or shelf.content_transition_active() or confirmation_active(shelf)):return False
        if self._clipboard_intro is not None:
            if self._clipboard_intro.isVisible():return True
            self._clipboard_intro.deleteLater();self._clipboard_intro=None
        intro=UpdateNotice(shelf,theme=self.settings.theme);self._clipboard_intro=intro
        intro.show_version('云剪贴板','复制的文本、链接、图片或单个文件（小于 20 MB）会自动加密传输到云端，'
                           '并同步到同账号已绑定设备。云端仅保留最新内容，保留 24 小时；粘贴不会删除内容。'
                           '\n\n默认开启，可随时在“设置 → 云剪贴板同步”关闭。')
        intro.title.setText('云剪贴板已默认开启')
        intro.version_label.setText('剪贴板内容将同步到云端')
        intro.update_button.setText('知道了');intro.later_button.setText('关闭同步')
        intro.later_button.setFixedWidth(76)
        def finish(enabled):
            if self._clipboard_intro is not intro:return
            try:self.settings.update(clipboard_sync_enabled=enabled,clipboard_notice_seen=True)
            except OSError:return
            intro.hide();self._clipboard_intro=None;intro.deleteLater()
            if enabled and sys.platform=='darwin':QTimer.singleShot(0,self.show_mac_permissions)
        intro.update_requested.connect(lambda:finish(True))
        intro.later_button.clicked.disconnect();intro.later_button.clicked.connect(lambda:finish(False))
        intro.dismissed.connect(lambda:finish(True))
        return True

    def _present_update_notice(self):
        if self._present_clipboard_intro():return
        if getattr(self,'update_dialog',None) is not None and self.update_dialog.isVisible():return
        failed_handoff=self.updates.state=='error' and bool(getattr(self,'_previous_update_error',''))
        if (self._quitting or not (self.updates.state in ('available','ready') or failed_handoff) or not self.shelf.isVisible()
                or not self.shelf.expanded or self.shelf.content_transition_active()
                or self.shelf.presentation._timer.isActive() or self.settings_open()):return
        if self.update_notice is not None and self.update_notice.isVisible():return
        if confirmation_active(self.shelf):return
        path=Path(self.settings.base)/'updates'/'notice-state.json'
        try:
            if json.loads(path.read_text(encoding='utf8')).get('dismissed_on')==time.strftime('%Y-%m-%d'):return
        except (OSError,ValueError,TypeError,AttributeError):pass
        if self.update_notice is None:
            self.update_notice=UpdateNotice(self.shelf,theme=self.settings.theme)
            self.update_notice.update_requested.connect(self._start_notified_update)
            self.update_notice.dismissed.connect(self._snooze_update_notice)
        from release_history import update_notes
        manifest=self.updates.manifest or {}
        notes=update_notes(getattr(self.updates,'app_version',APP_VERSION),manifest.get('version',''),
                           manifest.get('notes',''),manifest.get('releases'))
        self.update_notice.show_version(self.updates.state_payload.get('version','') or APP_VERSION,
                                       notes=notes or ('上次更新未完成，原应用已保留。' if failed_handoff else ''))
        self.update_notice.set_state(self.updates.state,**self.updates.state_payload)

    def _snooze_update_notice(self):
        path=Path(self.settings.base)/'updates'/'notice-state.json'
        try:
            path.parent.mkdir(parents=True,exist_ok=True)
            temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps({'dismissed_on':time.strftime('%Y-%m-%d')}),encoding='utf8')
            temporary.replace(path)
        except OSError:pass

    def _start_notified_update(self):
        if self.update_notice is not None:self.update_notice.hide()
        self.show_update_dialog()
        self.updates.activate()

    def show_update_dialog(self):
        if self.update_dialog is None:
            from update_dialog import UpdateDialog
            self.update_dialog=UpdateDialog(APP_VERSION,theme=self.settings.theme)
            self.update_dialog.bind_controller(self.updates)
        self.update_dialog.apply_theme(self.settings.theme)
        if not self.update_dialog.isVisible():
            self._center_dialog(self.update_dialog)
        self.update_dialog.showNormal() if self.update_dialog.isMinimized() else self.update_dialog.show()
        self.update_dialog.raise_();self.update_dialog.activateWindow()
        return self.update_dialog

    def _settings_update_clicked(self):
        if self.updates.can_start_update or self.updates.busy:
            self.show_update_dialog()
            if self.updates.can_start_update:self.updates.activate()
        else:self.updates.open(manual=True)

    def _update_notice_state_changed(self, state, payload):
        if self.update_notice is not None:self.update_notice.set_state(state,**payload)

    def _cloud_device_connections_changed(self,peers):
        # Presence metadata does not create device-to-device sync connections.
        snapshot={row['account_device_id']:dict(route='cloud' if row.get('online') else 'disconnected')
                  for row in peers if row.get('account_device') is True and row.get('account_device_id')}
        self.clipboard_sync.deliveries.set_connections(snapshot)

    def _sync_status_changed(self,text):
        self._sync_status = text
        if self.settings_dialog and hasattr(self,'account_panel'):
            self.account_panel.set_sync_status(text)

    def _transfers_changed(self,transfers):
        content=self.shelf.content
        content.quiet_cloud_refresh=bool(getattr(getattr(self,'sync',None),'catching_up',False))
        for ref in self._sync_transfer_refs:
            content.transfers.pop(ref,None)
            content.pending.pop(ref,None)
        self._sync_transfer_refs=set()
        real=set(self.shelf.files['permanent'])
        for state in transfers:
            state=dict(state)
            ref=state.get('ref')
            if not ref or ref not in real:
                ref='flickdock:transfer:'+state['id']
                content.pending[ref]=dict(name=state.get('name','正在上传'),kind=state.get('kind','file'))
            content.transfers[ref]=state;self._sync_transfer_refs.add(ref)
        self.shelf.transfers_updated()

    def _lan_identity(self,account):
        name=self.account.device_name
        self.lan.device_name=(str(account.get('username',''))+' · '+name)[:80] if account else name
        # Resume on macOS is asynchronous. The overview may already exist;
        # its profile must follow the account signal independently of files.
        for shelf in self.shelves:shelf.refresh_account_identity()

    def _lan_devices(self,devices):
        current=next((d for d in devices if d.get('id')==self.account.device_id),None)
        if current:
            self.account.device_name=current['name'];self._lan_identity(self.account.account)

    def _lan_offer(self,offer):
        if offer.get('account_device') is True and self.account.signed_in and offer.get('account_id')==self.account.account['id']:
            try:
                if self.lan.accept(offer['id'],self.settings.lan_receive_dir):self.lan_receive_notice.begin(offer)
            except (OSError,ValueError) as error:
                self.lan.reject(offer['id']);self.lan.status_changed.emit('同账号局域网文件未能接收：'+str(error))
            return
        self.show_lan_dialog()
        self.lan_dialog.panel.show_offer(offer)

    def _wan_offer(self,offer):
        if (offer.get('account_device') is True and self.account.signed_in
                and offer.get('account_id')==self.account.account['id']
                and self.wan._bound_identity==(self.account.device_id,self.account.api.token)):
            try:
                if self.wan.accept(offer['id'],self.settings.lan_receive_dir):self.receive_notice.begin(offer)
            except (OSError,ValueError) as error:
                self.wan.reject(offer['id']);self.wan.status_changed.emit('同账号文件未能接收：'+str(error))
            return
        self.show_wan_dialog();self.wan_dialog.panel.show_offer(offer)

    def _manual_send_notice(self,service,details):
        notice=self.receive_notice if service is self.wan else self.lan_receive_notice
        notice.begin(details)

    def _lan_selected(self):
        refs=self.shelf.popup.chosen_refs() if self.shelf.popup else []
        return [ref for ref in refs if not self.shelf.content.is_text(ref) and Path(ref).exists()]

    def _create_shelf(self, data):
        shelf = ShelfWindow(self, 1, data)
        shelf.handle = self.edge
        self.shelf = self.active_shelf = shelf
        self.shelves = [shelf]

    def activate_shelf(self, shelf):
        # Keep the edge target visible until the shelf has actually been
        # shown and raised. _show_at calls update_handles after that handoff.
        self.active_shelf = shelf

    def repair_network_access(self):
        from windows_network import repair_network_access
        if not repair_network_access():
            QMessageBox.information(self.shelf,'联网修复','未完成管理员授权，请重新选择“修复网络访问”后确认系统提示。')

    def tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_first()

    def show_first(self):
        if hasattr(self,'shelf') and confirmation_active(self.shelf): return
        if not self.shelves:
            self._create_shelf({})
        self.active_shelf.show_transient()

    def show_docked(self):
        if self.settings_open():self.settings_edge_hint.show_hint();return
        if self._dock_preview_values is not None or confirmation_active(self.shelf): return
        if self.active_shelf is None or not self.active_shelf.has_files():
            self.active_shelf = next((s for s in self.shelves if s.has_files()), self.shelves[0])
        self.active_shelf.reveal_from_dock(expanded=self.settings.trigger_view=='expanded')

    def warm_overview(self):
        if self._quitting:return
        shelf=self.shelf
        if not shelf.isVisible() and shelf.popup is None:
            # Compact-first users need the same idle construction as direct
            # overview users, before their first expand click.
            shelf.popup=ContentsPopup(shelf)
            shelf.popup.hide()

    def paste_new(self):
        if not self.active_shelf.isVisible():self.show_first()
        self.active_shelf.paste_clipboard("permanent")

    def settings_open(self):
        return bool(self.settings_dialog and self.settings_dialog.isVisible())

    def paste_target_at_pointer(self):
        if (self._quitting or self.settings_open() or self.outgoing_drag
                or QApplication.activeModalWidget() is not None or QApplication.activePopupWidget() is not None):return None
        point=QCursor.pos()
        for shelf in self.shelves:
            if (shelf.isVisible() and shelf.visible_state!='hiding' and not shelf.dragging
                    and not shelf.intake_blocks_overview() and not confirmation_active(shelf)
                    and shelf.visual_geometry().adjusted(14,7,-14,-20).contains(point)
                    and pointer_over_shelf(shelf,point)):
                # Ignore the reserved transparent animation canvas and the hover
                # bridge; neither is an actual paste surface.
                return shelf
        return None

    def paste_into_target(self,shelf):
        if (shelf in self.shelves and shelf.isVisible() and not self.settings_open()
                and not confirmation_active(shelf)):return shelf.paste_clipboard('permanent')
        return False

    def close_settings_from_edge(self):
        if self.settings_dialog:self.settings_dialog.reject()
        self.settings_edge_hint.hide();self.update_handles()

    def _settings_closed(self,*_):
        self.settings_edge_hint.hide();self.update_handles()
        if getattr(self,'mac_permissions_dialog',None) is not None:self.mac_permissions_dialog.hide()

    def _refresh_paste_permission(self):
        if self.settings_dialog:
            mac=sys.platform=='darwin'
            required=self.pointer_paste.permission_required or (mac and not self.clipboard_sync._native_accessible())
            self.settings_dialog.set_pointer_paste_permission(required,
                self.show_mac_permissions if mac else self.pointer_paste.request_permission,unified=mac,
                listener_pending=mac and self.pointer_paste.listener_pending)


    def _center_dialog(self,dialog):
        if hasattr(dialog,'chrome'):dialog.chrome.prepare_open()
        screen=QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area=screen.availableGeometry()
        dialog.move(area.center()-QPoint(dialog.width()//2,dialog.height()//2))

    def show_settings(self,*,category=None):
        if self.update_notice is not None:
            self.update_notice.hide()
        self.reset_drag_gesture()
        self.shelf.hide_immediately()
        self.auto_shown=None
        if self.settings_dialog is None:
            self.settings_dialog = SettingsDialog(self.settings)
            self.settings_dialog.finished.connect(self._settings_closed)
            self._refresh_paste_permission()
            self.settings_dialog.dock_requested.connect(lambda:self.shelf.hide_animated(explicit=True))
            self.settings_dialog.mini_requested.connect(self.show_shortcut)
            self.settings_dialog.update_requested.connect(self._settings_update_clicked)
            self.updates.state_changed.connect(self._render_settings_update)
            self.account_panel = AccountPanel(self.account,self.settings_dialog,theme=self.settings.theme,email_binding=False)
            self._cloud_device_connections_changed(self.wan.peers())
            self.settings_dialog.set_account_panel(self.account_panel)
            self.settings_dialog.preview_changed.connect(self.preview_dock)
            self.settings_dialog.gesture_preview_changed.connect(self.preview_shake)
            self.settings_dialog.shortcut_validator = lambda values: self.hotkeys.validate(values["shelf_shortcut"], values["overview_shortcut"])
        if self.settings_dialog.isVisible():self.settings_dialog.load_values()
        self.settings_dialog.set_update_state(self.updates.state,**self.updates.state_payload)
        self._render_settings_update(self.updates.state,self.updates.state_payload)
        self.account_panel.set_sync_status(self._sync_status)
        if category:self.settings_dialog.select_category(category)
        self._center_dialog(self.settings_dialog)
        self.settings_dialog.show()
        self.update_handles()
        self.settings_dialog.scroll.verticalScrollBar().setValue(0)
        QTimer.singleShot(0,self.settings_dialog,lambda:self.settings_dialog.scroll.verticalScrollBar().setValue(0))
        problem = self.shortcut_error or self.startup_error
        if problem:
            self.settings_dialog.error_label.setText(problem)
            self.settings_dialog.error_label.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    def _render_settings_update(self,state,payload):
        if self.settings_dialog is None:return
        self.settings_dialog.set_update_state(state,**payload)
        manifest=self.updates.manifest or {}
        self.settings_dialog.set_update_details(manifest.get('version',''),'')

    def show_transfer_dialog(self):
        dialog=self.lan_dialog or self.wan_dialog
        self._show_transfer_dialog(self.wan if dialog and dialog._wan else self.lan)

    def show_video_dialog(self,url=None):
        from video_panel import VideoDialog
        if self.video_dialog is None:self.video_dialog=VideoDialog(self,self.video_controller)
        if url is not None:self.video_dialog.url.setText(url);self.video_controller.parse(url)
        self.shelf._release_reveal_level()
        self.video_dialog.refresh();self._center_dialog(self.video_dialog);self.video_dialog.show();self.video_dialog.raise_();self.video_dialog.activateWindow()

    def queue_transfer_paths(self,mode,paths):
        self._show_transfer_dialog(self.wan if mode=='wan' else self.lan)
        self.lan_dialog.panel.queue_paths(paths)

    def show_lan_dialog(self):
        self._show_transfer_dialog(self.lan)

    def show_wan_dialog(self):
        self._show_transfer_dialog(self.wan)

    def _show_transfer_dialog(self,service):
        if getattr(self.shelf,'isVisible',lambda:False)() and self.shelf.expanded:
            self._overview_return_rect=QRect(self.shelf.geometry_morph._to if self.shelf.geometry_morph.active else self.shelf.visual_geometry())
        self.shelf._release_reveal_level()
        dialog=self.lan_dialog or self.wan_dialog
        if dialog is None:
            # One independent native window; both modes retain their own panel
            # state without exposing the desktop between close/show operations.
            dialog=LanDialog(service,self.settings)
            dialog.send_started.connect(self._manual_send_notice)
            dialog.overview_requested.connect(lambda:self.show_overview_from_transfer(dialog))
            dialog.mode_selected.connect(lambda mode:self.show_wan_dialog() if mode=='wan' else self.show_lan_dialog())
            dialog.lan_recommended.connect(self._use_recommended_lan)
            dialog.mini_requested.connect(self.show_shortcut)
            dialog.dock_requested.connect(lambda:self.shelf.hide_animated(explicit=True))
        dialog.set_service(service).set_paths_provider(self._lan_selected)
        self.lan_dialog=self.wan_dialog=dialog
        if not dialog.isVisible():
            self._center_dialog(dialog)
        if dialog.isMinimized():dialog.showNormal()
        elif not dialog.isVisible():dialog.show()
        dialog.raise_();dialog.activateWindow()

    def _use_recommended_lan(self,paths,identifier):
        previous=self.lan_dialog.panel if self.lan_dialog else None
        self.show_lan_dialog();panel=self.lan_dialog.panel
        if paths:panel.queue_paths(paths)
        index=panel.devices.findData(identifier)
        if index>=0:panel.devices.setCurrentIndex(index)
        if previous and previous is not panel:previous.queue_paths([],replace=True)

    def show_cloud_capacity_notice(self,paths):
        if self._quitting or not paths:return
        if not hasattr(self,'cloud_capacity_notice'):
            from shelf_capacity import CloudCapacityNotice
            self.cloud_capacity_notice=CloudCapacityNotice(self)
        self.cloud_capacity_notice.show_paths(paths)

    def check_cloud_capacity_for_upload(self,ref,callback):
        def checked(insufficient):
            if insufficient:self.show_cloud_capacity_notice([ref])
            elif ref in self.shelf.files['permanent']:callback()
        self.check_cloud_capacity([ref],checked,include_existing=True)

    def check_cloud_capacity(self,paths,callback,*,include_existing=False):
        if not self.account.signed_in or not self.settings.sync_upload_enabled:
            callback(False);return
        from shelf_capacity import intake_bytes
        refs=[ref for ref in dict.fromkeys(paths) if include_existing or ref not in self.shelf.files['permanent']]
        epoch=self.sync.epoch
        if not refs:callback(False);return
        job=DiskWork(intake_bytes,refs,dict(self.shelf.content.texts))
        self._capacity_jobs=getattr(self,'_capacity_jobs',set());self._capacity_jobs.add(job)
        def sized(size,error):
            self._capacity_jobs.discard(job)
            if self._quitting:return
            if epoch!=self.sync.epoch:callback(None);return
            if error:
                self.sync.status_changed.emit('容量检查未完成：'+str(error));callback(False);return
            def checked(storage,error):
                if self._quitting or epoch!=self.sync.epoch:return
                if error:self.sync.status_changed.emit('暂时无法读取云端容量，文件先保留在本机。')
                callback(storage is not None and size>storage['remaining_bytes'])
            if self.sync.storage is not None:checked(self.sync.storage,None)
            else:
                # An unavailable cloud must not hold a local drop hostage.
                # Upload reservation still enforces the same quota atomically
                # before any file body can be sent.
                self.sync.refresh_storage(lambda value,error:None)
                callback(False)
        job.signals.done.connect(sized);QThreadPool.globalInstance().start(job,-1)

    def _remote_intake(self,paths):
        paths=[ref for ref in paths if ref in self.shelf.files['permanent']]
        if not paths:return
        self.show_wan_dialog();panel=self.wan_dialog.panel;panel.queue_paths(paths)
        queued=set(panel._queued_paths)
        for ref in paths:
            if ref in queued:self.shelf.remove_path('permanent',ref,keep_local=True)

    def offer_large_intake(self,paths):
        if self._quitting or not paths:return
        job=DiskWork(oversized_intake,list(paths))
        self._large_intake_jobs=getattr(self,'_large_intake_jobs',set());self._large_intake_jobs.add(job)
        def ready(result,error):
            self._large_intake_jobs.discard(job)
            if self._quitting or error or not result:return
            current=[ref for ref in result if ref in self.shelf.files['permanent']]
            if not current:return
            if not hasattr(self,'remote_intake_notice'):
                from shelf_intake_limit import make_notice
                self.remote_intake_notice=make_notice(self)
            self.remote_intake_notice.show_paths(current)
        job.signals.done.connect(ready);QThreadPool.globalInstance().start(job,-1)

    def _refresh_displays(self,*_):
        from display_triggers import display_id
        screens=QGuiApplication.screens();wanted={display_id(screen):screen for screen in screens}
        for key in list(self.edges):
            if key not in wanted:
                edge=self.edges.pop(key);edge.hide();edge.deleteLater()
        for key,screen in wanted.items():
            if key not in self.edges:
                self.edges[key]=DockHandle(self,screen)
                screen.availableGeometryChanged.connect(self._position_edges)
                screen.geometryChanged.connect(self._position_edges)
            else:self.edges[key].display=screen
        if getattr(self,'edge',None) not in self.edges.values():
            self.edge=next((edge for edge in self.edges.values() if edge.display is QGuiApplication.primaryScreen()),next(iter(self.edges.values()),None))
        if hasattr(self,'shelf'):self.update_handles()

    def _position_edges(self,*_):
        for edge in self.edges.values():edge.position_at_edge()

    def dock_values(self):
        return {key:value for key,value in self.edge.values().items() if key!='enabled'}

    def preview_dock(self,values):
        self._dock_preview_values=dict(values) if isinstance(values,dict) else None
        # Real per-screen handles preview the draft and remain independent hit
        # targets. A single transparent preview on the active screen hid all
        # secondary handles while settings were open.
        self.dock_preview.hide()
        self._position_edges()
        if self._settings_prompt_active():
            self.settings_edge_hint.hide()
        if not self._quitting:self.update_handles()

    def _settings_prompt_active(self):
        modal=QApplication.activeModalWidget()
        return (modal is not None and modal is not self.settings_dialog) or QApplication.activePopupWidget() is not None

    def apply_settings(self):
        current=dict(self.settings._data)
        previous=getattr(self,'_applied_settings',None)
        self._applied_settings=current
        changed={key for key in current if previous is None or previous.get(key)!=current[key]}
        if not changed:return
        if changed & {'clipboard_sync_enabled','clipboard_notice_seen'}:
            self.clipboard_sync.set_enabled(self._clipboard_runtime_enabled())
            if not self._clipboard_runtime_enabled() and hasattr(self,'clipboard_notice'):self.clipboard_notice.supersede()
        if 'clipboard_success_notice' in changed and not self.settings.clipboard_success_notice:
            if hasattr(self,'clipboard_notice'):self.clipboard_notice.supersede()
        if 'view_mode' in changed:
            # A presentation preference cannot reset a drag gesture, repaint
            # every application panel, re-register hotkeys or touch sync.
            if self.shelf.popup:
                popup=self.shelf.popup
                popup.view_list.setChecked(self.settings.view_mode=='list')
                popup.view_grid.setChecked(self.settings.view_mode=='icons')
                popup.rebuild()
        if 'theme' in changed:
            ui_theme.set_theme(self.settings.theme)
            ui_theme.apply_application(self.app, shelf_ui_common.FONT_FAMILY)
            self.shelf.refresh()
            if self.shelf.popup:self.shelf.popup.preview.media.apply_theme()
            if self.settings_dialog:self.settings_dialog.apply_theme(self.settings.theme)
            if self.lan_dialog:self.lan_dialog.apply_theme(self.settings.theme)
            if self.wan_dialog:self.wan_dialog.apply_theme(self.settings.theme)
            if self.video_dialog:self.video_dialog.refresh()
            self.updates.apply_theme(self.settings.theme)
            if self.update_notice is not None:self.update_notice.apply_theme(self.settings.theme)
            if self.update_dialog is not None:self.update_dialog.apply_theme(self.settings.theme)
        if changed & {'sync_upload_enabled','sync_receive_enabled'}:
            self.sync.set_directions(self.settings.sync_upload_enabled,self.settings.sync_receive_enabled)
        if changed & {'dock_edge','dock_position','dock_length','dock_thickness','dock_color','dock_opacity','trigger_visible','display_triggers'}:
            self._position_edges()
        if 'shake_sensitivity' in changed:self._apply_shake_sensitivity()
        if changed & {'trigger_view','capture_mode','shake_sensitivity'}:self.reset_drag_gesture()
        if 'trigger_view' in changed and self.settings.trigger_view=='expanded':
            QTimer.singleShot(0,self.warm_overview)
        if changed & {'theme','keep_empty_handle','trigger_visible','dock_edge','dock_position','dock_length','dock_thickness','dock_color','dock_opacity','display_triggers'}:
            self.update_handles()
        if changed & {'shelf_shortcut','overview_shortcut'}:
            self.shortcut_error=self.hotkeys.apply(self.settings.shelf_shortcut,self.settings.overview_shortcut)

    def show_overview_from_transfer(self,dialog):
        if confirmation_active(self.shelf) or self.shelf._archive_choosing:return
        self.reset_drag_gesture();self.edge_suppressed=False
        target=QRect(getattr(self,'_overview_return_rect',QRect()))
        if target.isEmpty():target=self.shelf._expansion_rect(self.shelf.visual_geometry())
        screen=QGuiApplication.screenAt(target.center()) or QGuiApplication.primaryScreen()
        if screen:
            area=screen.availableGeometry()
            target.setSize(target.size().boundedTo(area.size()))
            target.moveLeft(max(area.left(),min(target.left(),area.right()-target.width()+1)))
            target.moveTop(max(area.top(),min(target.top(),area.bottom()-target.height()+1)))
        with self.shelf._surface_transaction():
            self.shelf.presentation_kind='overview';self.shelf.visible_state='floating'
            if not self.shelf.isVisible():self.shelf.setGeometry(self.shelf._reserved_surface_rect(target,target))
            self.shelf.show_contents(target=target,animate=False)
            self.shelf.presentation.jump({'sx':.97 if self.motion else 1,'sy':.97 if self.motion else 1,'alpha':0,'dx':0,'dy':0})
            self.shelf.show();self.shelf._raise_for_reveal()
            from ui_transitions import SHELF_REVEAL_RESPONSE_SECONDS,SHELF_REVEAL_DAMPING
            self.shelf.presentation.animate({'sx':1,'sy':1,'alpha':1,'dx':0,'dy':0},response=SHELF_REVEAL_RESPONSE_SECONDS,damping=SHELF_REVEAL_DAMPING)
        self.shelf.activateWindow();self.shelf.setFocus()

    def reset_drag_gesture(self):
        self._drag_content_ready=False
        self._content_gesture_at=None
        self.drag_gate.reset_session()
        self.shake_detector.reset()
        self._drag_probe_detector.reset()

    def _apply_shake_sensitivity(self):
        value=self.settings.shake_sensitivity if self._shake_preview_value is None else self._shake_preview_value
        config=config_for_sensitivity(value)
        if self.shake_detector.config!=config:
            self.reset_drag_gesture()
            self.shake_detector=ShakeDetector(config)

    def preview_shake(self,value):
        self._shake_preview_value=value
        self._apply_shake_sensitivity()

    def show_shortcut(self):
        if confirmation_active(self.shelf) or self.shelf._archive_choosing: return
        self.reset_drag_gesture()
        self.edge_suppressed = False
        if self.shelf.expanded:
            cursor=QCursor.pos()
            area=(QGuiApplication.screenAt(cursor) or QGuiApplication.primaryScreen()).availableGeometry()
            x=max(area.left()+4,min(cursor.x()+22,area.right()-CARD_W-32))
            y=max(area.top()+4,min(cursor.y()-130,area.bottom()-CARD_H-31))
            self.shelf.presentation_kind='shortcut'
            self.shelf.visible_state='floating'
            self.shelf.collapse_contents(target=QRect(x,y,CARD_W+28,CARD_H+27))
            self.shelf._raise_for_reveal()
        else:self.shelf.show_transient(QCursor.pos(), reason="shortcut")
        if not self._pointer_dragging():
            self.shelf.activateWindow()
            self.shelf.setFocus()

    def _pointer_dragging(self):
        return bool(mouse_buttons_mask() & 1)

    def show_overview(self):
        if confirmation_active(self.shelf) or self.shelf._archive_choosing: return
        self.reset_drag_gesture()
        self.edge_suppressed = False
        cursor = QCursor.pos()
        area = (QGuiApplication.screenAt(cursor) or QGuiApplication.primaryScreen()).availableGeometry()
        width,height=min(EXPANDED_W,area.width()-38),min(EXPANDED_H,area.height()-38)
        x = cursor.x() + 18
        if x + width + 28 > area.right():
            x = cursor.x() - width - 46
        x = max(area.left() + 4, min(x, area.right() - width - 30))
        y = max(area.top() + 4, min(cursor.y() - 55, area.bottom() - height - 30))
        target=QRect(x,y,width+28,height+27)
        if not self.shelf.isVisible():
            # A shortcut summons the final overview with the preview entrance.
            # Showing a compact shelf first adds a sideways expansion and a
            # second animation phase before the overview is ready to use.
            with self.shelf._surface_transaction():
                self.shelf.setGeometry(self.shelf._reserved_surface_rect(target,target))
                self.shelf._show_contents('permanent',target,animate=False)
                self.shelf._show_at(target.topLeft(),'overview',QPoint(0,14))
        else:
            self.shelf.presentation_kind='overview'
            self.shelf.visible_state='floating'
            self.shelf.show_contents(target=target)
        if not self._pointer_dragging():
            self.shelf.activateWindow()
            self.shelf.setFocus()

    def load(self):
        self._legacy_source = None
        source = STATE_FILE if STATE_FILE.exists() else OLD_STATE_FILE
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            if int(data.get("version", 0)) < 6:
                self._legacy_source = source
            merged = merge_shelves(data.get("shelf", data))
            retained, seen = [], set()
            for ref in merged.get("permanent", []) + merged.get("temporary", []):
                key = ref if ContentRepository.is_text(ref) else os.path.normcase(ref)
                if key not in seen:
                    seen.add(key)
                    retained.append(ref)
            merged["permanent"] = retained
            merged.pop("temporary", None)
            current=data.get("shelf",data)
            merged["metadata"]=current.get("metadata",{}) if isinstance(current,dict) else {}
            merged["pinned"]=current.get("pinned",False) if isinstance(current,dict) else False
            self._create_shelf(merged)
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
            pass

    def _flush_intake_sync(self):
        if not self._quitting and hasattr(self,'sync'):
            self.sync.local_changed()

    def save_after_intake(self):
        self.save(defer_sync=True)

    def save(self, *, defer_sync=False):
        BASE.mkdir(parents=True, exist_ok=True)
        if self._legacy_source is not None and self._legacy_source.exists():
            backup = BASE / "shelves-before-v6.json"
            if not backup.exists():
                shutil.copy2(self._legacy_source, backup)
            self._legacy_source = None
        refs = self.shelf.files["permanent"]
        self.shelf.content.prune(refs)
        item = {"permanent": self.shelf.files["permanent"],
                "texts": self.shelf.content.export_texts(refs), "metadata": self.shelf.content.export_metadata(refs), "pinned": self.shelf.pinned, "pos": [self.shelf.visual_geometry().x(), self.shelf.visual_geometry().y()]}
        data = {"version": 6, "shelf": item}
        temp = STATE_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(STATE_FILE)
        if hasattr(self,'sync'):
            if defer_sync:
                # Let Qt paint the accepted item and start its landing before
                # scanning/journaling cloud changes on the GUI thread. The
                # local shelf file is already durable at this point.
                if not self._intake_sync_timer.isActive():
                    self._intake_sync_timer.start(48)
            else:
                self._intake_sync_timer.stop()
                self.sync.local_changed()

    def update_handles(self):
        shake_visible=any(s.isVisible() and s.presentation_kind=='drag' and not s.expanded for s in self.shelves)
        for edge in getattr(self,'edges',{'primary':self.edge}).values():
            if self.settings_open():edge.show_handle()
            elif self._dock_preview_values is not None:edge.hide()
            elif shake_visible or (self.settings.keep_empty_handle or any(s.has_files() for s in self.shelves)) and not any(s.isVisible() for s in self.shelves):edge.show_handle()
            else:edge.hide()

    def _content_drag_validated(self, position):
        if (self.outgoing_drag or self.auto_shown is not None or
                self.shelf.isVisible() and self.shelf.visible_state != 'hiding'):
            return
        self._content_gesture_at=None
        shelf = self.active_shelf or self.shelves[0]
        if shelf.expanded:shelf.hide_immediately()
        shelf.show_transient(position,reason='drag',velocity=self.shake_detector.trigger_velocity)
        self.auto_shown = shelf

    def _content_type_ready(self,position):
        if (self.outgoing_drag or self.shelf.isVisible() and self.shelf.visible_state != 'hiding'
                or not self.was_down):return
        # OLE DragEnter can arrive well after the configured shake when a
        # recording overlay delays or repeatedly reorders its native target.
        # The MIME probe and completed gesture both belong to this held press;
        # reset_drag_gesture clears the timestamp when the press ends.
        self._drag_content_ready=True
        self._drag_probe_detector.reset()
        if (self._content_gesture_at is not None and self._pointer_dragging() and not self.drag_started_inside
                and not native_window_move_active()):
            self._content_drag_validated(position)

    def _probe_drop(self, mime, position):
        if self.auto_shown is None:return False
        return bool(self.auto_shown and self.auto_shown.receive_mime("permanent", mime))

    def poll(self):
        if hasattr(self,'sync'):
            self.sync.set_interactive(self.shelf.isVisible() or bool(self.settings_dialog and self.settings_dialog.isVisible())
                or bool(self.lan_dialog and self.lan_dialog.isVisible()) or bool(self.wan_dialog and self.wan_dialog.isVisible())
                or bool(self.video_dialog and self.video_dialog.isVisible()))
        now=time.monotonic(); pos=QCursor.pos()
        # Cocoa may move a preallocated tool surface beneath a stationary
        # pointer without delivering Enter/Move to its children. Reconcile on
        # the existing native input tick, including while a drag is active.
        for shelf in self.shelves:
            if shelf.isVisible() and not shelf.expanded:
                shelf.zones['permanent']._reconcile_card_hover()
        for edge in getattr(self,'edges',{}).values():
            if edge.values()['enabled'] and edge.display.geometry().contains(pos):self.edge=edge;break
        self.drop_bridge.sync(self.shelf,dragging=bool(mouse_buttons_mask()&1))
        if self.settings_open():
            if self._settings_prompt_active():
                self.dock_preview.hide();self.settings_edge_hint.hide()
                self.reset_drag_gesture();self.was_down=bool(mouse_buttons_mask()&1);return
            if self.edge.values()['enabled'] and (self.edge.activation_region().contains(pos) or self.settings_edge_hint.contains_pointer(pos)):
                if not self.settings_edge_hint.isVisible():self.settings_edge_hint.show_hint()
            else:self.settings_edge_hint.hide()
            self.reset_drag_gesture();self.was_down=bool(mouse_buttons_mask()&1);return
        self.settings_edge_hint.hide()
        app_active=self.app.applicationState() == Qt.ApplicationState.ApplicationActive
        app_deactivated=self._app_was_active and not app_active
        self._app_was_active=app_active
        native=native_input_available()
        mask=mouse_buttons_mask()
        down=bool(mask&1); new_press=bool(mask&~self._button_mask); self._button_mask=mask
        if self.edge_suppressed and not self.edge.activation_region().contains(pos): self.edge_suppressed=False
        if app_deactivated:
            retired=False
            for shelf in self.shelves:
                if (shelf.isVisible() and not shelf.pinned and not shelf.dragging
                        and not shelf.incoming_drop and not shelf._archive_choosing and not shelf.picker_hold_active(now)
                        and not confirmation_active(shelf) and not self.outgoing_drag
                        and not (self.clipboard_preview_dialog is not None and self.clipboard_preview_dialog.isVisible()
                                 and self.clipboard_preview_dialog.frameGeometry().contains(pos))):
                    shelf.hide_animated(explicit=True);retired=True
            if retired:self.update_handles()
        if self.edge.isVisible() and self.edge.activation_region().contains(pos) and not self.edge_suppressed and not self.shelf.isVisible(): self.show_docked()
        for shelf in self.shelves:
            if not shelf.isVisible(): continue
            modal=QApplication.activeModalWidget(); popup=QApplication.activePopupWidget()
            blocked=shelf.picker_hold_active(now) or shelf._menu_open or shelf.rename_edit.isVisible() or modal is not None or popup is not None or confirmation_active(shelf)
            press_blocked=shelf.picker_hold_active(now) or modal is not None or shelf.rename_edit.isVisible() or (popup is not None and popup.frameGeometry().contains(pos)) or confirmation_active(shelf)
            in_settings=app_active and self.settings_dialog is not None and self.settings_dialog.isVisible() and self.settings_dialog.frameGeometry().contains(pos)
            in_settings=in_settings or bool(app_active and self.lan_dialog and self.lan_dialog.isVisible() and self.lan_dialog.frameGeometry().contains(pos))
            in_settings=in_settings or bool(app_active and self.wan_dialog and self.wan_dialog.isVisible() and self.wan_dialog.frameGeometry().contains(pos))
            in_settings=in_settings or bool(app_active and self.video_dialog and self.video_dialog.isVisible() and self.video_dialog.frameGeometry().contains(pos))
            capacity_notice=getattr(self,'cloud_capacity_notice',None)
            in_settings=in_settings or bool(capacity_notice and capacity_notice.isVisible() and capacity_notice.frameGeometry().contains(pos))
            inside=shelf.hover_region_contains(pos)
            if new_press and in_settings:shelf._release_reveal_level()
            if new_press and not inside and not in_settings and not press_blocked and not shelf.pinned and not self.outgoing_drag:
                shelf.hide_animated(explicit=True)
                continue
            if shelf.dragging or shelf.incoming_drop or shelf.pinned: continue
            if shelf.presentation_kind=='dock' and not blocked and not self.outgoing_drag:
                if not inside and shelf.visible_state!='hiding': shelf.hide_animated(explicit=False)
                elif inside and shelf.visible_state=='hiding' and not shelf._explicit_hiding and not self.edge_suppressed: shelf.reveal_from_dock()
            elif shelf.presentation_kind=='drag' and shelf.visible_state=='floating' and not blocked:
                if shelf.shake_idle_expired(pos,now,inside):
                    shelf.hide_animated(explicit=False)
                    self.auto_shown=None;self.just_dropped=now;self.reset_drag_gesture()
            elif shelf.visible_state=='floating' and not blocked and not down and not shelf.preview_playing:
                if inside or in_settings: shelf.touch()
                elif now-shelf.last_activity>=IDLE_SECONDS: shelf.hide_animated(explicit=False)
        if not native or self.outgoing_drag:
            self.was_down=down; self.reset_drag_gesture(); return
        if down and not self.was_down:
            self.reset_drag_gesture(); self.auto_shown=None
            self.drag_started_inside=any(s.isVisible() and s.visual_geometry().contains(pos) for s in self.shelves)
        # Python 3.12 on Windows uses a coarse GetTickCount clock for monotonic.
        # QPC timestamps avoid quantised dt corrupting velocity/acceleration.
        sample_time=time.perf_counter()
        moving=native_window_move_active()
        blocked=(self.outgoing_drag or self.drag_started_inside or
                 self.shelf.isVisible() and self.shelf.visible_state != 'hiding')
        full_shake=self.shake_detector.update(pos.x(),pos.y(),sample_time,
                                               button_down=down,window_moving=moving,blocked=blocked)
        probe_shake=(not self._drag_content_ready and self._drag_probe_detector.update(
            pos.x(),pos.y(),sample_time,button_down=down,window_moving=moving,blocked=blocked))
        if full_shake and self.auto_shown is None and not self.shelf.isVisible() and now-self.just_dropped>.4:
            self._content_gesture_at=sample_time
            if self._drag_content_ready:self._content_drag_validated(pos)
            else:self.drag_gate.sustain_for_shake(pos)
        if probe_shake and self.auto_shown is None and not self.shelf.isVisible() and now-self.just_dropped>.4:
            self.drag_gate.sustain_for_shake(pos)
        if self.drag_gate.active: self.drag_gate.track_position(pos)
        if not down and self.was_down:
            self.reset_drag_gesture()
            if self.auto_shown and not self.auto_shown.incoming_drop: self.auto_shown.hide_animated(explicit=False)
            self.auto_shown=None
        self.was_down=down

    def quit(self):
        if hasattr(self,'receive_notice'):self.receive_notice.close()
        if hasattr(self,'lan_receive_notice'):self.lan_receive_notice.close()
        if hasattr(self,'intake_notice'):self.intake_notice.hide()
        if hasattr(self,'clipboard_notice'):self.clipboard_notice.supersede()
        if hasattr(self,'remote_intake_notice'):self.remote_intake_notice.hide()
        for job in getattr(self,'_large_intake_jobs',()):job.cancel.set()
        if hasattr(self,'cloud_capacity_notice'):self.cloud_capacity_notice.close()
        for job in getattr(self,'_capacity_jobs',()):job.cancel.set()
        if getattr(self,'clipboard_preview_dialog',None) is not None:self.clipboard_preview_dialog.hide()
        if getattr(self,'mac_permissions_dialog',None) is not None:self.mac_permissions_dialog.hide()
        if hasattr(self,'clipboard_presenter'):self.clipboard_presenter.close()
        if self.update_dialog is not None:self.update_dialog.hide()
        if self.update_notice is not None:
            self.update_notice.hide()
        self.manual.close()
        if hasattr(self,'video_watcher'):self.video_watcher.close()
        if hasattr(self,'video_controller'):self.video_controller.close()
        if self.video_dialog:self.video_dialog.close()
        self._quitting=True
        self.pointer_paste.close();self.settings_edge_hint.close()
        self._intake_sync_timer.stop()
        self.timer.stop()
        self.update_timer.stop()
        self.updates.close()
        self.cloud.close()
        self.drop_bridge.hide();self.drop_bridge.deleteLater()
        self.hotkeys.close()
        if self.settings_dialog:
            self.settings_dialog.close()
        if self.lan_dialog:self.lan_dialog.close()
        if self.wan_dialog:self.wan_dialog.close()
        self.drag_gate.close()
        if self.outgoing_drag:
            QDrag.cancel()
        self.save()
        self.tray.hide()
        for edge in getattr(self,'edges',{'primary':self.edge}).values():edge.hide()
        self.dock_preview.hide()
        for shelf in self.shelves:
            shelf._copy_epoch+=1
            for job in getattr(shelf,'_intake_limit_jobs',()):job.cancel.set()
            for job in tuple(shelf.copy_jobs):job.cancel();job.wait(3000)
            if shelf.archive_job:
                shelf.archive_job.cancel();shelf.archive_job.wait(3000)
            shelf.resolver.close()
            shelf.image_cache.close()
            if shelf.popup: shelf.popup.preview.media.release_source()
            shelf.presentation.stop()
            shelf.geometry_morph.stop()
            shelf.hide()
        self.app.quit()


def main():
    if '--video-worker' in sys.argv:
        from video_worker import run
        return run()
    if '--verify-overview' in sys.argv:
        from offscreen_verify import run_overview
        return run_overview(sys.argv,load_font)
    cocoa_smoke = '--verify-cocoa-startup' in sys.argv
    if cocoa_smoke and (sys.platform != 'darwin' or not getattr(sys, 'frozen', False)
                        or os.environ.get('FLICKDOCK_CI_COCOA_SMOKE') != '1'
                        or os.environ.get('QT_QPA_PLATFORM', '').lower() != 'cocoa'):
        return 2
    if '--verify-cloud-stream' in sys.argv:
        from test_cloud_api_stream_v183 import main as verify_cloud_stream
        return verify_cloud_stream()
    if '--verify-clipboard' in sys.argv:
        from verify_clipboard import main as verify_clipboard
        return verify_clipboard()
    if '--verify-wan' in sys.argv:
        from verify_wan import run
        return run(sys.argv,load_font)
    if '--verify-transfers' in sys.argv:
        from verify_transfers import run
        return run(sys.argv,load_font)
    if "--verify-cloud" in sys.argv:
        from verify_cloud import run
        return run(sys.argv,load_font)
    if "--verify-update" in sys.argv:
        from offscreen_verify import run_update
        return run_update(sys.argv, load_font)
    if "--verify-media" in sys.argv:
        # Release verification runs without the manager, tray or native hooks.
        # Require the offscreen platform before constructing any QWidget.
        from offscreen_verify import run
        return run(sys.argv, load_font)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    from platform_native import configure_menu_bar_application
    configure_menu_bar_application()
    load_font(app)
    guard = InstanceGuard(app, str(BASE))
    if not guard.acquire():
        return
    if sys.platform=='win32' and native_input_available():
        from gui_watchdog import GuiWatchdog
        app._ui_watchdog=GuiWatchdog(app,BASE)
    mac_icon=Path(sys.executable).parent.parent/'Resources'/'FlickDock.icns'
    app.setWindowIcon(QIcon(str(mac_icon)) if sys.platform=='darwin' and getattr(sys,'frozen',False)
                      and mac_icon.is_file() else app_icon())
    manager = ShelfManager(app, cocoa_smoke=cocoa_smoke)
    guard.activated.connect(manager.show_first)
    if cocoa_smoke:
        index = sys.argv.index('--verify-cocoa-startup')
        if index + 1 >= len(sys.argv):
            manager.quit()
            guard.close()
            return 2
        report_path = Path(sys.argv[index + 1])
        from mac_entry import verify_cocoa_window_order,verify_cocoa_interactions
        native_order = verify_cocoa_window_order(manager.edge, manager.shelf)
        native_order.update(verify_cocoa_interactions(manager.edge, manager.shelf))
        manager.edge.show_handle()
        manager.shelf.show_transient()
        def finish_cocoa_smoke():
            result = dict(ok=bool(manager.shelf.isVisible()),
                          qt_platform=app.platformName(), version=APP_VERSION,
                          edge_constructed=bool(manager.edge.isVisible() or manager.shelf.isVisible()),
                          shelf_constructed=bool(manager.shelves),
                          native_hotkeys=False, lan_listener=False,
                          cloud_session=False, startup_mutation=False,
                          native_window_order=native_order)
            report_path.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
            manager.quit()
        QTimer.singleShot(2200, finish_cocoa_smoke)
    if sys.platform == 'darwin' and getattr(sys, 'frozen', False) and '--post-update-ready' in sys.argv:
        index = sys.argv.index('--post-update-ready')
        if index + 2 < len(sys.argv):
            marker = Path(sys.argv[index + 1])
            token = sys.argv[index + 2]
            if (marker.name == 'launch-ready' and marker.parent.name.startswith('.flickdock-update-')
                    and len(token) == 32 and all(character in '0123456789abcdef' for character in token)):
                # A successful Finder/open() return does not prove a Qt app
                # survived startup. The updater retains its rollback until
                # this callback runs from the live Cocoa event loop.
                QTimer.singleShot(1600, lambda: marker.write_text(token, encoding='ascii'))
    if "--preview" in sys.argv:
        manager.show_first()
        QTimer.singleShot(650, lambda: (manager.shelves[0].grab().save(str(Path.cwd() / "flickdock-preview.png")), manager.quit()))
    result = app.exec()
    guard.close()
    sys.exit(result)


if __name__ == "__main__":
    raise SystemExit(main())
