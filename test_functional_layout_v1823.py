"""Real offscreen production widgets, with fake transport and temporary files."""
import os,tempfile,unittest
os.environ['QT_QPA_PLATFORM']='offscreen'
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QPoint,QRect,Qt
from PySide6.QtTest import QTest
import test_html_native_workspace as workspace
from test_html_native_lan import FakeLan
from app_settings import SettingsStore
from lan_panel import LanDialog

class OverviewRefinementTests(unittest.TestCase):
    def setUp(self):
        self.fixture=workspace.WorkspaceTests('runTest');self.fixture.setUp();self.popup=self.fixture.overview()
    def tearDown(self):self.fixture.tearDown()
    def test_sort_segment_keeps_last_criterion_and_default_cycles(self):
        p=self.popup;buttons=(p.selection_hint,*p.sort_buttons.values())
        self.assertTrue(all(b.parentWidget() is p.sort_segment for b in buttons))
        for key,direction in (('time',1),('time',2),('time',0),('size',1),('time',1)):
            QTest.mouseClick(p.sort_buttons[key],Qt.LeftButton);workspace.flush()
            self.assertEqual(p.sort_direction,direction);self.assertEqual(p.sort_key,key if direction else None)
            self.assertEqual(sum(b.isChecked() for b in buttons),1)
            for other,button in p.sort_buttons.items():self.assertEqual(button.direction,direction if other==key else 0)
        p.selection_hint.click();workspace.flush();self.assertIsNone(p.sort_key);self.assertTrue(p.selection_hint.isChecked())
    def test_bulk_actions_before_select_all_and_each_control_fits(self):
        p=self.popup
        for button in (p.zip_button,p.zip_all_button,p.clear_button):
            self.assertIs(button.parentWidget(),p.selection_bar)
            bounds=QRect(button.mapTo(p.selection_bar,QPoint()),button.size())
            self.assertTrue(p.selection_bar.rect().contains(bounds))
            self.assertLess(bounds.right(),p.select_all_button.geometry().left())
        for b in (p.selection_hint,*p.sort_buttons.values()):self.assertTrue(p.sort_segment.rect().contains(b.geometry()))
        self.assertGreaterEqual(p.selection_hint.width(),p.selection_hint.fontMetrics().horizontalAdvance('默认')+20)
        self.assertEqual(p.sort_segment.geometry().left(),p.rows_layout.contentsMargins().left())
        self.assertGreater(p.count_label.geometry().left(),p.sort_segment.geometry().right())
        self.assertEqual(p.view_segment.mapTo(p,QPoint()).x()+p.view_segment.width(),p.middle.geometry().right()+1-12)
        row=next(iter(p._row_widgets.values())) if hasattr(p,'_row_widgets') and p._row_widgets else p.rows_body.findChild(__import__('flickdock').FileRow)
        self.assertIsNotNone(row);self.assertLessEqual(row.name_label.font().pixelSize(),13)

    def test_detail_actions_call_existing_operations_and_remove_keeps_original(self):
        p=self.popup;ref=self.fixture.paths[0];p.select_item(ref);workspace.flush();preview=p.preview
        self.assertTrue(preview.info.isVisible());self.assertGreater(preview.info.width(),90);self.assertTrue(preview.info.text())
        operations=((preview.open_button,'open_path'),(preview.reveal_button,'reveal_ref'),(preview.rename_button,'rename_ref'))
        for button,name in operations:
            preview.detail_scroll.ensureWidgetVisible(button,0,4);workspace.flush()
            self.assertTrue(button.isVisible())
            with patch.object(self.fixture.shelf,name) as action:button.click();action.assert_called_once_with(ref)
        for button,name in ((preview.copy_selection_button,'copy_items'),(preview.zip_selection_button,'compress_refs')):
            with patch.object(self.fixture.shelf,name) as action:button.click();action.assert_called_once_with([ref])
        self.assertEqual(preview.remove_button._reference_icon,('trash','red'))
        preview.remove_button.click();workspace.flush()
        self.assertNotIn(ref,self.fixture.shelf.files['permanent']);self.assertTrue(Path(ref).is_file())

    def test_reveal_uses_platform_file_manager_without_shell_interpolation(self):
        shelf=self.fixture.shelf;ref=self.fixture.paths[0]
        with patch('flickdock.subprocess.Popen') as process:
            with patch('flickdock.sys.platform','darwin'):shelf.reveal_ref(ref)
            process.assert_called_once_with(['/usr/bin/open','-R',str(Path(ref).resolve())])
        with patch('flickdock.subprocess.Popen') as process:
            with patch('flickdock.sys.platform','win32'):shelf.reveal_ref(ref)
            process.assert_called_once_with(['explorer.exe','/select,',str(Path(ref).resolve())])

    def test_cancelled_upload_becomes_unsynced_and_resume_calls_existing_task(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        p=self.popup;ref=self.fixture.paths[0];paused=[False]
        def cancel(value):
            self.assertEqual(value,ref);paused[0]=True;self.fixture.shelf.content.transfers.pop(ref,None)
        retry=Mock(side_effect=lambda value:paused.__setitem__(0,False))
        self.fixture.manager.sync=SimpleNamespace(upload_enabled=True,is_upload_paused=lambda value:paused[0],retry_upload=retry,
            sync_status_for_ref=lambda value:'paused' if paused[0] else 'uploading',
            sync_statuses_for_refs=lambda refs:{r:'paused' if paused[0] else 'uploading' for r in refs})
        self.fixture.shelf.content.transfers[ref]=dict(id='fixture-upload',phase='uploading',received=30,total=100,remote=False)
        p.select_item(ref);p.preview.update_transfer_status();workspace.flush()
        with patch.object(self.fixture.shelf,'cancel_transfer',side_effect=cancel):p.preview.cancel_transfer.click()
        workspace.flush();self.assertEqual(p.preview.status_value.text(),'未同步')
        self.assertEqual(p.preview.cancel_transfer.text(),'上传云端');self.assertTrue(p.preview.cancel_transfer.isVisible())
        p.preview.cancel_transfer.click();retry.assert_called_once_with(ref);self.assertTrue(Path(ref).exists())

    def test_grid_status_pills_and_remove_buttons_use_the_same_real_state(self):
        from types import SimpleNamespace
        from content_grid import IconItem,GRID_ITEM_HEIGHT
        p=self.popup;status=['uploading']
        self.fixture.manager.sync=SimpleNamespace(sync_status_for_ref=lambda ref:status[0],
            sync_statuses_for_refs=lambda refs:{ref:status[0] for ref in refs},is_upload_paused=lambda ref:False)
        for mode in ('list','icons'):
            p.change_view_mode(mode);workspace.flush();p.resume_content();workspace.flush()
            for value in ('uploading','synced','pending','local','error'):
                status[0]=value;p.refresh_cloud_status();workspace.flush()
                items=[w for w in p._item_widgets.values() if w.isVisible()]
                self.assertTrue(items)
                for item in items:
                    self.assertEqual(item.status_pill.status,value);self.assertTrue(item.remove_button.isVisible())
                    if isinstance(item,IconItem):
                        self.assertEqual(item.height(),GRID_ITEM_HEIGHT)
                        self.assertTrue(item.rect().contains(item.status_pill.geometry()))
                        self.assertLess(item.remove_button.geometry().bottom(),item.status_pill.geometry().top())

    def test_default_grid_fits_three_cards_with_original_status_icons(self):
        from content_grid import IconItem
        from transfer_visuals import CloudStatusBadge
        p=self.popup;p.change_view_mode('icons');p.resume_content()
        workspace.until(lambda:p._build_cursor==len(p.visible_refs()));workspace.flush()
        cards=sorted((item for item in p._item_widgets.values() if isinstance(item,IconItem)),key=lambda item:item.geometry().left())
        self.assertEqual(len(cards),3)
        self.assertEqual(len({item.geometry().top() for item in cards}),1)
        for item in cards:
            self.assertIsInstance(item.status_badge,CloudStatusBadge)
            self.assertTrue(p.rows_body.rect().contains(item.geometry()))
        for first,second in zip(cards,cards[1:]):self.assertLess(first.geometry().right(),second.geometry().left())

    def test_cards_and_detail_use_saved_intake_time_with_seconds_not_cloud_upload_time(self):
        from PySide6.QtCore import QDateTime
        from types import SimpleNamespace
        from unittest.mock import Mock
        from shelf_content import ContentRepository
        p=self.popup;ref=self.fixture.paths[0];stamp='2026-10-31T04:23:45Z'
        fixed=QDateTime.fromString(stamp,Qt.ISODate)
        with patch('flickdock.QDateTime') as clock:
            clock.currentDateTimeUtc.return_value=fixed
            self.fixture.shelf.add_paths('permanent',[ref])
        self.fixture.manager.sync=SimpleNamespace(uploaded_at_for_ref=Mock(return_value='2026-11-01T09:00:00Z'),
            sync_status_for_ref=lambda _: 'synced',sync_statuses_for_refs=lambda refs:{r:'synced' for r in refs})
        p.change_view_mode('icons');p.resume_content();workspace.until(lambda:p._build_cursor==len(p.visible_refs()))
        p.select_item(ref);p.refresh_cloud_status();workspace.flush()
        expected=fixed.toLocalTime().toString('MM-dd HH:mm:ss')
        self.assertEqual(p._item_widgets[ref].added_time,expected)
        self.assertEqual(p.preview.upload_time_value.text(),fixed.toLocalTime().toString('yyyy-MM-dd HH:mm:ss'))
        from transfer_visuals import FileStatusPill
        self.assertIsInstance(p.preview.cloud_badge,FileStatusPill)
        self.assertEqual(p.preview.cloud_badge.status,'synced')
        self.assertLess(p.preview.cloud_badge.geometry().right(),p.preview.copy_button.geometry().left())
        self.fixture.manager.sync.uploaded_at_for_ref.assert_not_called()
        p.change_view_mode('list');p.resume_content();workspace.until(lambda:p._build_cursor==len(p.visible_refs()))
        row=p._item_widgets[ref]
        self.assertIn(expected,row.detail_label.text())
        self.assertNotIn('PNG 图像',row.detail_label.text())
        row.refresh_content();self.assertIn(expected,row.detail_label.text())
        p.change_view_mode('icons');p.resume_content();workspace.until(lambda:p._build_cursor==len(p.visible_refs()))
        restored=ContentRepository(metadata=self.fixture.shelf.content.export_metadata())
        self.assertEqual(restored.metadata[ref]['shelf_added_at'],stamp)
        self.fixture.shelf.content.metadata[ref].pop('shelf_added_at')
        p._item_widgets[ref].refresh_content();workspace.flush()
        self.assertEqual(p._item_widgets[ref].added_time,'暂无时间')

    def test_copy_intake_keeps_drop_time_through_background_file_copy(self):
        self.fixture.settings.update(capture_mode='copy')
        stamp='2026-10-31T04:23:45Z';committed=[]
        self.fixture.shelf.import_paths('permanent',[self.fixture.paths[0]],on_committed=lambda:committed.append(True),_added_at=stamp)
        workspace.until(lambda:bool(committed))
        captured=self.fixture.shelf.files['permanent'][-1]
        self.assertNotEqual(captured,self.fixture.paths[0]);self.assertTrue(Path(captured).exists())
        self.assertEqual(self.fixture.shelf.content.metadata[captured]['shelf_added_at'],stamp)

class TransferCardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.dialog=LanDialog(FakeLan(),SettingsStore(Path(self.temp.name)));self.dialog.show();workspace.flush()
    def tearDown(self):self.dialog.hide();self.dialog.deleteLater();workspace.flush();self.temp.cleanup()
    def test_actual_progress_route_speed_eta_and_final_turn_history(self):
        p=self.dialog.panel;p._wan=True
        data=dict(id='card',direction='receive',peer='MacBook Air',name='original.bin',state='transferring',received=2_000_000,total=20_000_000,route='relay',relay_bytes=2_100_000)
        with patch('lan_panel.time.monotonic',return_value=10):p._progress(data)
        data.update(received=4_000_000,relay_bytes=4_200_000)
        with patch('lan_panel.time.monotonic',return_value=11):p._progress(data)
        workspace.flush();row=p._rows['card']
        self.assertEqual(row['bar'].value(),200);self.assertEqual(row['title'].accessibleName(),'original.bin')
        self.assertEqual(row['route_badge'].text(),'TURN:4.2MB');self.assertEqual(row['direction_badge'].text(),'接收')
        self.assertIn('MacBook Air',row['detail'].accessibleName());self.assertIn('剩余约 8 秒',row['metrics'].accessibleName())
        self.assertIn('1.9 MB/s',row['metrics'].accessibleName());self.assertNotIn('original.bin',row['detail'].accessibleName())
        self.assertLess(row['heading'].geometry().bottom(),row['bar'].geometry().top())
        self.assertLess(row['bar'].geometry().bottom(),row['summary'].geometry().top())
        data.update(state='cancelled',relay_bytes=4_300_000);p._progress(data);workspace.flush()
        self.assertTrue(row['frame'].isHidden());self.assertIn('TURN:4.3MB',p.history_rows[0].title.accessibleName())
        self.assertEqual(p.history_rows[0].state.text(),'已取消');self.assertEqual(p.history_rows[0].remove_button.text(),'')
    def test_unknown_route_is_not_reported_as_direct_and_destination_heights_match(self):
        p=self.dialog.panel;p._wan=True
        p._progress(dict(id='pending',direction='send',peer='Windows',state='waiting',received=0,total=100))
        self.assertEqual(p._rows['pending']['route_badge'].text(),'连接中')
        self.assertFalse(p._rows['pending']['metrics'].isVisible())
        for theme in ('light','dark'):
            self.dialog.apply_theme(theme);workspace.flush()
            self.assertEqual(p.receive_path.height(),p.choose_destination.height())
            self.assertEqual(p.receive_path.geometry().center().y(),p.choose_destination.geometry().center().y())

    def test_queue_has_two_columns_and_individual_remove_preserves_originals(self):
        p=self.dialog.panel;paths=[]
        for index in range(5):
            path=Path(self.temp.name)/f'original{index}.bin';path.write_bytes(b'original');paths.append(str(path))
        p.queue_paths(paths);workspace.until(lambda:p.queue_column.isVisible() and p.queue_area.value==1.)
        rows=p.queue_rows
        self.assertEqual(rows[0].y(),rows[1].y());self.assertLess(rows[0].geometry().right(),rows[1].geometry().left())
        self.assertLess(rows[0].geometry().bottom(),rows[2].geometry().top())
        remove=next(button for button in rows[1].findChildren(__import__('PySide6.QtWidgets',fromlist=['QPushButton']).QPushButton) if button.accessibleName().startswith('移除待发送文件'))
        self.assertTrue(remove.isVisible());remove.click();workspace.flush()
        self.assertEqual(p._queued_paths,[ref for ref in paths if ref!=paths[1]])
        self.assertTrue(all(Path(ref).read_bytes()==b'original' for ref in paths))

    def test_initial_send_uses_overview_selection_until_user_clears_queue(self):
        p=self.dialog.panel;original=Path(self.temp.name)/'selected.txt';original.write_text('original')
        self.assertFalse(p._explicit_queue)
        p.set_paths_provider(lambda:[str(original)])
        p.send_button.click();workspace.flush()
        self.assertTrue(any(call[0]=='send' and call[-1]==[str(original)] for call in p.service.calls))

    def test_removing_last_explicit_file_never_resends_hidden_overview_selection(self):
        p=self.dialog.panel;original=Path(self.temp.name)/'kept.bin';original.write_bytes(b'keep')
        p.set_paths_provider(lambda:[str(original)]);p.queue_paths([str(original)]);workspace.flush()
        remove=next(button for button in p.queue_rows[0].findChildren(__import__('PySide6.QtWidgets',fromlist=['QPushButton']).QPushButton) if button.accessibleName().startswith('移除待发送文件'))
        remove.click();workspace.flush();self.assertEqual(p._queued_paths,[])
        with patch('lan_panel.QFileDialog.getOpenFileNames',return_value=([],'')) as picker:p.send_button.click()
        picker.assert_called_once();self.assertFalse(any(call[0]=='send' for call in p.service.calls));self.assertTrue(original.exists())

class UploadTimeTests(unittest.TestCase):
    def test_commit_keeps_server_time_and_accessor_rejects_missing_or_invalid_time(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from shelf_sync import ShelfSync
        entry=dict(id='id',ref='original',version=1,seq=2,dirty=True)
        owner=SimpleNamespace(state={'entries':{'id':entry}},_remote_heads={},_upload_tokens={},_forget_resume=Mock(),_persist=Mock(),_notify_transfers=Mock(),_push_next=Mock())
        self.assertIsNone(ShelfSync.uploaded_at_for_ref(owner,'original'))
        transfer=dict(id='id',version=1,seq=2,sha256='a'*64,fingerprint='fixture',upload_id='upload')
        server='2026-10-31T04:23:45.000Z'
        ShelfSync._upload_committed(owner,transfer,dict(id='id',version=2,sha256='a'*64,updated_at=server))
        self.assertEqual(entry['cloud_uploaded_at'],server);self.assertEqual(ShelfSync.uploaded_at_for_ref(owner,'original'),server)
        for invalid in ('','invalid','2026-10-31T04:23:45',123):
            entry['cloud_uploaded_at']=invalid;self.assertIsNone(ShelfSync.uploaded_at_for_ref(owner,'original'))

if __name__=='__main__':unittest.main()
