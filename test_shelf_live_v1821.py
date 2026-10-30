"""Open-window remote changes; disposable Qt offscreen views, no OS hooks."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest

import flickdock as subject
from sync_adapter import ShelfAdapter
from test_html_native_workspace import flush, until
import test_html_native_workspace as workspace_fixture
from content_stack import StackTile


class LiveShelfTests(unittest.TestCase):
    def setUp(self):
        workspace_fixture.WorkspaceTests.setUp(self)
        self.patches.enter_context(patch('native_ui_common.reduced_motion',return_value=False))
    tearDown = workspace_fixture.WorkspaceTests.tearDown
    overview = workspace_fixture.WorkspaceTests.overview

    def rows(self, popup):
        return {row.path: row for row in popup.rows_body.findChildren(subject.FileRow)
                if not row.isHidden()}

    def test_metadata_update_keeps_existing_widgets_selection_and_preview(self):
        popup = self.overview()
        popup.select_item(self.paths[1])
        before = self.rows(popup)
        current = popup.preview.current_path
        adapter = ShelfAdapter(self.manager)
        view = adapter.snapshot()
        view['metadata'].setdefault(self.paths[0], {})['title'] = '来自其他设备的新名称.txt'
        with patch.object(popup.preview, 'show_path', wraps=popup.preview.show_path) as show:
            adapter.restore(view)
            flush()
            after = self.rows(popup)
            self.assertIs(after[self.paths[1]], before[self.paths[1]])
            self.assertIs(after[self.paths[0]], before[self.paths[0]])
            self.assertEqual(after[self.paths[0]].name_label.text(), '来自其他设备的新名称.txt')
            self.assertEqual(popup.selected, {self.paths[1]})
            self.assertEqual(popup.preview.current_path, current)
            self.assertFalse(any(call.kwargs.get('force') for call in show.call_args_list))

    def test_change_during_expand_is_reconciled_on_landing(self):
        popup = self.overview()
        ref = 'flickdock:transfer:remote-one'
        with patch.object(self.shelf, 'content_transition_active', return_value=True):
            self.shelf.content.pending[ref] = {'name': 'remote.png', 'kind': 'file'}
            self.shelf.content.transfers[ref] = {'id': 'remote-one', 'remote': True,
                                              'phase': 'remote', 'total': 1024, 'received': 0}
            self.shelf.transfers_updated()
        popup.resume_content()
        until(lambda: popup._build_cursor == len(popup.visible_refs()))
        self.assertIn(ref, self.rows(popup))

    def test_progress_only_does_not_rebuild_widgets(self):
        popup = self.overview()
        ref = 'flickdock:transfer:remote-one'
        self.shelf.content.pending[ref] = {'name': 'remote.png', 'kind': 'file'}
        self.shelf.content.transfers[ref] = {'id': 'remote-one', 'remote': True,
                                          'phase': 'remote', 'total': 1024, 'received': 0}
        self.shelf.transfers_updated()
        until(lambda: popup._build_cursor == len(popup.visible_refs()))
        before = self.rows(popup)
        with patch.object(popup, 'rebuild', wraps=popup.rebuild) as rebuild:
            for received in range(10, 100, 10):
                self.shelf.content.transfers[ref]['received'] = received
                self.shelf.transfers_updated()
            self.assertEqual(rebuild.call_count, 0)
        self.assertEqual(self.rows(popup), before)

    def test_reservation_to_verified_file_keeps_card_and_never_deletes_its_pixels(self):
        from PySide6.QtGui import QImage,QColor
        self.manager.motion=True;self.manager._sync_transfer_refs=set()
        tile=self.shelf.zones['permanent'].icon;tile.motion=True
        self.manager.sync=SimpleNamespace(catching_up=False,sync_status_for_ref=lambda _:'synced')
        popup=self.overview();adapter=ShelfAdapter(self.manager)
        for mode in ('list','icons'):
            self.settings.update(view_mode=mode);popup._render_signature=None;popup.rebuild();flush()
            identity='handoff-'+mode;pending='flickdock:transfer:'+identity
            transfer=dict(id=identity,remote=True,phase='downloading',name=mode+'.png',kind='file',total=1024,received=100)
            subject.ShelfManager._transfers_changed(self.manager,[transfer]);until(lambda:pending in popup._item_widgets)
            tile.landing.setCurrentTime(260);popup.select_item(pending);old=popup._item_widgets[pending]
            image=QImage(64,64,QImage.Format_RGB32);image.fill(QColor('#2478ab'))
            path=self.base/(mode+'.png');image.save(str(path));ref=str(path)
            view=adapter.snapshot();view['permanent'].append(ref);view['metadata'][ref]={'_cloud_item_id':identity,'title':path.name}
            with patch.object(self.shelf,'show') as show,patch.object(self.shelf,'hide') as hide:
                adapter.restore(view);until(lambda:ref in popup._item_widgets)
                subject.ShelfManager._transfers_changed(self.manager,[]);flush()
                self.assertIs(popup._item_widgets[ref],old);self.assertFalse(popup.item_transitions.ghosts)
                self.assertEqual(popup.selected,{ref});self.assertEqual(popup.preview.current_path,ref)
                self.assertEqual(tile.landing.state(),tile.landing.State.Stopped)
                self.assertEqual(tile.removal.state(),tile.removal.State.Stopped)
                show.assert_not_called();hide.assert_not_called()
            with patch.object(popup,'remove_path') as remove:
                old.remove_button.click();remove.assert_called_once_with(ref)
            if mode=='list':
                with patch.object(popup,'select_item') as select:
                    old.selection_check.click();select.assert_called_once_with(ref,toggle=True)

    def test_single_icon_opens_in_first_slot_before_any_new_file_arrives(self):
        self.settings.update(view_mode='icons');self.shelf.files['permanent']=self.paths[:1];self.shelf.refresh()
        popup=self.overview();flush();flush()
        item=popup._item_widgets[self.paths[0]]
        self.assertEqual(item.x(),popup.rows_layout.contentsMargins().left())
        self.assertEqual(item.y(),popup.rows_layout.contentsMargins().top())
        self.assertGreater(popup._grid_columns,1)
        popup.resize(740,550);flush();flush()
        self.assertEqual(item.x(),popup.rows_layout.contentsMargins().left())

    def test_batch_removal_parallel_fades_and_survivors_move(self):
        popup=self.overview();self.manager.motion=True
        before=self.rows(popup);remaining=self.paths[0]
        start=before[remaining].pos()
        adapter=ShelfAdapter(self.manager);view=adapter.snapshot()
        clock=[0.0]
        # Drive only this visual clock: a busy CI runner may paint a first
        # animation tick during flush(), which is valid rather than a jump.
        with patch('item_transitions.time',SimpleNamespace(perf_counter=lambda:clock[0])):
            view['permanent']=[remaining];adapter.restore(view);flush()
            motion=popup.item_transitions
            self.assertEqual(len(motion.ghosts),2)
            self.assertTrue(motion.overlay.isVisible())
            self.assertEqual(set(popup.visible_refs()),{remaining})
            self.assertIs(self.rows(popup)[remaining],before[remaining])
            self.assertEqual(before[remaining].pos(),start)
            clock[0]=.075;motion._tick();flush()
            middle=before[remaining].pos()
            self.assertLess(middle.y(),start.y())
            clock[0]=.245;motion._tick();flush()
            self.assertLess(before[remaining].y(),middle.y())
            self.assertFalse(motion.timer.isActive());self.assertFalse(motion.ghosts)
        self.assertTrue(popup.isVisible());self.assertTrue(self.shelf.expanded)

    def test_delete_all_fades_over_empty_state_then_releases_snapshots(self):
        popup=self.overview();self.manager.motion=True
        adapter=ShelfAdapter(self.manager);view=adapter.snapshot();view['permanent']=[]
        adapter.restore(view);flush()
        motion=popup.item_transitions
        self.assertEqual(popup.visible_refs(),[])
        self.assertTrue(popup.empty_state.isVisible())
        self.assertEqual(len(motion.ghosts),3)
        self.assertTrue(motion.overlay.isVisible())
        QTest.qWait(240);flush()
        self.assertFalse(motion.ghosts);self.assertFalse(motion.overlay.isVisible())

    def test_metadata_during_batch_build_does_not_stall_or_leak_height(self):
        popup=self.overview();adapter=ShelfAdapter(self.manager)
        view=adapter.snapshot()
        for index in range(40):
            path=self.base/f'new-{index}.dat';path.write_bytes(b'x')
            view['permanent'].append(str(path))
        adapter.restore(view)
        view['metadata'].setdefault(self.paths[0],{})['title']='new title'
        adapter.restore(view)
        other=self.base/'last.dat';other.write_bytes(b'x');view['permanent'].append(str(other))
        adapter.restore(view)
        until(lambda:popup._build_cursor==len(popup.visible_refs()))
        self.assertEqual(len(self.rows(popup)),44)
        view['permanent']=self.paths[:1];adapter.restore(view);flush()
        self.assertEqual(popup.rows_body.minimumHeight(),0)
        self.assertEqual(popup.scroll.verticalScrollBar().maximum(),0)

    def test_user_scroll_during_append_is_not_overwritten(self):
        popup=self.overview();adapter=ShelfAdapter(self.manager);view=adapter.snapshot()
        for index in range(85):
            path=self.base/f'scroll-{index}.dat';path.write_bytes(b'x');view['permanent'].append(str(path))
        adapter.restore(view);until(lambda:popup._build_cursor==len(popup.visible_refs()))
        bar=popup.scroll.verticalScrollBar();until(lambda:bar.maximum()>=2000);bar.setValue(800)
        for index in range(35):
            path=self.base/f'appended-{index}.dat';path.write_bytes(b'x');view['permanent'].append(str(path))
        adapter.restore(view);bar.setValue(1600)
        self.assertEqual(bar.value(),1600)
        until(lambda:popup._build_cursor==len(popup.visible_refs()))
        self.assertEqual(bar.value(),1600)

    def test_compact_delete_parallel_fade_and_no_false_arrival(self):
        tile=StackTile(self.shelf,motion=True);tile.resize(170,140);tile.show()
        tile.set_stack(self.shelf.content,self.paths)
        tile.landing.setCurrentTime(260)
        tile.set_stack(self.shelf.content,self.paths[:1])
        self.assertEqual(tile.refs,tuple(self.paths[:1]))
        self.assertEqual(tile.removal.duration(),200)
        self.assertEqual(set(tile._exit_from)-set(tile._exit_to),set(self.paths[1:]))
        self.assertEqual(tile.landing.state(),tile.landing.State.Stopped)
        tile.removal.setCurrentTime(70)
        a=tile.grab().toImage()
        tile.set_stack(self.shelf.content,[]);tile.set_content('empty')
        self.assertEqual(tile.refs,())
        tile.removal.setCurrentTime(100);tile.grab()
        self.assertEqual(tile._kind,'empty')
        tile.removal.setCurrentTime(200)
        self.assertFalse(tile._exit_from);self.assertFalse(tile._exit_to)
        self.assertFalse(a.isNull())

    def test_reconnect_backfill_keeps_front_then_live_arrival_is_animated(self):
        tile=StackTile(self.shelf,motion=True);tile.resize(170,140);tile.show()
        repository=self.shelf.content
        refs=list(self.paths)
        for index,ref in enumerate(refs):repository.metadata.setdefault(ref,{})['_cloud_item_id']=f'known-{index}'
        tile.set_stack(repository,refs);tile.landing.setCurrentTime(260)
        front=tile.refs[-1];repository.quiet_cloud_refresh=True
        for index in range(12):
            ref=f'flickdock:transfer:backfill-{index}'
            repository.pending[ref]={'name':f'old-{index}.txt','kind':'file'};refs.append(ref)
            tile.set_stack(repository,refs)
            self.assertEqual(tile.refs[-1],front)
            self.assertEqual(tile.landing.state(),tile.landing.State.Stopped)
        # Final reconciliation and later progress updates cannot replay them.
        repository.quiet_cloud_refresh=False;tile.set_stack(repository,refs)
        self.assertEqual(tile.refs[-1],front)
        ref='flickdock:transfer:new-live';repository.pending[ref]={'name':'fresh.txt','kind':'file'}
        tile.set_stack(repository,refs+[ref])
        self.assertEqual(tile.refs[-1],ref)
        self.assertEqual(tile.landing.state(),tile.landing.State.Running)

    def test_reduced_motion_keeps_immediate_data_and_no_reflow(self):
        popup=self.overview();self.manager.motion=True
        adapter=ShelfAdapter(self.manager);view=adapter.snapshot();view['permanent']=self.paths[:1]
        with patch('native_ui_common.reduced_motion',return_value=True):
            adapter.restore(view);flush()
        self.assertEqual(popup.visible_refs(),self.paths[:1])
        self.assertFalse(popup.item_transitions.ghosts)
        self.assertFalse(popup.item_transitions.moves)

    def test_visible_remote_metadata_does_not_become_local_edit(self):
        popup=self.overview();ref=self.paths[0];row=self.rows(popup)[ref]
        self.manager._sync_transfer_refs=set()
        adapter=ShelfAdapter(self.manager);original=adapter.title(ref)
        subject.ShelfManager._transfers_changed(self.manager,[dict(id='remote-version',ref=ref,
            remote=True,phase='downloading',name='new remote name.txt',received=0,total=123)])
        flush()
        self.assertIs(self.rows(popup)[ref],row)
        self.assertEqual(self.shelf.content.title(ref),'new remote name.txt')
        self.assertEqual(row.name_label.full_text,'new remote name.txt')
        self.assertEqual(adapter.title(ref),original)
        self.assertNotIn('title',adapter.snapshot()['metadata'].get(ref,{}))


if __name__ == '__main__':
    unittest.main(defaultTest='LiveShelfTests')
