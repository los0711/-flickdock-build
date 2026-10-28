"""Real shelf transfer presentation and owned-file cleanup, Qt offscreen only."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'

from contextlib import ExitStack,contextmanager
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication,QEvent,QMimeData,QPoint,QRect,QTimer,Qt,QUrl
from PySide6.QtGui import QColor,QDesktopServices,QImage,QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QWidget

import app_settings
import capture_files
import flickdock as subject
import ui_theme
from content_grid import IconItem
from transfer_visuals import TransferIconLabel,TransferRateTracker,transfer_phase_label

APP=QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
subject.load_font(APP)


class TransferRateTests(unittest.TestCase):
    def test_eta_uses_observed_progress_and_disappears_when_stalled_or_reset(self):
        clock=[0.0];tracker=TransferRateTracker(clock=lambda:clock[0])
        state={'phase':'uploading','received':0,'total':16*1024*1024}
        tracker.observe({'item':state})
        self.assertEqual(tracker.remaining_text('item',state),'计算中')
        clock[0]=1.0;state['received']=4*1024*1024;tracker.observe({'item':state})
        self.assertEqual(tracker.remaining_text('item',state),'剩余约 3 秒')
        self.assertEqual(tracker.remaining_text('__aggregate__',state),'剩余约 3 秒')
        clock[0]=8.0;tracker.observe({'item':state})
        self.assertEqual(tracker.remaining_text('item',state),'等待传输')
        state['received']=0;tracker.observe({'item':state})
        self.assertEqual(tracker.remaining_text('item',state),'计算中')
        state['phase']='queued';self.assertEqual(tracker.remaining_text('item',state),'等待开始')
        self.assertEqual(transfer_phase_label({'phase':'copying'}),'正在复制')
        self.assertEqual(transfer_phase_label({'phase':'downloading'}),'正在下载')
        self.assertEqual(transfer_phase_label({'phase':'uploading'}),'正在上传')
        state['phase']='verifying'
        self.assertEqual(transfer_phase_label(state),'等待云端确认')
        self.assertEqual(tracker.remaining_text('item',state),'等待云端确认')
        state['phase']='retrying'
        self.assertEqual(transfer_phase_label(state),'等待重试')
        self.assertEqual(tracker.remaining_text('item',state),'等待重试')
        state['phase']='validating'
        self.assertEqual(transfer_phase_label(state),'校验原文件')
        self.assertEqual(tracker.remaining_text('item',state),'校验原文件')

    def test_queue_wait_and_retry_delay_do_not_create_false_eta(self):
        clock=[0.0];tracker=TransferRateTracker(clock=lambda:clock[0])
        state={'phase':'queued','received':0,'total':16*1024*1024}
        tracker.observe({'item':state})
        clock[0]=20.0;state.update(phase='uploading',received=4*1024*1024)
        tracker.observe({'item':state})
        self.assertEqual(tracker.remaining_text('item',state),'计算中')
        clock[0]=21.0;state['received']=8*1024*1024;tracker.observe({'item':state})
        self.assertEqual(tracker.remaining_text('item',state),'剩余约 2 秒')
        clock[0]=30.0;tracker.observe({'item':state})
        self.assertEqual(tracker.remaining_text('item',state),'等待传输')
        state['received']=9*1024*1024;tracker.observe({'item':state})
        self.assertEqual(tracker.remaining_text('item',state),'计算中')


def flush():
    APP.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)


def wait(predicate,timeout=5):
    until=time.monotonic()+timeout
    while not predicate() and time.monotonic()<until:flush();QTest.qWait(5)
    if not predicate():raise AssertionError('Background transfer fixture did not settle')


def mime(path):
    value=QMimeData();value.setUrls([QUrl.fromLocalFile(str(path))]);return value


def opaque_artwork(widget):
    source=widget.grab().toImage()
    result=QImage(source.size(),QImage.Format.Format_ARGB32)
    result.setDevicePixelRatio(source.devicePixelRatio());result.fill(QColor('#ffffff'))
    painter=QPainter(result);painter.drawImage(QPoint(),source);painter.end()
    return result


class TransferUITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-transfer-ui-')
        self.root=Path(self.temp.name);self.source=self.root/'source';self.source.mkdir()
        self.saved=self.root/'saved';self.saved.mkdir()
        self.patches=ExitStack();self.barriers=[]
        self.patches.enter_context(patch.object(app_settings,'_startup_runtime_allowed',return_value=False))
        screen=SimpleNamespace(availableGeometry=lambda:QRect(0,0,1920,1080),devicePixelRatio=lambda:1)
        self.patches.enter_context(patch.object(subject.QGuiApplication,'screenAt',return_value=screen))
        self.patches.enter_context(patch.object(subject.QGuiApplication,'primaryScreen',return_value=screen))
        self.settings=app_settings.SettingsStore(self.root/'profile')
        self.settings.update(capture_mode='copy',capture_dir=str(self.saved),view_mode='icons')
        self.manager=SimpleNamespace(motion=False,settings=self.settings,save=lambda:None,update_handles=lambda:None,
            activate_shelf=lambda _:None,_quitting=False,outgoing_drag=False)
        self.shelf=subject.ShelfWindow(self.manager,1);self.shelf.pinned=True
        self.manager.shelf=self.shelf
        self.shelf._show_at(QPoint(30,30),'manual',QPoint());flush()
    def tearDown(self):
        for release in self.barriers:release.set()
        self.shelf._copy_epoch+=1
        for job in tuple(self.shelf.copy_jobs):job.cancel()
        wait(lambda:not self.shelf.copy_jobs and not self.shelf._delete_jobs)
        self.shelf._transfer_timer.stop();self.shelf._image_timer.stop();self.shelf._intake_timer.stop()
        self.shelf.resolver.close();self.shelf.image_cache.close()
        if self.shelf.popup:self.shelf.popup.preview.media.release_source()
        self.shelf.presentation.stop();self.shelf.geometry_morph.stop()
        self.shelf.hide();self.shelf.deleteLater();flush();self.patches.close();self.temp.cleanup()
    def overview(self):
        self.shelf.show_contents();flush()
        self.shelf.popup.resume_content();flush()
        return self.shelf.popup
    def test_empty_overview_has_full_width_drop_target_and_disabled_bulk_actions(self):
        popup=self.overview()
        self.assertTrue(popup.empty_state.isVisible())
        self.assertFalse(popup.scroll.isVisible())
        self.assertEqual(popup.empty_title.text(),'搁架是空的')
        self.assertFalse(popup.zip_button.isEnabled())
        self.assertFalse(popup.zip_all_button.isEnabled())
        self.assertFalse(popup.clear_button.isEnabled())
        self.assertIs(popup.category_buttons['all'].parent(),popup.sidebar)
        self.assertGreater(popup.empty_state.width(),300)
        self.assertTrue(all(button.property('flatToolbar') for button in
            (self.shelf.cloud_button,self.shelf.lan_button,self.shelf.pin_button,
             self.shelf.expand_button,self.shelf.menu_button,self.shelf.close_button)))

    def test_mini_count_pill_hover_never_opens_without_a_click(self):
        zone=self.shelf.zones['permanent']
        with patch.object(self.shelf,'show_contents') as expand:
            zone._track_pill_hover(zone.caption.geometry().center())
            zone._track_pill_hover(QPoint(4,4))
            QTest.qWait(310);flush()
            expand.assert_not_called()
            zone._track_pill_hover(zone.caption.geometry().center())
            QTest.qWait(310);flush()
            expand.assert_not_called()
    def file(self,name='original.bin',size=256*1024):
        path=self.source/name;path.write_bytes((bytes(range(256))*(size//256+1))[:size]);return path
    def pending(self,name='正在传输.mp4',*,remote=False,received=25,total=100):
        ref='flickdock:transfer:fixture-'+str(len(self.shelf.content.pending))
        self.shelf.content.pending[ref]=dict(name=name,kind='file')
        self.shelf.content.transfers[ref]=dict(id='fixture-item',phase='uploading',remote=remote,received=received,total=total)
        self.shelf.refresh();self.shelf.transfers_updated();flush();return ref
    def test_held_copy_keeps_file_artwork_and_shows_small_progress_badge(self):
        path=self.file('原始图片.png',3*1024*1024)
        original_reader=capture_files._source_reader;entered,release=threading.Event(),threading.Event()
        self.barriers.append(release)
        class Reader:
            def __init__(self,stream):self.stream=stream;self.reads=0
            def read(self,count):
                self.reads+=1
                if self.reads==2:entered.set();assert release.wait(5)
                return self.stream.read(count)
        @contextmanager
        def reader(source):
            with original_reader(source) as stream:yield Reader(stream)
        with patch.object(capture_files,'_source_reader',side_effect=reader):
            self.assertTrue(self.shelf.receive_mime('permanent',mime(path)));wait(entered.is_set)
            self.assertEqual(self.shelf.files['permanent'],[])
            self.assertEqual(len(self.shelf.display_refs()),1)
            ref=self.shelf.display_refs()[0]
            self.assertTrue(self.shelf.content.is_pending(ref));self.assertEqual(self.shelf.content.title(ref),path.name)
            state=self.shelf.content.transfers[ref]
            wait(lambda:state['received']>0)
            self.assertEqual(state['phase'],'copying');self.assertGreater(state['received'],0)
            self.assertEqual(state['total'],path.stat().st_size)
            partial=list((self.saved/'imports').glob('capture-*/*'))
            self.assertEqual(len(partial),1);self.assertGreater(partial[0].stat().st_size,0)
            compact=self.shelf.zones['permanent'].icon
            active_compact=opaque_artwork(compact)
            removed=self.shelf.content.transfers.pop(ref);normal_compact=opaque_artwork(compact);self.shelf.content.transfers[ref]=removed
            self.assertNotEqual(active_compact,normal_compact,'compact shelf needs a small progress badge')
            popup=self.overview();self.assertNotIn(ref,popup.visible_refs())
            popup.set_category('uploading');popup.preview.show_path(ref,force=True);flush()
            item=next(item for item in popup.rows_body.findChildren(IconItem) if item.ref==ref)
            active=opaque_artwork(item)
            removed=self.shelf.content.transfers.pop(ref);normal=opaque_artwork(item);self.shelf.content.transfers[ref]=removed
            self.assertNotEqual(active,normal,'transfer tile needs a small progress badge')
            candidates=[]
            ratio=normal.devicePixelRatio()
            # The artwork is centered in an adaptive-width cell. Inspect its
            # actual paint area, rather than the old fixed148px card origin.
            icon_left=(item.width()-44)//2
            for y in range(round(24*ratio),round(43*ratio)):
                for x in range(round((icon_left+8)*ratio),round((icon_left+32)*ratio)):
                    color=normal.pixelColor(x,y);sat=max(color.red(),color.green(),color.blue())-min(color.red(),color.green(),color.blue())
                    candidates.append((sat,x,y))
            saturation,x,y=max(candidates);self.assertGreater(saturation,40)
            self.assertEqual(active.pixelColor(x,y),normal.pixelColor(x,y),'upload must preserve icon colors')
            blue=sum(1 for y in range(active.height()) for x in range(active.width())
                     if (lambda c:c.blue()>180 and c.blue()-c.red()>85 and c.blue()-c.green()>25)(active.pixelColor(x,y)))
            self.assertGreater(blue,20,'small upload arc must remain visible beside artwork')
            self.assertEqual(popup.preview.cancel_transfer.text(),'取消复制')
            self.assertFalse(popup.preview.copy_button.isEnabled())
            QTest.mouseClick(popup.preview.cancel_transfer,Qt.MouseButton.LeftButton)
            self.assertTrue(next(iter(self.shelf.copy_jobs))._cancel_event.is_set())
            release.set();wait(lambda:not self.shelf.copy_jobs)
        self.assertEqual(self.shelf.display_refs(),[]);self.assertFalse(self.shelf.content.transfers)
        self.assertEqual(list((self.saved/'imports').iterdir()),[])
        self.assertTrue(path.is_file());self.assertEqual(path.stat().st_size,3*1024*1024)
    def test_copy_removal_and_clear_delete_only_owned_bytes_asynchronously(self):
        paths=[self.file('第一份.bin'),self.file('第二份.bin')]
        self.shelf.import_paths('permanent',list(map(str,paths)));wait(lambda:not self.shelf.copy_jobs)
        copies=list(map(Path,self.shelf.files['permanent']))
        self.assertEqual(len(copies),2);self.assertTrue(all(path.parent==copies[0].parent for path in copies))
        foreign=self.saved/'imports'/'foreign-owner';foreign.mkdir();(foreign/'keep').write_bytes(b'preserve')
        entered,release=threading.Event(),threading.Event();self.barriers.append(release)
        original=subject.delete_owned_copy;gui=threading.get_ident()
        def held(spec,cancel):
            self.assertNotEqual(threading.get_ident(),gui)
            entered.set();assert release.wait(5);return original(spec,cancel)
        with patch.object(subject,'delete_owned_copy',side_effect=held):
            self.shelf.remove_path('permanent',str(copies[0]));wait(entered.is_set)
            self.assertNotIn(str(copies[0]),self.shelf.files['permanent'])
            self.assertTrue(copies[0].exists(),'disk cleanup runs on a worker')
            beats=[];QTimer.singleShot(0,lambda:beats.append(True));wait(lambda:bool(beats))
            release.set();wait(lambda:not self.shelf._delete_jobs)
        self.assertFalse(copies[0].exists());self.assertTrue(copies[1].exists())
        # Deletion must keep using the recorded old owner after save location changes.
        self.settings.update(capture_dir=str(self.root/'different-save-location'))
        self.shelf.add_paths('permanent',[str(paths[0])])
        self.shelf.remove_all('permanent');wait(lambda:not self.shelf._delete_jobs)
        self.assertFalse(copies[1].exists());self.assertFalse(copies[0].parent.exists())
        self.assertTrue(all(path.is_file() for path in paths))
        self.assertEqual((foreign/'keep').read_bytes(),b'preserve')
    def test_reference_removal_and_untrusted_copy_metadata_never_delete_original(self):
        path=self.file();self.settings.update(capture_mode='reference')
        self.assertTrue(self.shelf.receive_mime('permanent',mime(path)))
        self.assertEqual(self.shelf.files['permanent'],[str(path)])
        self.shelf.remove_path('permanent',str(path));wait(lambda:not self.shelf._delete_jobs)
        self.assertTrue(path.is_file())
        self.shelf.add_paths('permanent',[str(path)])
        self.shelf.content.metadata[str(path)]={'capture_mode':'copy','owned_root':str(self.saved)}
        self.shelf.remove_all('permanent');wait(lambda:not self.shelf._delete_jobs)
        self.assertTrue(path.is_file());self.assertEqual(path.stat().st_size,256*1024)
    def test_pending_remote_video_never_opens_player_file_drag_clipboard_or_archive(self):
        ref=self.pending(remote=True)
        with patch.object(subject.MediaPreview,'set_source',side_effect=AssertionError('Pending content cannot play')),\
             patch.object(QDesktopServices,'openUrl',side_effect=AssertionError('Pending content cannot open')),\
             patch.object(subject.QMessageBox,'warning',side_effect=AssertionError('Pending content cannot open modal errors')):
            popup=self.overview();self.assertNotIn(ref,popup.visible_refs())
            popup.set_category('uploading');popup.preview.show_path(ref,force=True)
            self.assertIs(popup.preview.stack.currentWidget(),popup.preview.icon)
            self.assertFalse(popup.preview.copy_button.isEnabled())
            self.assertIn('另一台设备',popup.preview.info.text())
            self.assertIsNone(popup.preview.media.player)
            preview_active=opaque_artwork(popup.preview.stack)
            state=self.shelf.content.transfers.pop(ref);preview_normal=opaque_artwork(popup.preview.stack)
            self.shelf.content.transfers[ref]=state
            self.assertNotEqual(preview_active,preview_normal,'right preview needs small transfer feedback')
            self.shelf.open_path(ref)
        payload=self.shelf.content.mime_for([ref]);self.assertFalse(payload.hasUrls());self.assertFalse(payload.hasText())
        with patch.object(subject,'QDrag',side_effect=AssertionError('Pending content cannot drag')),\
             patch.object(self.shelf,'choose_zip_path',side_effect=AssertionError('Pending content cannot archive')),\
             patch.object(QApplication,'clipboard',side_effect=AssertionError('Pending content cannot copy')):
            self.assertEqual(self.shelf.drag_files('permanent',[ref]),Qt.DropAction.IgnoreAction)
            self.assertFalse(self.shelf.compress_refs([ref]));self.shelf.copy_items([ref])
        popup.select_item(ref);self.assertFalse(popup.zip_button.isEnabled());self.assertFalse(popup.zip_all_button.isEnabled())
    def test_upload_filter_count_progress_updates_without_rebuilding_for_each_chunk(self):
        path=self.file();self.settings.update(capture_mode='reference');self.shelf.add_paths('permanent',[str(path)])
        pending=self.pending('云端图片.svg',remote=True,received=20,total=100)
        self.shelf.content.transfers[str(path)]={'id':'local-upload','phase':'uploading','received':40,'total':100}
        self.shelf.transfers_updated();popup=self.overview();popup.set_category('uploading');flush()
        self.assertEqual(set(popup.visible_refs()),{str(path),pending})
        self.assertEqual(popup.transfer_badge.text(),'2');self.assertTrue(popup.transfer_badge.isVisible())
        self.assertIn('2',popup.transfer_count.text())
        self.assertTrue(popup.transfer_summary.isVisible());self.assertEqual(popup.transfer_bar.value(),300)
        epoch=popup._build_epoch
        self.shelf.content.transfers[str(path)]['received']=80
        self.shelf.transfers_updated();flush()
        self.assertEqual(popup.transfer_bar.value(),500)
        self.assertEqual(popup._build_epoch,epoch,'chunk progress must repaint without layout rebuild')
        self.shelf.content.transfers.pop(str(path));self.shelf.content.pending.pop(pending);self.shelf.content.transfers.pop(pending)
        self.shelf.transfers_updated();flush()
        self.assertEqual(popup.visible_refs(),[]);self.assertFalse(popup.transfer_summary.isVisible())
        self.assertEqual(popup.category_buttons['uploading'].text(),'传输中')
        self.assertFalse(popup.transfer_badge.isVisible())
        popup.set_category('all');self.assertFalse(popup.transfer_strip.isVisible())

    def test_upload_count_lives_in_transfer_tab_and_footer_not_cloud_header(self):
        original_theme=ui_theme.current_theme()
        try:
            for theme in ('light','dark'):
                ui_theme.set_theme(theme);ui_theme.apply_application(APP,subject.FONT_FAMILY)
                for count in (1,123):
                    self.shelf.content.pending={f'flickdock:transfer:fixture-{index}':
                        dict(name=f'文件{index}.bin',kind='file') for index in range(count)}
                    self.shelf.content.transfers={ref:dict(id=ref,phase='uploading',received=0,total=100)
                                                  for ref in self.shelf.content.pending}
                    self.shelf.refresh();self.shelf.transfers_updated();popup=self.overview()
                    popup.set_category('all');flush()
                    self.assertFalse(hasattr(self.shelf,'upload_count'))
                    self.assertEqual(popup.transfer_badge.text(),'99+' if count>99 else str(count))
                    self.assertEqual(popup.category_buttons['uploading'].accessibleName(),f'传输中的项目，共 {count} 项')
                    self.assertEqual(popup.strip_title.text(),f'传输中 · {count} 项')
                    self.assertTrue(popup.transfer_strip.isVisible())
        finally:
            ui_theme.set_theme(original_theme);ui_theme.apply_application(APP,subject.FONT_FAMILY)

    def test_overview_footer_shows_one_compact_transfer_and_opens_the_full_list(self):
        refs=[self.pending(f'传输文件{index}.bin',received=index*10,total=100)
              for index in range(4)]
        popup=self.overview()
        self.assertEqual(popup.category,'all')
        self.assertEqual(popup.visible_refs(),[],'active transfers stay outside the completed list')
        self.assertTrue(popup.transfer_strip.isVisible())
        self.assertEqual(popup._strip_refs,refs[-1:])
        self.assertEqual(sum(row.isVisible() for row,*_ in popup.strip_rows),1)
        self.assertIn('+3',popup.strip_more.text())
        self.assertEqual(popup.strip_rows[0][3].value(),300)
        popup.setFixedWidth(330);popup.layout().activate();flush()
        self.assertTrue(popup.transfer_strip.isVisible())
        self.assertLessEqual(popup.transfer_strip.geometry().right(),popup.width())
        self.assertEqual(sum(row.isVisible() for row,*_ in popup.strip_rows),1)
        epoch=popup._build_epoch
        self.shelf.content.transfers[refs[-1]]['received']=65
        self.shelf.transfers_updated();flush()
        self.assertEqual(popup.strip_rows[0][3].value(),650)
        self.assertEqual(popup._build_epoch,epoch,'progress must not rebuild the overview')
        QTest.mouseClick(popup.strip_rows[0][1],Qt.MouseButton.LeftButton);flush()
        self.assertEqual(popup.category,'uploading')
        self.assertEqual(popup.primary_selection,refs[-1])
        self.assertFalse(popup.transfer_strip.isVisible(),'the full list does not repeat the footer')
        self.assertEqual(len(popup.visible_refs()),4)
        popup.set_category('all')
        self.shelf.content.transfers.clear();self.shelf.content.pending.clear()
        self.shelf.transfers_updated();flush()
        self.assertFalse(popup.transfer_strip.isVisible())

    def test_transfer_arriving_during_expand_appears_in_all_footer_at_settle(self):
        popup=self.overview()
        self.assertEqual(popup.category,'all')
        self.assertFalse(popup.transfer_strip.isVisible())
        self.shelf._transition_preparing=True
        self.shelf._morph_host_active=True
        refs=[]
        for index in range(3):
            ref='flickdock:transfer:during-expand-'+str(index)
            refs.append(ref)
            self.shelf.content.pending[ref]=dict(name=f'传输中-{index}.bin',kind='file')
            self.shelf.content.transfers[ref]=dict(id=str(index),phase='uploading',received=0,total=100)
        self.shelf.transfers_updated();flush()
        self.assertFalse(popup.transfer_strip.isVisible(),
                         'the hidden transition tree should not be laid out mid-frame')
        self.shelf._settle_morph(self.shelf.visual_geometry());flush()
        self.assertTrue(popup.transfer_strip.isVisible(),
                        'the All footer must reconcile at transition completion')
        self.assertEqual(popup._strip_refs,refs[-1:])
        self.assertIn('+2',popup.strip_more.text())
        self.assertEqual(sum(row.isVisible() for row,*_ in popup.strip_rows),1)

    def test_percent_and_estimated_time_update_in_footer_category_and_preview(self):
        clock=[0.0]
        self.shelf.transfer_rates=TransferRateTracker(clock=lambda:clock[0])
        ref=self.pending('正在上传的大文件.mov',received=0,total=16*1024*1024)
        popup=self.overview()
        self.assertIn('0%',popup.strip_rows[0][2].text())
        self.assertIn('计算中',popup.strip_rows[0][2].toolTip())
        popup.set_category('uploading');popup.preview.show_path(ref,force=True)
        self.assertIn('0%',popup.transfer_eta.text())
        self.assertIn('计算中',popup.preview.transfer_meta.text())
        epoch=popup._build_epoch
        clock[0]=1.0;self.shelf.content.transfers[ref]['received']=4*1024*1024
        self.shelf.transfers_updated();flush()
        self.assertEqual(popup._build_epoch,epoch)
        self.assertIn('25% · 剩余约 3 秒',popup.transfer_eta.text())
        self.assertIn('25% · 剩余约 3 秒',popup.preview.transfer_meta.text())
        popup.set_category('all')
        self.assertEqual(popup.strip_rows[0][2].text(),'25%')
        self.assertIn('剩余约 3 秒',popup.strip_rows[0][2].toolTip())

    def test_local_item_is_available_in_all_while_its_cloud_upload_runs(self):
        path=str(self.file('刚拖进来的原文件.mp4'))
        self.shelf.add_paths('permanent',[path])
        self.shelf.content.transfers[path]=dict(id='local-cloud-upload',phase='uploading',
                                                received=0,total=256*1024)
        self.shelf.transfers_updated()
        popup=self.overview()
        self.assertEqual(popup.category,'all')
        self.assertIn(path,popup.visible_refs())
        self.assertIn(path,popup._build_refs)
        popup.set_category('video');self.assertIn(path,popup.visible_refs())
        popup.set_category('uploading');self.assertIn(path,popup.visible_refs())
        pending=self.pending('远端尚未上传完的文件.mp4',remote=True)
        popup.set_category('all');self.assertIn(path,popup.visible_refs())
        self.assertNotIn(pending,popup.visible_refs())
        popup.set_category('uploading');self.assertIn(pending,popup.visible_refs())

    def test_selected_preview_uses_same_cloud_state_mark_as_file_artwork(self):
        path=str(self.file('状态预览.txt'))
        self.shelf.add_paths('permanent',[path])
        state={'value':'pending'}
        self.manager.sync=SimpleNamespace(
            sync_status_for_ref=lambda ref:state['value'],
            sync_statuses_for_refs=lambda refs:{ref:state['value'] for ref in refs})
        popup=self.overview()
        self.assertEqual(popup.preview.current_path,path)
        self.assertEqual(popup.preview.cloud_badge.status,'pending')
        self.assertTrue(popup.preview.cloud_badge.isVisible())
        self.assertLess(popup.preview.cloud_badge.geometry().right(),
                        popup.preview.copy_button.geometry().left())
        state['value']='synced'
        popup.refresh_cloud_status();flush()
        self.assertEqual(popup.preview.cloud_badge.status,'synced')
        popup.preview.show_group((path,path))
        self.assertFalse(popup.preview.cloud_badge.isVisible())
    def test_manager_projects_remote_pending_enables_expand_and_routes_cancel_to_sync(self):
        self.manager._sync_transfer_refs=set()
        calls=[]
        self.manager.sync=SimpleNamespace(cancel_upload=lambda value,**options:calls.append((value,options)))
        self.assertTrue(self.shelf.expand_button.isEnabled(),'empty shelf should still open the overview')
        state=dict(id='remote-item',ref=None,name='另一台设备的文档.pdf',kind='file',remote=True,
                   phase='remote',received=300,total=1000,upload_id='remote-upload')
        subject.ShelfManager._transfers_changed(self.manager,[state]);flush()
        self.assertEqual(self.shelf.files['permanent'],[])
        refs=self.shelf.display_refs();self.assertEqual(len(refs),1)
        self.assertTrue(self.shelf.expand_button.isEnabled(),'pending first item must enable overview')
        self.assertEqual(self.shelf.zones['permanent'].caption.text(),'1 项')
        for kind,category in (('text','text'),('url','links')):
            subject.ShelfManager._transfers_changed(self.manager,[dict(state,kind=kind,name='没有文件扩展名的内容')])
            self.assertEqual(self.shelf.content.category(refs[0]),category)
        subject.ShelfManager._transfers_changed(self.manager,[state])
        popup=self.overview();popup.preview.show_path(refs[0],force=True)
        QTest.mouseClick(popup.preview.cancel_transfer,Qt.MouseButton.LeftButton);flush()
        self.assertEqual(calls,[('remote-item',{})])
        subject.ShelfManager._transfers_changed(self.manager,[]);flush()
        self.assertEqual(self.shelf.display_refs(),[]);self.assertTrue(self.shelf.expand_button.isEnabled())
        self.assertFalse(self.shelf.content.transfers)
    def test_hovering_pending_then_restoring_completed_selection_clears_stale_progress(self):
        paths=[str(self.file('所选一.bin')),str(self.file('所选二.bin'))]
        self.shelf.add_paths('permanent',paths)
        pending=self.pending('其他设备正在上传.mp4',remote=True)
        popup=self.overview();popup.select_item(paths[0]);popup.select_item(paths[1],toggle=True)
        self.assertEqual(set(popup.chosen_refs()),set(paths))
        popup.hover_item(pending);self.assertFalse(popup.preview.copy_button.isEnabled())
        self.assertTrue(popup.preview.transfer_row.isVisible())
        popup.end_hover(pending);flush()
        self.assertTrue(popup.preview.copy_button.isEnabled(),'restored completed selection must remain copyable')
        self.assertFalse(popup.preview.transfer_row.isVisible(),'remote hover progress cannot remain on completed selected group')
        self.shelf.content.transfers[paths[1]]={'id':'selected-upload','phase':'uploading','received':10,'total':100}
        self.shelf.transfers_updated();self.shelf._repaint_transfers();flush()
        self.assertFalse(popup.preview.transfer_row.isVisible(),'a repaint must not restore individual progress over a selected group')
    def test_transfers_filter_uses_single_row_picker_and_single_column_at_narrow_width(self):
        for index in range(1024):
            ref='flickdock:transfer:layout-'+str(index)
            self.shelf.content.pending[ref]={'name':f'项目{index}.bin','kind':'file'}
            self.shelf.content.transfers[ref]={'id':str(index),'phase':'queued','received':0,'total':100}
        self.shelf.refresh();popup=self.overview();popup.update_transfer_status()
        native_geometry=self.shelf.geometry()
        for width in (330,480,500,756,800):
            popup.setFixedWidth(width);popup.layout().activate();flush()
            self.assertEqual(popup._narrow_layout,width<780)
            self.assertEqual(popup.category_picker.isVisible(),width<780)
            self.assertEqual(popup.sidebar.isVisible(),width>=780)
            self.assertLessEqual(popup.category_picker.geometry().right(),popup.rect().right())
            for selected in ('all','uploading'):
                popup.set_category(selected);popup.layout().activate();flush()
                if width<780 and selected=='uploading':
                    self.assertTrue(popup.scroll.isVisible())
                    self.assertFalse(popup.preview.isVisible())
                    popup.toggle_narrow_preview();flush()
                    self.assertFalse(popup.scroll.isVisible())
                    self.assertTrue(popup.preview.isVisible())
                    self.assertGreater(popup.preview.width(),width-30)
                    popup.toggle_narrow_preview();flush()
                if width<780:continue
                for button in popup.category_buttons.values():
                    self.assertLessEqual(button.geometry().right(),popup.rect().right(),
                                         f'{button.text()} extends beyond the {width}px overview')
                    self.assertLessEqual(button.mapTo(popup,QPoint(button.width(),0)).x(),popup.width(),
                                         f'{button.text()} is cut off at {width}px')
                    text_width=button.fontMetrics().horizontalAdvance(button.text())
                    self.assertGreaterEqual(button.width()-16,text_width,
                                            f'{button.text()} is clipped in the {width}px overview')
        self.assertEqual(self.shelf.geometry(),native_geometry,'filter layout must not resize the native animation host')

    def test_narrow_overview_selection_preview_category_and_bulk_menu(self):
        refs=[self.shelf.content.add_text(text) for text in ('第一项内容','第二项内容')]
        self.shelf.files['permanent'].extend(refs);self.shelf.refresh()
        popup=self.overview();popup.setFixedWidth(330);popup.layout().activate();flush()
        self.assertTrue(popup._narrow_layout)
        popup.select_item(refs[-1]);flush()
        self.assertTrue(popup.zip_button.isVisible())
        self.assertTrue(popup.scroll.isVisible())
        self.assertFalse(popup.preview.isVisible())
        popup.toggle_narrow_preview();flush()
        self.assertEqual(popup.preview.current_path,refs[-1])
        self.assertTrue(popup.preview.isVisible())
        self.assertFalse(popup.scroll.isVisible())
        self.assertEqual(popup.narrow_view_button.text(),'返回内容')
        popup.toggle_narrow_preview();flush()
        self.assertTrue(popup.scroll.isVisible())
        self.assertFalse(popup.preview.isVisible())
        labels=[]
        def choose_images(menu,*_):
            labels.extend(action.text() for action in menu.actions())
            next(action for action in menu.actions() if action.text()=='图片').trigger()
        with patch.object(subject.AnimatedMenu,'exec',choose_images):popup.open_category_menu()
        self.assertIn('传输中',labels)
        self.assertEqual(popup.category,'images')
        self.assertEqual(popup.category_picker.text(),'图片')
        popup.set_category('all');popup.select_item(refs[-1]);flush()
        bulk=[]
        def inspect_bulk(menu,*_):
            bulk.extend((action.text(),action.isEnabled()) for action in menu.actions() if not action.isSeparator())
        with patch.object(subject.AnimatedMenu,'exec',inspect_bulk):popup.open_bulk_menu()
        self.assertEqual(bulk,[('打包所选…',True),('打包全部…',True),('清空全部',True)])
        for widget in (popup.title,popup.bulk_more,popup.category_picker,popup.narrow_view_button,popup.scroll):
            self.assertGreaterEqual(widget.mapTo(popup,QPoint(0,0)).x(),0)
            self.assertLessEqual(widget.mapTo(popup,QPoint(widget.width(),0)).x(),popup.width())
    def test_transfer_summary_reports_copy_upload_download_without_mislabeling(self):
        for index,phase in enumerate(('copying','uploading','downloading','queued','remote','verifying','retrying','validating')):
            ref=self.pending(f'项目{index}.bin',remote=phase in ('remote','downloading'))
            self.shelf.content.transfers[ref]['phase']=phase
        popup=self.overview();popup.update_transfer_status()
        self.assertEqual(popup.transfer_badge.text(),'8')
        self.assertEqual(popup.transfer_count.text(),
                         '正在复制 1 项 · 正在上传 3 项 · 等待确认 1 项 · 正在下载 1 项 · 校验中 1 项 · 等待重试 1 项')
        self.shelf.content.transfers={ref:state for ref,state in self.shelf.content.transfers.items() if state['phase']=='downloading'}
        popup.update_transfer_status()
        self.assertEqual(popup.transfer_count.text(),'正在下载 1 项')
        self.assertNotIn('上传',popup.transfer_count.text())
    def test_pending_only_clear_all_opens_confirmation_and_respects_cancel(self):
        self.pending('尚未接收完成.mp4',remote=True)
        self.assertEqual(self.shelf.files['permanent'],[])
        counts=[]
        for accepted in (False,True):
            def decide(dialog):
                counts.append(dialog.count)
                self.assertEqual(dialog.count,1)
                return accepted
            with patch.object(subject.ClearConfirmOverlay,'exec',decide),patch.object(self.shelf,'remove_all') as remove:
                self.assertEqual(self.shelf.confirm_clear_all(),accepted)
                if accepted:remove.assert_called_once_with('permanent')
                else:remove.assert_not_called()
            flush()
        self.assertEqual(counts,[1,1],'pending-only contents require the same second confirmation as completed files')


if __name__=='__main__':unittest.main(verbosity=2)
