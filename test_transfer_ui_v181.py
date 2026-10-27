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

from PySide6.QtCore import QCoreApplication,QEvent,QMimeData,QPoint,QTimer,Qt,QUrl
from PySide6.QtGui import QColor,QDesktopServices,QImage,QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QWidget

import app_settings
import capture_files
import flickdock as subject
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
            for y in range(round(32*ratio),round(57*ratio)):
                for x in range(round(44*ratio),round(68*ratio)):
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
        self.assertTrue(self.shelf.upload_count.isVisible());self.assertEqual(self.shelf.upload_count.count,2)
        self.assertIn('2',popup.category_buttons['uploading'].text());self.assertIn('2',popup.transfer_count.text())
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
        self.assertTrue(self.shelf.upload_count.complete)
        self.assertTrue(self.shelf.upload_count.isVisible())
        wait(lambda:not self.shelf.upload_count.isVisible(),timeout=3)

    def test_overview_footer_shows_two_transfers_and_opens_the_full_list(self):
        refs=[self.pending(f'传输文件{index}.bin',received=index*10,total=100)
              for index in range(4)]
        popup=self.overview()
        self.assertEqual(popup.category,'all')
        self.assertEqual(popup.visible_refs(),[],'active transfers stay outside the completed list')
        self.assertTrue(popup.transfer_strip.isVisible())
        self.assertEqual(popup._strip_refs,refs[-1:-3:-1])
        self.assertEqual(sum(row.isVisible() for row,*_ in popup.strip_rows),2)
        self.assertIn('+2',popup.strip_more.text())
        self.assertEqual(popup.strip_rows[0][3].value(),300)
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

    def test_percent_and_estimated_time_update_in_footer_category_and_preview(self):
        clock=[0.0]
        self.shelf.transfer_rates=TransferRateTracker(clock=lambda:clock[0])
        ref=self.pending('正在上传的大文件.mov',received=0,total=16*1024*1024)
        popup=self.overview()
        self.assertIn('0%',popup.strip_rows[0][2].text())
        self.assertIn('计算中',popup.strip_rows[0][2].text())
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
        self.assertIn('25% · 剩余约 3 秒',popup.strip_rows[0][2].text())
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
    def test_transfers_filter_tabs_fit_narrow_overview_with_four_digit_count(self):
        for index in range(1024):
            ref='flickdock:transfer:layout-'+str(index)
            self.shelf.content.pending[ref]={'name':f'项目{index}.bin','kind':'file'}
            self.shelf.content.transfers[ref]={'id':str(index),'phase':'queued','received':0,'total':100}
        self.shelf.refresh();popup=self.overview();popup.update_transfer_status()
        native_geometry=self.shelf.geometry()
        for width in (480,500):
            popup.setFixedWidth(width);popup.layout().activate();flush()
            for selected in ('all','uploading'):
                popup.set_category(selected);popup.layout().activate();flush()
                for button in popup.category_buttons.values():
                    self.assertLessEqual(button.geometry().right(),popup.rect().right(),
                                         f'{button.text()} extends beyond the {width}px overview')
                    text_width=button.fontMetrics().horizontalAdvance(button.text())
                    self.assertGreaterEqual(button.width()-16,text_width,
                                            f'{button.text()} is clipped in the {width}px overview')
        self.assertEqual(self.shelf.geometry(),native_geometry,'filter layout must not resize the native animation host')
    def test_transfer_summary_reports_copy_upload_download_without_mislabeling(self):
        for index,phase in enumerate(('copying','uploading','downloading','queued','remote')):
            ref=self.pending(f'项目{index}.bin',remote=phase in ('remote','downloading'))
            self.shelf.content.transfers[ref]['phase']=phase
        popup=self.overview();popup.update_transfer_status()
        self.assertEqual(popup.category_buttons['uploading'].text(),'传输中 5')
        self.assertEqual(popup.transfer_count.text(),'正在复制 1 项 · 正在上传 3 项 · 正在下载 1 项')
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
