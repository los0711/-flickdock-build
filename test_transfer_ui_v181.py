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
from transfer_visuals import TransferIconLabel

APP=QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
subject.load_font(APP)


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
    def test_held_copy_displays_dim_file_card_ring_and_can_cancel_partial(self):
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
            self.assertNotEqual(active_compact,normal_compact,'compact shelf also needs a dim card and circular progress')
            popup=self.overview();popup.preview.show_path(ref,force=True);flush()
            item=next(item for item in popup.rows_body.findChildren(IconItem) if item.ref==ref)
            active=opaque_artwork(item)
            removed=self.shelf.content.transfers.pop(ref);normal=opaque_artwork(item);self.shelf.content.transfers[ref]=removed
            self.assertNotEqual(active,normal,'transfer tile must visibly dim artwork and draw its ring')
            candidates=[]
            ratio=normal.devicePixelRatio()
            for y in range(round(32*ratio),round(57*ratio)):
                for x in range(round(44*ratio),round(68*ratio)):
                    color=normal.pixelColor(x,y);sat=max(color.red(),color.green(),color.blue())-min(color.red(),color.green(),color.blue())
                    candidates.append((sat,x,y))
            saturation,x,y=max(candidates);self.assertGreater(saturation,40)
            dim=active.pixelColor(x,y);dim_saturation=max(dim.red(),dim.green(),dim.blue())-min(dim.red(),dim.green(),dim.blue())
            self.assertLess(dim_saturation,saturation*.65)
            blue=sum(1 for y in range(round(13*ratio),round(77*ratio)) for x in range(round(24*ratio),round(88*ratio))
                     if (lambda c:c.blue()>180 and c.blue()-c.red()>85 and c.blue()-c.green()>25)(active.pixelColor(x,y)))
            self.assertGreater(blue,20,'circular upload arc must remain visible over dim artwork')
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
            popup=self.overview();popup.preview.show_path(ref,force=True)
            self.assertIs(popup.preview.stack.currentWidget(),popup.preview.icon)
            self.assertFalse(popup.preview.copy_button.isEnabled())
            self.assertIn('另一台设备',popup.preview.info.text())
            self.assertIsNone(popup.preview.media.player)
            preview_active=opaque_artwork(popup.preview.stack)
            state=self.shelf.content.transfers.pop(ref);preview_normal=opaque_artwork(popup.preview.stack)
            self.shelf.content.transfers[ref]=state
            self.assertNotEqual(preview_active,preview_normal,'right preview also needs dim artwork and transfer feedback')
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
        self.shelf.transfers_updated();popup=self.overview();popup.update_transfer_status();popup.set_category('uploading');flush()
        self.assertEqual(set(popup.visible_refs()),{str(path),pending})
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
        self.assertEqual(popup.category_buttons['uploading'].text(),'正在上传')
    def test_manager_projects_remote_pending_enables_expand_and_routes_cancel_to_sync(self):
        self.manager._sync_transfer_refs=set()
        calls=[]
        self.manager.sync=SimpleNamespace(cancel_upload=lambda value,**options:calls.append((value,options)))
        self.assertFalse(self.shelf.expand_button.isEnabled())
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
        self.assertEqual(self.shelf.display_refs(),[]);self.assertFalse(self.shelf.expand_button.isEnabled())
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


if __name__=='__main__':unittest.main(verbosity=2)
