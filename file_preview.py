"""Selected-file preview and its six existing file actions."""
from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtCore import QUrl
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QFrame
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QSizePolicy
from content_stack import stack_pixmap
from flickdock_motion import ContentTile
from image_preview import ImagePreview
from media_preview import MediaPreview
from pathlib import Path
from shelf_file_tools import kind
from shelf_file_tools import text_snippet
from shelf_file_tools import TEXT_SUFFIXES
from transfer_visuals import CLOUD_STATUS_LABELS
from transfer_visuals import cloud_status_for_ref
from transfer_visuals import file_presence_label
from transfer_visuals import transfer_fraction
from transfer_visuals import transfer_phase_label
import os


class ElidedLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.full_text = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(False)
        self.setFixedHeight(23)
        self.setMinimumWidth(1)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def setText(self, text):
        self.full_text = str(text).replace("\n", " ").replace("\r", "")
        self.setAccessibleName(self.full_text)
        self._elide()

    def _elide(self):
        QLabel.setText(self, self.fontMetrics().elidedText(self.full_text, Qt.TextElideMode.ElideRight, max(1, self.width())))

    def resizeEvent(self, event):
        self._elide()
        super().resizeEvent(event)


class PreviewPane(QFrame):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.shelf=parent.shelf;self.popup=parent;self.current_path=None
        self._cloud_batch_checks=set()
        from text_preview import TextPreviewLoader
        self._text_loader=TextPreviewLoader(self);self._text_loader.ready.connect(self._text_ready)
        self._text_truncated=False
        from reference_widgets import build_preview
        build_preview(self,ElidedLabel,ImagePreview,MediaPreview,ContentTile)
        self._copy_feedback=QTimer(self);self._copy_feedback.setSingleShot(True)
        self._copy_feedback.timeout.connect(lambda:self.copy_button.set_glyph('copy'))
        self._path_copy_feedback=QTimer(self);self._path_copy_feedback.setSingleShot(True)
        self._path_copy_feedback.timeout.connect(lambda:self.copy_path_button.setText('复制路径'))
        self.copy_button.setEnabled(False)
        self.update_action_states()
        self._set_empty_state(True)

    def _set_empty_state(self,empty):
        """Show only a calm prompt until an item is selected."""
        from reference_widgets import css,colors
        for part in (self.preview_head,self.status_card,self.size_card,self.path_card,self.action_footer):
            part.setVisible(not empty)
        if empty:
            css(self.stack,'QStackedWidget{background:transparent;border:0;}')
        else:
            from native_ui_common import RADIUS_CARD_PX
            css(self.stack,*[f"QStackedWidget{{background:{colors(t)['card']};border:1px solid {colors(t)['border']};border-radius:{RADIUS_CARD_PX}px;}}" for t in ('dark','light')])
        self.empty_icon.update()

    def _playing_changed(self, playing):
        self.shelf.preview_playing=bool(playing)
        self.shelf.touch()

    def refresh_cloud_status(self):
        visible = bool(self.current_path) and not getattr(self, '_group_refs', ())
        self.cloud_badge.hide();self.cloud_action.setVisible(visible)
        if visible:
            lookup=getattr(getattr(self,'popup',None),'cloud_status',None)
            status=lookup(self.current_path) if callable(lookup) else cloud_status_for_ref(self.shelf,self.current_path)
            from reference_widgets import css,colors
            self.status_value.setText(file_presence_label(status))
            token='green' if status=='synced' else 'blue'
            css(self.status_value,*[f"color:{colors(theme)[token]};background:transparent;border:0;font:400 10px 'FlickDock UI';" for theme in ('dark','light')])
            sync=getattr(self.shelf.manager,'sync',None)
            self.cloud_badge.set_status(status)
            synced=status=='synced'
            self.cloud_action.set_glyph('cloud-remove' if synced else 'cloud-upload')
            self.cloud_action.set_glyph_tint(colors(self.shelf.manager.settings.theme)['red'] if synced else '#0A84FF')
            action='删除云端副本' if synced else '上传云端'
            self.cloud_action.setAccessibleName(action);self.cloud_action.setToolTip(action)
            busy_lookup=getattr(sync,'cloud_copy_busy_for_ref',None)
            busy=bool(callable(busy_lookup) and busy_lookup(self.current_path))
            self.cloud_action.setEnabled(bool(sync and sync.service.signed_in and not busy and status not in ('uploading','pending') and not self.shelf.content.is_pending(self.current_path)))
            from shelf_content import shelf_time_info
            label,stamp=shelf_time_info(self.shelf.content,self.current_path)
            self.upload_time.setText(label+'：'+stamp)
        else:self.status_value.clear();self.upload_time.clear()
        update=getattr(self,'update_action_states',None)
        if getattr(self,'_group_refs',()) and callable(update):update()

    def toggle_cloud_copy(self):
        ref=self.current_path;sync=getattr(self.shelf.manager,'sync',None)
        if not sync or ref not in self.shelf.files['permanent'] or self.shelf.content.is_pending(ref):return
        if sync.sync_status_for_ref(ref)=='synced':sync.delete_cloud_copy(ref)
        else:
            manager=self.shelf.manager
            revision=getattr(sync,'cloud_copy_revision',None)
            intent=revision(ref) if callable(revision) else None
            def checked():
                from shiboken6 import isValid
                if not isValid(self) or getattr(manager,'sync',None) is not sync:return
                if callable(revision) and revision(ref)!=intent:return
                busy=getattr(sync,'cloud_copy_busy_for_ref',lambda _:False)
                if ref in self.shelf.files['permanent'] and not self.shelf.content.is_pending(ref) and not busy(ref):
                    sync.retry_upload(ref)
            manager.check_cloud_capacity_for_upload(ref,checked)
        self.refresh_cloud_status()

    def update_action_states(self):
        for control in (self.remove_button,self.group_remove):
            if control._hold_started is not None and control.identity()!=control._hold_identity:
                control.cancel_hold()
        ref=self.current_path
        present=bool(ref and ref in self.shelf.display_refs())
        ready=present and not self.shelf.content.is_pending(ref)
        self.open_button.setEnabled(ready)
        self.rename_button.setEnabled(ready and not self.shelf.content.is_text(ref))
        self.copy_path_button.setEnabled(ready);self.send_control.setEnabled(ready);self.send_button.setEnabled(ready);self.send_menu_button.setEnabled(ready)
        self.zip_button.setEnabled(ready and self.shelf.archive_job is None and not self.shelf._archive_choosing)
        self.remove_button.setEnabled(present)
        refs=self.popup.chosen_refs()
        group_ready=bool(refs) and all(not self.shelf.content.is_pending(item) for item in refs)
        self.group_copy.setEnabled(group_ready)
        self.group_zip.setEnabled(group_ready and self.shelf.archive_job is None and not self.shelf._archive_choosing)
        self.group_send_control.setEnabled(group_ready);self.group_send.setEnabled(group_ready);self.group_send_menu.setEnabled(group_ready)
        self.group_remove.setEnabled(bool(refs))
        self.group_cloud_upload.setEnabled(bool(self._cloud_candidates('upload',refs)))
        self.group_cloud_remove.setEnabled(bool(self._cloud_candidates('remove',refs)))

    def _cloud_candidates(self,action,refs):
        sync=getattr(self.shelf.manager,'sync',None)
        if not sync or not getattr(getattr(sync,'service',None),'signed_in',False):return []
        if action=='upload' and not getattr(sync,'upload_enabled',True):return []
        lookup=getattr(sync,'sync_status_for_ref',None)
        if not callable(lookup):return []
        busy=getattr(sync,'cloud_copy_busy_for_ref',lambda _:False)
        result=[]
        for ref in dict.fromkeys(refs):
            if ref not in self.shelf.files['permanent'] or self.shelf.content.is_pending(ref) or busy(ref):continue
            status=lookup(ref)
            if action=='remove':
                if status=='synced':result.append(ref)
            elif status!='synced' and ref not in self._cloud_batch_checks:
                state=self.shelf.content.transfers.get(ref)
                if not state:
                    result.append(ref)
        return result

    def upload_selected_to_cloud(self):
        manager=self.shelf.manager;sync=getattr(manager,'sync',None)
        refs=self._cloud_candidates('upload',self.popup.chosen_refs())
        self._cloud_batch_checks.update(refs);self.update_action_states()
        from shiboken6 import isValid
        for ref in refs:
            revision=getattr(sync,'cloud_copy_revision',None)
            intent=revision(ref) if callable(revision) else None
            def checked(insufficient,ref=ref,revision=revision,intent=intent):
                if not isValid(self):return
                self._cloud_batch_checks.discard(ref)
                current=not callable(revision) or revision(ref)==intent
                if getattr(manager,'sync',None) is sync and insufficient is not None and current:
                    if insufficient:manager.show_cloud_capacity_notice([ref])
                    elif ref in self._cloud_candidates('upload',[ref]):sync.retry_upload(ref)
                self.update_action_states()
            manager.check_cloud_capacity([ref],checked,include_existing=True)

    def remove_selected_from_cloud(self):
        sync=getattr(self.shelf.manager,'sync',None)
        for ref in self._cloud_candidates('remove',self.popup.chosen_refs()):sync.delete_cloud_copy(ref)
        self.update_action_states()

    def _current_file_path(self):
        return self.file_path_for(self.current_path)

    def file_path_for(self,ref):
        if ref not in self.shelf.files['permanent'] or self.shelf.content.is_pending(ref):return None
        try:
            path=(self.shelf.content.materialize_pasted_text(ref,self.shelf.manager.settings.capture_dir)
                  if self.shelf.content.is_text(ref) else ref)
            if not Path(path).exists():raise FileNotFoundError('找不到文件：'+str(path))
        except OSError as error:
            self.info.setText('无法使用此文件：'+str(error));return None
        if self.shelf.content.is_text(ref):self.shelf.manager.save()
        return str(Path(path).absolute())

    def copy_path(self):
        path=self._current_file_path()
        if path is None:return
        QApplication.clipboard().setText(path)
        self.copy_path_button.setText('已复制');self._path_copy_feedback.start(850);self.shelf.touch()

    def zip_current(self):
        if self.current_path in self.shelf.files['permanent']:
            self.shelf.compress_refs([self.current_path])

    def send_current(self):
        self._send_other([self.current_path])

    def open_send_menu(self,refs,button):
        """Choose a same-account target or open the existing transfer workbench."""
        from PySide6.QtCore import QPoint
        from shelf_ui_common import AnimatedMenu,style_menu
        refs=tuple(ref for ref in refs if ref)
        if not refs:return
        existing=getattr(self,'_send_menu',None)
        if existing is not None and existing.isVisible():
            existing.close();self._send_menu_just_closed_on_button=None;return
        if getattr(self,'_send_menu_just_closed_on_button',None) is button:
            self._send_menu_just_closed_on_button=None;return
        if existing is not None:existing.deleteLater();self._send_menu=None
        menu=AnimatedMenu(self);style_menu(menu);self._send_menu=menu
        from native_ui_common import PopupChevronMotion
        if not hasattr(button,'_popup_chevron'):button._popup_chevron=PopupChevronMotion(button)
        button._popup_chevron.attach(menu)
        manager=self.shelf.manager;account=getattr(manager,'account',None);wan=getattr(manager,'wan',None)
        signed=bool(account and getattr(account,'signed_in',False))
        bound=(signed and wan and getattr(wan,'_bound_identity',None)==
               (getattr(account,'device_id',None),getattr(getattr(account,'api',None),'token',None)))
        peers=[peer for peer in wan.peers() if peer.get('account_device') is True
               and peer.get('account_device_id')!=getattr(account,'device_id',None)] if bound else []
        if not signed:message='登录后可选择同账号设备'
        elif not bound:message='正在连接同账号设备…'
        elif not peers:message='暂无同账号设备'
        else:message='同账号设备'
        heading=menu.addAction(message);heading.setEnabled(False)
        from PySide6.QtWidgets import QPushButton,QWidgetAction
        from reference_widgets import css,colors
        device_buttons={}
        for peer in peers:
            identifier=peer.get('id')
            if not identifier:continue
            nearby=getattr(wan,'nearby_peer',lambda _:None)(identifier)
            available=bool(nearby) or peer.get('online') is not False
            label=str(peer.get('name') or '未命名设备')+(' · 离线' if not available else '')
            action=QWidgetAction(menu);action.setText(label)
            control=QPushButton(label,menu);control.setMinimumWidth(190);control.setFixedHeight(32)
            control.setCursor(Qt.PointingHandCursor if available else Qt.ArrowCursor)
            control.setAccessibleName(label);control.setAccessibleDescription('在线' if available else '离线')
            css(control,*[f"QPushButton{{background:transparent;color:{colors(t)['green'] if available else ('#737373' if t=='dark' else '#A3A3A3')};border:0;border-radius:6px;padding:6px 12px;text-align:left;font:400 12px 'FlickDock UI';}} QPushButton:disabled{{color:{'#737373' if t=='dark' else '#A3A3A3'};}} QPushButton:hover:enabled,QPushButton[menuSelected=\"true\"]:enabled{{background:{colors(t)['hover']};}}" for t in ('dark','light')])
            action.setDefaultWidget(control);action.setEnabled(available);menu.addAction(action);device_buttons[action]=control
            action.triggered.connect(lambda _checked=False,target=identifier:self._send_to_device(refs,target))
            control.clicked.connect(lambda _checked=False,item=action:(item.trigger(),menu.close()))
        def highlight(item):
            for action,control in device_buttons.items():
                active=action is item
                if bool(control.property('menuSelected'))!=active:
                    control.setProperty('menuSelected',active);control.style().unpolish(control);control.style().polish(control);control.update()
        menu.hovered.connect(highlight)
        menu.addSeparator()
        fallback=menu.addAction('其他设备…')
        fallback.triggered.connect(lambda:self._send_other(refs))
        def closed():
            from PySide6.QtGui import QCursor
            if button.rect().contains(button.mapFromGlobal(QCursor.pos())):
                self._send_menu_just_closed_on_button=button
                QTimer.singleShot(250,self,lambda:setattr(self,'_send_menu_just_closed_on_button',None))
            self.shelf._menu_open=False
            if getattr(self,'_send_menu',None) is menu:self._send_menu=None
            menu.deleteLater();self.shelf.touch()
        menu.aboutToHide.connect(closed)
        self.shelf._menu_open=True
        menu.adjustSize();menu.popup(button.mapToGlobal(QPoint(button.width()-menu.sizeHint().width(),-menu.sizeHint().height()-4)))

    def _paths_for_send(self,refs):
        paths=[]
        for ref in refs:
            path=self.file_path_for(ref)
            if path is None:
                if self.group_view.isVisible():self.group_title.setText('部分所选文件暂不可用')
                return []
            paths.append(path)
        return paths

    def _send_to_device(self,refs,identifier):
        paths=self._paths_for_send(refs)
        if paths:self.shelf.manager.send_to_account_device(identifier,paths)

    def _send_other(self,refs):
        paths=self._paths_for_send(refs)
        if paths:self.shelf.manager.queue_transfer_paths('lan',paths)

    def remove_current(self):
        if self.current_path in self.shelf.display_refs():
            self.shelf.remove_path('permanent',self.current_path)

    def _poster_ready(self, path, image):
        self.shelf.surface_effect.invalidate_source()
        if not image.isNull():
            # This is a thumbnail cache, separate from the visible full-quality
            # video surface. Bound both dimensions and total native image bytes.
            poster=image.scaled(512,512,Qt.KeepAspectRatio,Qt.SmoothTransformation) if max(image.width(),image.height())>512 else QImage(image)
            self.shelf.preview_cache.pop(path,None);self.shelf.preview_cache[path]=poster
            from reference_widgets import invalidate_thumbnail
            invalidate_thumbnail(self.shelf,path)
            while (len(self.shelf.preview_cache)>100 or
                   sum(value.sizeInBytes() for value in self.shelf.preview_cache.values())>16*1024*1024):
                self.shelf.preview_cache.pop(next(iter(self.shelf.preview_cache)))
            if self.popup.isVisible(): self.popup.update_item_icon(path)
            self.shelf.zones['permanent'].refresh()

    def _show_image(self,path):
        if not self.image.set_source(path):return False
        self.stack.setCurrentWidget(self.image)
        return True

    def _image_source_ready(self,path,success):
        self.shelf.surface_effect.invalidate_source()
        ref=self.current_path
        if not ref or getattr(self,'_group_refs',()):return
        expected=self.shelf.content.metadata.get(ref,{}).get('preview_image') or ref
        if os.path.normcase(os.path.abspath(expected))!=os.path.normcase(os.path.abspath(path)):return
        if success:self.stack.setCurrentWidget(self.image)
        elif self.shelf.content.text(ref) is not None:
            self.text.setPlainText(self.shelf.content.text(ref));self.stack.setCurrentWidget(self.text)
        else:
            self.icon.set_content('file',pixmap=self.shelf.item_pixmap(ref,128,dpr=self.devicePixelRatioF()))
            self.stack.setCurrentWidget(self.icon)

    def clear_preview(self):
        self._text_loader.cancel();self._text_truncated=False
        if self.current_path is None and self.stack.currentWidget() is self.empty_preview:
            self._set_empty_state(True);return
        self.shelf.surface_effect.invalidate_source()
        self.current_path=None;self._group_refs=();self._pending_preview=False
        self.media.release_source();self.image.clear_source();self.text.clear()
        self.icon.set_content('empty');self.group_art.clear();self.group_view.hide()
        self._copy_feedback.stop();self._path_copy_feedback.stop()
        self.copy_button.set_glyph('copy');self.copy_button.setEnabled(False)
        self.copy_path_button.setText('复制路径')
        for item in (self.name,self.info,self.upload_time,self.path_label,self.status_value,self.transfer_meta):item.setText('')
        self.transfer_row.hide();self.transfer_overlay.hide()
        self.stack.setCurrentWidget(self.empty_preview)
        self._set_empty_state(True)
        self.update_action_states()

    def show_path(self, ref, force=False):
        if self.current_path==ref and not force and not getattr(self,'_group_refs',()): return
        self._text_loader.cancel();self._text_truncated=False
        self._set_empty_state(False)
        self.shelf.surface_effect.invalidate_source()
        self._group_refs=()
        self.group_view.hide()
        self.media.stop()
        self.current_path=ref
        metadata=self.shelf.content.metadata.get(ref,{})
        self.popup._preview_metadata={key:metadata.get(key) for key in ('preview_image','text_file')}
        self.popup._preview_metadata['text']=self.shelf.content.texts.get(ref)
        self._path_copy_feedback.stop();self.copy_path_button.setText('复制路径')
        self.path_label.setText('原生文字' if self.shelf.content.is_text(ref) else ref)
        self.update_action_states()
        self.refresh_cloud_status()
        self.copy_button.set_glyph("copy")
        self.name.setText(self.shelf.content.title(ref))
        self.update_transfer_status()
        if self.shelf.content.is_pending(ref):
            self.icon.set_content('file',pixmap=self.shelf.item_pixmap(ref,128,dpr=self.devicePixelRatioF()))
            self.stack.setCurrentWidget(self.icon);self.copy_button.setEnabled(False)
            self._pending_preview=False
            return
        self.copy_button.setEnabled(True)
        # Prepare the same preview surface used at rest, while leaving native
        # decoder setup until the compositor has landed.
        category=self.shelf.content.category(ref)
        self._pending_preview=(self.shelf.content_transition_active() and
            category not in ('images','text','links'))
        if self._pending_preview:
            if category in ('video','audio') and MediaPreview.can_preview(ref):
                self.media.set_source(ref,poster=self.shelf.preview_cache.get(ref),defer=True)
                self.stack.setCurrentWidget(self.media)
            else:
                self.icon.set_content('file',pixmap=self.shelf.item_pixmap(ref,128,dpr=self.devicePixelRatioF()))
                self.stack.setCurrentWidget(self.icon)
            self.info.setText('')
            return
        raw=self.shelf.content.text(ref)
        meta=self.shelf.content.metadata.get(ref,{})
        if raw is not None:
            cover=meta.get('preview_image')
            if cover and self._show_image(cover):
                self.info.setText("网页 · "+QUrl(raw).host())
            else:
                self.text.setPlainText(raw)
                self.stack.setCurrentWidget(self.text)
                self.info.setText("正在解析…" if meta.get('loading') else "网页链接" if self.shelf.content.category(ref)=='links' else f"文字 · {len(raw)} 字符")
            self.copy_button.setToolTip("复制链接" if self.shelf.content.category(ref)=='links' else "复制文字")
            return
        source=Path(ref)
        from shortcut_preview import shortcut_details
        shortcut=shortcut_details(source)
        if shortcut is not None:
            if shortcut.get('url') and QApplication.instance().platformName() not in ('offscreen','minimal'):
                self.shelf.queue_link_previews([ref])
            cover=meta.get('preview_image')
            if cover and self._show_image(cover):pass
            else:
                self.text.setPlainText(shortcut['description']);self.stack.setCurrentWidget(self.text)
        elif self.shelf.content.category(ref)=='images' and self._show_image(ref):
            self.info.setText("矢量图像" if source.suffix.lower() in ('.svg','.svgz') else kind(ref)+" 图像")
        elif self.shelf.content.category(ref) in ('video','audio') and MediaPreview.can_preview(ref):
            self.media.set_source(ref,poster=self.shelf.preview_cache.get(ref))
            self.stack.setCurrentWidget(self.media)
            self.info.setText("视频" if self.shelf.content.category(ref)=='video' else "音频")
        elif source.suffix.lower() in TEXT_SUFFIXES:
            self.text.setPlainText('正在读取文本预览…');self.stack.setCurrentWidget(self.text)
            self.copy_button.setEnabled(False)
            self._text_loader.request(ref)
        else:
            self.icon.set_content("file",pixmap=self.shelf.item_pixmap(ref,128,dpr=self.devicePixelRatioF()))
            self.stack.setCurrentWidget(self.icon)
            try: size=source.stat().st_size if source.is_file() else 0
            except OSError: size=0
            self.info.setText(kind(ref)+" · "+(f"{size/1024:.1f} KB" if size<1024*1024 else f"{size/(1024*1024):.1f} MB"))
        from reference_widgets import item_detail
        self.info.setText(item_detail(self.shelf,ref))
        self.copy_button.setToolTip("复制文字内容" if self.stack.currentWidget() is self.text else "复制文件")

    def _text_ready(self,ref,value):
        if ref!=self.current_path or value is None:return
        self.copy_button.setEnabled(True)
        if value.get('error'):
            self.text.setPlainText('暂时无法读取文本预览，请打开原文件。');return
        self._text_truncated=value['truncated'];self.text.setPlainText(value['text'])
        if self._text_truncated:
            self.info.setText('仅预览前 64 KB · '+getattr(self.info,'full_text',self.info.text()))
            self.copy_button.setToolTip('复制完整原文件')

    def update_transfer_status(self):
        if getattr(self,'_group_refs',()):
            self.transfer_row.hide();self.transfer_overlay.hide();return
        state=self.shelf.content.transfers.get(self.current_path)
        self.transfer_row.setVisible(bool(state))
        if not state:
            self.transfer_overlay.hide();self.transfer_meta.clear();return
        copying=state.get('phase')=='copying'
        receiving=self.shelf._receiving_transfer(state)
        self.transfer_overlay.setGeometry(self.stack.rect());self.transfer_overlay.setVisible(copying or receiving)
        if copying or receiving:self.transfer_overlay.raise_();self.transfer_overlay.update()
        total,received=state.get('total',0),state.get('received',0)
        waiting_remote=state.get('remote') and state.get('phase') in ('remote','preparing')
        self.transfer_progress.setVisible(not waiting_remote)
        self.transfer_progress.setRange(0,1000 if total else 0)
        if total:self.transfer_progress.setValue(round(1000*min(1,received/total)))
        fraction=transfer_fraction(state)
        progress=f'{round(fraction*100)}% · ' if fraction is not None else ''
        self.transfer_meta.setText(progress+self.shelf.transfer_rates.remaining_text(self.current_path,state))
        self.cancel_transfer.setText('取消复制' if copying else '取消下载' if receiving else '取消上传')
        self.cancel_transfer.setEnabled(True)
        if self.shelf.content.is_pending(self.current_path):
            self.info.setText(transfer_phase_label(state) if state.get('phase') in ('verifying','retrying','validating')
                              else '另一台设备正在上传' if state.get('remote') and state.get('phase')!='downloading'
                              else '正在下载原文件' if state.get('phase')=='downloading'
                              else transfer_phase_label(state))

    def show_group(self,refs):
        refs=tuple(refs)
        if getattr(self,'_group_refs',())==refs: return
        self._set_empty_state(False)
        self.shelf.surface_effect.invalidate_source()
        self.media.stop();self._pending_preview=False;self._group_refs=refs;self.current_path=refs[-1]
        self.refresh_cloud_status()
        self.copy_button.setEnabled(True);self.transfer_row.hide();self.transfer_overlay.hide()
        self.name.setText(f'{len(refs)} 项已选');self.info.setText('可一起拖出或打包 ZIP')
        self.icon.set_content('file',pixmap=stack_pixmap(self.shelf.content,refs,128,dpr=self.devicePixelRatioF(),preview_cache=self.shelf.preview_cache))
        self.stack.setCurrentWidget(self.icon);self.copy_button.setToolTip('复制所选内容')
        self.group_title.setText(f'已选择 {len(refs)} 项')
        self.group_art.setPixmap(stack_pixmap(self.shelf.content,refs,72,dpr=self.devicePixelRatioF(),preview_cache=self.shelf.preview_cache))
        self.group_view.setGeometry(self.rect().adjusted(1,0,0,0));self.group_view.show();self.group_view.raise_()

    def resizeEvent(self,event):
        super().resizeEvent(event)
        self.group_view.setGeometry(self.rect().adjusted(1,0,0,0))

    def resume_preview(self):
        if (not getattr(self,'_pending_preview',False) or self.shelf.content_transition_active()
                or not self.shelf.expanded or not self.shelf.isVisible()): return
        ref=self.current_path
        if ref in self.shelf.files['permanent']:
            self.show_path(ref,force=True)

    def copy_current(self):
        if not self.current_path: return
        if getattr(self,'_group_refs',()): self.shelf.copy_items(self._group_refs)
        elif self.stack.currentWidget() is self.text and not self._text_truncated: QApplication.clipboard().setText(self.text.toPlainText())
        else: self.shelf.copy_items([self.current_path])
        self.copy_button.set_glyph("check"); self.copy_button.spring.kick({"scale":1.8})
        self._copy_feedback.start(850)
        self.shelf.touch()

    def hideEvent(self,event):
        if self.current_path and self.stack.currentWidget() is self.media:self._pending_preview=True
        self.media.stop()
        super().hideEvent(event)
