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
from transfer_visuals import CLOUD_STATUS_LABELS
from transfer_visuals import cloud_status_for_ref
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
        from reference_widgets import build_preview
        build_preview(self,ElidedLabel,ImagePreview,MediaPreview,ContentTile)
        self._copy_feedback=QTimer(self);self._copy_feedback.setSingleShot(True)
        self._copy_feedback.timeout.connect(lambda:self.copy_button.set_glyph('copy'))
        self._path_copy_feedback=QTimer(self);self._path_copy_feedback.setSingleShot(True)
        self._path_copy_feedback.timeout.connect(lambda:self.copy_path_button.setText('复制路径'))
        self.copy_button.setEnabled(False)
        self.update_action_states()

    def _playing_changed(self, playing):
        self.shelf.preview_playing=bool(playing)
        self.shelf.touch()

    def refresh_cloud_status(self):
        visible = bool(self.current_path) and not getattr(self, '_group_refs', ())
        self.cloud_badge.setVisible(visible);self.cloud_action.setVisible(visible)
        if visible:
            lookup=getattr(getattr(self,'popup',None),'cloud_status',None)
            status=lookup(self.current_path) if callable(lookup) else cloud_status_for_ref(self.shelf,self.current_path)
            from reference_widgets import css,colors
            self.status_value.setText(CLOUD_STATUS_LABELS.get(status,'仅保存在本机'))
            token={'peer':'link','synced':'green','uploading':'link','pending':'link','local':'secondary','paused':'orange','error':'red'}.get(status,'secondary')
            css(self.status_value,*[f"color:{colors(theme)[token]};background:transparent;border:0;font:11px 'HarmonyOS Sans SC';" for theme in ('dark','light')])
            sync=getattr(self.shelf.manager,'sync',None)
            self.cloud_badge.set_status(status)
            synced=status=='synced'
            self.cloud_action.set_glyph('cloud-remove' if synced else 'cloud-upload')
            self.cloud_action.set_glyph_tint(colors(self.shelf.manager.settings.theme)['red'] if synced else '#0A84FF')
            action='删除云端副本' if synced else '上传云端'
            self.cloud_action.setAccessibleName(action);self.cloud_action.setToolTip(action)
            busy=bool(sync and any(e.get('ref')==self.current_path and e['id'] in sync.cloud_copy_operations for e in (sync.state or {}).get('entries',{}).values()))
            self.cloud_action.setEnabled(bool(sync and sync.service.signed_in and not busy and status not in ('uploading','pending') and not self.shelf.content.is_pending(self.current_path)))
            stamp=sync.upload_time_for_ref(self.current_path) if sync is not None else None
            if stamp:
                from datetime import datetime
                self.upload_time.setText('上传时间：'+datetime.fromisoformat(stamp).astimezone().strftime('%Y-%m-%d %H:%M'))
            else:self.upload_time.setText('上传时间：暂无记录' if status=='synced' else '未上传')
        else:self.status_value.clear();self.upload_time.clear()

    def toggle_cloud_copy(self):
        ref=self.current_path;sync=getattr(self.shelf.manager,'sync',None)
        if not sync or ref not in self.shelf.files['permanent'] or self.shelf.content.is_pending(ref):return
        if sync.sync_status_for_ref(ref)=='synced':sync.delete_cloud_copy(ref)
        else:
            self.shelf.manager.check_cloud_capacity_for_upload(ref,lambda:sync.retry_upload(ref))
        self.refresh_cloud_status()

    def update_action_states(self):
        ref=self.current_path
        present=bool(ref and ref in self.shelf.display_refs())
        ready=present and not self.shelf.content.is_pending(ref)
        self.open_button.setEnabled(ready)
        self.rename_button.setEnabled(ready and not self.shelf.content.is_text(ref))
        self.copy_path_button.setEnabled(ready);self.send_button.setEnabled(ready)
        self.zip_button.setEnabled(ready and self.shelf.archive_job is None and not self.shelf._archive_choosing)
        self.remove_button.setEnabled(present)
        refs=self.popup.chosen_refs()
        group_ready=bool(refs) and all(not self.shelf.content.is_pending(item) for item in refs)
        self.group_copy.setEnabled(group_ready)
        self.group_zip.setEnabled(group_ready and self.shelf.archive_job is None and not self.shelf._archive_choosing)
        self.group_send.setEnabled(group_ready)
        self.group_remove.setEnabled(bool(refs))

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
        path=self._current_file_path()
        if path is not None:self.shelf.manager.queue_transfer_paths('lan',[path])

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
        if self.current_path is None and self.stack.currentWidget() is self.empty_preview:return
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
        self.update_action_states()

    def show_path(self, ref, force=False):
        if self.current_path==ref and not force and not getattr(self,'_group_refs',()): return
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
        elif text_snippet(ref,1) is not None:
            try:
                data=source.read_bytes()
                try: text=data.decode('utf-8-sig')
                except UnicodeDecodeError: text=data.decode('gb18030',errors='replace')
            except OSError: text=""
            self.text.setPlainText(text); self.stack.setCurrentWidget(self.text)
            self.info.setText(f"{kind(ref)} · {len(text)} 字符")
        else:
            self.icon.set_content("file",pixmap=self.shelf.item_pixmap(ref,128,dpr=self.devicePixelRatioF()))
            self.stack.setCurrentWidget(self.icon)
            try: size=source.stat().st_size if source.is_file() else 0
            except OSError: size=0
            self.info.setText(kind(ref)+" · "+(f"{size/1024:.1f} KB" if size<1024*1024 else f"{size/(1024*1024):.1f} MB"))
        from reference_widgets import item_detail
        self.info.setText(item_detail(self.shelf,ref))
        self.copy_button.setToolTip("复制文字内容" if self.stack.currentWidget() is self.text else "复制文件")

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
        self.shelf.surface_effect.invalidate_source()
        self.media.stop();self._pending_preview=False;self._group_refs=refs;self.current_path=refs[-1]
        self.refresh_cloud_status()
        self.copy_button.setEnabled(True);self.transfer_row.hide();self.transfer_overlay.hide()
        self.name.setText(f'{len(refs)} 项已选');self.info.setText('可一起拖出或打包 ZIP')
        self.icon.set_content('file',pixmap=stack_pixmap(self.shelf.content,refs,128,dpr=self.devicePixelRatioF(),preview_cache=self.shelf.preview_cache))
        self.stack.setCurrentWidget(self.icon);self.copy_button.setToolTip('复制所选内容')
        self.group_title.setText(f'已选择 {len(refs)} 项')
        self.group_art.setPixmap(stack_pixmap(self.shelf.content,refs,72,dpr=self.devicePixelRatioF(),preview_cache=self.shelf.preview_cache))
        self.group_view.setGeometry(self.rect());self.group_view.show();self.group_view.raise_()

    def resizeEvent(self,event):
        super().resizeEvent(event)
        self.group_view.setGeometry(self.rect())

    def resume_preview(self):
        if (not getattr(self,'_pending_preview',False) or self.shelf.content_transition_active()
                or not self.shelf.expanded or not self.shelf.isVisible()): return
        ref=self.current_path
        if ref in self.shelf.files['permanent']:
            self.show_path(ref,force=True)

    def copy_current(self):
        if not self.current_path: return
        if getattr(self,'_group_refs',()): self.shelf.copy_items(self._group_refs)
        elif self.stack.currentWidget() is self.text: QApplication.clipboard().setText(self.text.toPlainText())
        else: self.shelf.copy_items([self.current_path])
        self.copy_button.set_glyph("check"); self.copy_button.spring.kick({"scale":1.8})
        self._copy_feedback.start(850)
        self.shelf.touch()

    def hideEvent(self,event):
        if self.current_path and self.stack.currentWidget() is self.media:self._pending_preview=True
        self.media.stop()
        super().hideEvent(event)
