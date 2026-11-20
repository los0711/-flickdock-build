"""Batch actions, truthful aggregate progress, and interrupted category motion."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from PySide6.QtCore import QAbstractAnimation,QPoint,Qt
from PySide6.QtTest import QTest
import test_html_native_workspace as w
import ui_theme,shelf_ui_common


class BatchCloudTests(unittest.TestCase):
    def setUp(self):
        self.fixture=w.WorkspaceTests();self.fixture.setUp()
        self.shelf=self.fixture.shelf;self.manager=self.fixture.manager
        self.refs=self.fixture.paths;self.status={self.refs[0]:'local',self.refs[1]:'synced',self.refs[2]:'paused'}
        self.sync=w.SyncView(service=SimpleNamespace(signed_in=True),upload_enabled=True,
            sync_status_for_ref=lambda ref:self.status.get(ref,'local'),
            sync_statuses_for_refs=lambda refs:{ref:self.status.get(ref,'local') for ref in refs},
            cloud_copy_busy_for_ref=lambda ref:ref in self.busy,
            retry_upload=Mock(side_effect=self.started),delete_cloud_copy=Mock(side_effect=self.removed),
            is_upload_paused=lambda ref:self.status[ref]=='paused')
        self.busy=set();self.manager.sync=self.sync;self.callbacks=[];self.notices=[]
        self.manager.check_cloud_capacity=lambda refs,done,**kw:self.callbacks.append((refs[0],done))
        self.manager.show_cloud_capacity_notice=lambda refs:self.notices.append(tuple(refs))
        self.popup=self.fixture.overview()

    def tearDown(self):
        self.shelf.content.transfers.clear()
        self.shelf.image_cache.close();self.shelf.image_cache._pool.waitForDone(5000)
        self.fixture.tearDown()

    def started(self,ref):
        self.status[ref]='pending'
        self.shelf.content.transfers[ref]=dict(phase='queued',received=0,total=100)

    def removed(self,ref):self.busy.add(ref)

    def select_all(self):
        for ref in self.refs:self.popup.select_item(ref,toggle=bool(self.popup.selected))
        w.flush();return self.popup.preview

    def test_batch_targets_only_eligible_refs_and_never_requeues_pending_checks(self):
        preview=self.select_all();self.assertTrue(preview.group_cloud_upload.isEnabled());self.assertTrue(preview.group_cloud_remove.isEnabled())
        preview.group_cloud_upload.click();self.assertEqual({x[0] for x in self.callbacks},{self.refs[0],self.refs[2]})
        preview.group_cloud_upload.click();self.assertEqual(len(self.callbacks),2)
        # A changed cloud state and a concurrent transfer are rechecked when an
        # asynchronous capacity response arrives, before any upload is queued.
        self.status[self.refs[2]]='synced'
        for ref,callback in self.callbacks:callback(False)
        self.sync.retry_upload.assert_called_once_with(self.refs[0])
        preview.group_cloud_remove.click()
        self.assertEqual({call.args[0] for call in self.sync.delete_cloud_copy.call_args_list},{self.refs[1],self.refs[2]})
        preview.group_cloud_remove.click();self.assertEqual(self.sync.delete_cloud_copy.call_count,2)

    def test_capacity_refusal_and_active_download_never_submit_upload(self):
        preview=self.select_all();self.shelf.content.transfers[self.refs[2]]=dict(phase='downloading',remote=True,total=100,received=3)
        preview.group_cloud_upload.click();self.assertEqual([x[0] for x in self.callbacks],[self.refs[0]])
        self.callbacks[0][1](True);self.sync.retry_upload.assert_not_called()
        self.assertEqual(self.notices,[(self.refs[0],)])

    def test_cloud_delete_first_click_updates_badge_and_clears_focus(self):
        ref=self.refs[1];self.popup.select_item(ref);w.flush()
        preview=self.popup.preview;button=preview.cloud_action
        self.assertEqual(button.glyph,'cloud-remove')
        def removing(value):
            self.busy.add(value);self.popup._upload_metadata_changed(value)
        self.sync.delete_cloud_copy.side_effect=removing
        button.setFocus(Qt.TabFocusReason);w.flush()
        QTest.mouseClick(button,Qt.LeftButton);w.flush()
        self.sync.delete_cloud_copy.assert_called_once_with(ref)
        self.assertFalse(button.isEnabled())
        self.assertFalse(preview.copy_button.hasFocus() and preview.copy_button._keyboard_focus)
        self.status[ref]='local';self.busy.clear()
        self.popup._upload_metadata_changed(ref);w.flush()
        self.assertEqual(preview.status_value.text(),'仅保存在本机')
        self.assertEqual(button.glyph,'cloud-upload')
        self.sync.retry_upload.assert_not_called()

    def test_cloud_action_reveals_over_the_status_card_and_remains_keyboard_accessible(self):
        self.popup.select_item(self.refs[1]);w.flush()
        preview=self.popup.preview;card=preview.status_card;action=preview.cloud_action
        self.assertIs(action.parentWidget(),card)
        self.assertTrue(card.rect().contains(action.geometry()))
        self.assertEqual(action.y(),4)
        self.assertEqual(card.width()-action.geometry().right()-1,8)
        self.assertEqual(action.property('glyphSize'),14)
        self.assertEqual(card.height(),preview.size_card.height())
        self.assertEqual(preview.name.width(),preview.preview_head.width()-32-16-32-32)

        def red_pixels():
            image=card.grab().toImage();ratio=image.devicePixelRatio();rect=action.geometry()
            return sum(1 for y in range(round(rect.top()*ratio),round((rect.bottom()+1)*ratio))
                       for x in range(round(rect.left()*ratio),round((rect.right()+1)*ratio))
                       if (lambda color:color.red()>140 and color.red()>color.green()*1.5
                           and color.red()>color.blue()*1.5)(image.pixelColor(x,y)))

        preview.copy_button.setFocus(Qt.TabFocusReason)
        QTest.mouseMove(preview.action_footer,QPoint(3,3));w.flush()
        self.assertFalse(action._card_hovered)
        self.assertTrue(action.testAttribute(Qt.WA_TransparentForMouseEvents))
        self.assertEqual(red_pixels(),0)
        action.setFocus(Qt.TabFocusReason);w.flush()
        self.assertTrue(action.hasFocus())
        self.assertGreater(red_pixels(),0)
        preview.copy_button.setFocus(Qt.TabFocusReason);w.flush()
        self.assertEqual(red_pixels(),0)
        original_size=card.size()
        QTest.mouseMove(card,QPoint(8,8));w.flush()
        self.assertTrue(action._card_hovered)
        self.assertFalse(action.testAttribute(Qt.WA_TransparentForMouseEvents))
        self.assertGreater(red_pixels(),0)
        self.assertEqual(card.size(),original_size)
        QTest.mouseMove(action,action.rect().center());w.flush()
        self.assertTrue(action.underMouse())
        QTest.mouseClick(action,Qt.LeftButton);w.flush()
        self.sync.delete_cloud_copy.assert_called_once_with(self.refs[1])
        QTest.mouseMove(preview.action_footer,QPoint(3,3));w.flush()
        self.assertFalse(action._card_hovered)

    def test_stale_remove_button_does_not_trigger_upload(self):
        ref=self.refs[1];self.popup.select_item(ref);w.flush()
        preview=self.popup.preview
        self.assertEqual(preview.cloud_action.glyph,'cloud-remove')
        self.status[ref]='local'
        self.manager.check_cloud_capacity_for_upload=Mock()
        QTest.mouseClick(preview.cloud_action,Qt.LeftButton);w.flush()
        self.sync.retry_upload.assert_not_called()
        self.manager.check_cloud_capacity_for_upload.assert_not_called()

    def test_hover_card_upload_keeps_capacity_check(self):
        ref=self.refs[0];self.popup.select_item(ref);w.flush()
        preview=self.popup.preview;action=preview.cloud_action
        self.assertEqual(action.glyph,'cloud-upload')
        self.manager.check_cloud_capacity_for_upload=Mock(side_effect=lambda current,done:done())
        QTest.mouseMove(preview.status_card,QPoint(8,8));w.flush()
        QTest.mouseMove(action,action.rect().center());w.flush()
        self.assertTrue(action.underMouse())
        QTest.mouseClick(action,Qt.LeftButton);w.flush()
        self.manager.check_cloud_capacity_for_upload.assert_called_once()
        self.sync.retry_upload.assert_called_once_with(ref)

    def test_pending_upload_click_cannot_override_later_cloud_removal(self):
        revisions={ref:0 for ref in self.refs}
        self.sync.cloud_copy_revision=lambda ref:revisions[ref]
        preview=self.select_all();preview.group_cloud_upload.click()
        revisions[self.refs[0]]+=1
        for ref,callback in self.callbacks:
            if ref==self.refs[0]:callback(False)
        self.sync.retry_upload.assert_not_called()
        self.popup.selected.clear();self.popup.select_item(self.refs[0]);w.flush()
        pending=[];self.manager.check_cloud_capacity_for_upload=lambda ref,done:pending.append(done)
        preview.toggle_cloud_copy();revisions[self.refs[0]]+=1;pending.pop()()
        self.sync.retry_upload.assert_not_called()
        preview.toggle_cloud_copy();pending.pop()()
        self.sync.retry_upload.assert_called_once_with(self.refs[0])

    def test_footer_aggregates_bytes_and_smoothly_interrupts_retargeting(self):
        a,b,c=self.refs
        self.shelf.content.transfers.update({a:dict(phase='uploading',received=50,total=100),
            b:dict(phase='uploading',received=50,total=300),c:dict(phase='downloading',remote=True,received=90,total=100)})
        self.popup.update_transfer_status();w.flush();strip=self.popup.transfer_strip
        self.assertAlmostEqual(strip.progress_fraction,.25)
        self.assertEqual(self.popup.strip_rows[0][2].text(),'25%')
        self.assertEqual(self.popup.strip_rows[0][1].title,'2 个文件')
        self.assertFalse(self.popup.strip_more.isVisible())
        margins=strip.layout().contentsMargins()
        self.assertEqual((margins.left(),margins.right()),(12,12))
        self.manager.motion=True
        with patch('ui_transitions.motion_allowed',return_value=True):
            self.shelf.content.transfers[b]['received']=250;self.popup.update_transfer_status()
            anim=strip.progress_animation;self.assertEqual(anim.state(),QAbstractAnimation.Running)
            anim.pause();anim.setCurrentTime(80);middle=strip.paint_fraction
            self.assertGreater(middle,.25);self.assertLess(middle,.75)
            self.shelf.content.transfers[a]['received']=100;self.popup.update_transfer_status()
            self.assertAlmostEqual(anim.startValue(),middle)
            anim.pause();anim.setCurrentTime(anim.duration())
            self.assertAlmostEqual(strip.paint_fraction,.875)
            self.assertEqual(self.popup.strip_rows[0][2].text(),'88%')
            self.shelf.content.transfers[b]['total']=0;self.popup.update_transfer_status()
            self.assertIsNone(strip.paint_fraction);self.assertFalse(self.popup.strip_rows[0][2].isVisible())
        self.manager.motion=False

    def test_split_arrow_toggles_and_device_colours_match_availability(self):
        self.manager.account=SimpleNamespace(signed_in=True,device_id='self',api=SimpleNamespace(token='token'))
        peers=[dict(id='one',name='在线设备',online=True,account_device=True,account_device_id='one'),
               dict(id='two',name='离线设备',online=False,account_device=True,account_device_id='two')]
        self.manager.wan=SimpleNamespace(_bound_identity=('self','token'),peers=lambda:peers,nearby_peer=lambda _:None)
        self.manager.send_to_account_device=Mock()
        self.popup.select_item(self.refs[0]);preview=self.popup.preview
        for theme in ('light','dark'):
            self.fixture.settings.update(theme=theme);ui_theme.set_theme(theme);ui_theme.apply_application(w.APP,shelf_ui_common.FONT_FAMILY)
            QTest.mouseClick(preview.send_menu_button,Qt.LeftButton);menu=preview._send_menu
            online=next(a for a in menu.actions() if a.text()=='在线设备')
            offline=next(a for a in menu.actions() if a.text().startswith('离线设备'))
            self.assertTrue(online.isEnabled());self.assertFalse(offline.isEnabled())
            self.assertIn('color:'+(''+__import__('native_ui_common').colors(theme)['green']),online.defaultWidget().styleSheet())
            self.assertIn('#737373' if theme=='dark' else '#A3A3A3',offline.defaultWidget().styleSheet())
            QTest.mouseClick(preview.send_menu_button,Qt.LeftButton);self.assertFalse(menu.isVisible())
            self.assertEqual(self.popup.chosen_refs(),[self.refs[0]])
            w.flush()

    def test_send_dropdown_keeps_its_mouse_owner_during_the_opening_spring(self):
        self.popup.select_item(self.refs[0])
        preview=self.popup.preview
        with patch('dropdown_motion.QGuiApplication.platformName',return_value='windows'), \
             patch('native_ui_common.reduced_motion',return_value=False):
            QTest.mouseClick(preview.send_menu_button,Qt.LeftButton)
            menu=preview._send_menu
            self.assertIsNotNone(menu)
            motion=menu._dropdown_motion
            w.until(lambda:motion.presentation is not None)
            self.assertIs(motion.presentation.parentWidget(),menu)
            self.assertFalse(motion.presentation.isWindow())
            self.assertTrue(motion.presentation.testAttribute(Qt.WA_TransparentForMouseEvents))
            w.until(lambda:not motion.height._timer.isActive())
            self.assertTrue(menu.isVisible())
            self.assertEqual(menu.windowOpacity(),1.)
            QTest.keyClick(menu,Qt.Key_Escape)
            w.until(lambda:motion.presentation is None)
            self.assertIsNone(preview._send_menu)

    def test_retry_creates_only_selected_missing_journal_entry_and_skips_synced(self):
        from shelf_sync import ShelfSync
        a,b,c=self.refs;entries={};index={}
        adapter=SimpleNamespace(snapshot=lambda:dict(permanent=[a,b],texts={}),title=lambda ref:ref.split('\\')[-1])
        engine=SimpleNamespace(state={'entries':entries},account_id='account',service=SimpleNamespace(signed_in=True),
            adapter=adapter,_ref_index=index,transfer=None,
            upload_lanes=SimpleNamespace(active={},kick=Mock()),debounce=SimpleNamespace(start=Mock()),
            _entry_for_ref=lambda ref:index.get(ref),
            _sync_status_for_entry=lambda entry:'synced' if not entry.get('dirty') and entry.get('version') else 'pending',
            _end_intake=Mock(),_cancel_upload_owner=Mock(),_persist=Mock(),_notify_transfers=Mock())
        self.assertTrue(ShelfSync.retry_upload(engine,a))
        self.assertEqual(set(index),{a});self.assertEqual(len(entries),1)
        self.assertEqual(index[a]['kind'],'file');self.assertTrue(index[a]['dirty'])
        index[b]=dict(id='synced',ref=b,version=1,dirty=False);entries['synced']=index[b]
        self.assertFalse(ShelfSync.retry_upload(engine,b));self.assertFalse(index[b]['dirty'])
        self.assertFalse(ShelfSync.retry_upload(engine,c));self.assertNotIn(c,index)

    def test_file_widgets_wait_until_panel_motion_completes(self):
        for number in range(20):
            path=self.fixture.base/f'motion-{number}.dat';path.write_bytes(b'x');self.shelf.files['permanent'].append(str(path))
        self.shelf.refresh();self.popup.rebuild();self.popup.resume_content()
        w.until(lambda:self.popup._build_cursor==len(self.popup.visible_refs()))
        self.manager.motion=True
        with patch('ui_transitions.motion_allowed',return_value=True):
            self.popup.set_category('audio');self.popup.preview_width_motion.animation.setCurrentTime(200)
            self.popup.sidebar._selection_animation.setCurrentTime(self.popup.sidebar._selection_animation.duration())
            self.popup.set_category('all')
            self.assertTrue(self.popup.preview_width_motion.active)
            self.assertTrue(self.popup._file_entry_pending)
            self.assertEqual(self.popup._build_cursor,0)
            self.popup.preview_width_motion.animation.pause();self.popup.preview_width_motion.animation.setCurrentTime(100)
            self.popup._append_batch();self.assertEqual(self.popup._build_cursor,0)
            self.popup.preview_width_motion.animation.setCurrentTime(200)
            self.popup.sidebar._selection_animation.setCurrentTime(self.popup.sidebar._selection_animation.duration())
            self.popup._resume_deferred_work()
            w.until(lambda:self.popup._build_cursor>0)
            w.until(lambda:self.popup._build_cursor==len(self.popup.visible_refs()))
        self.manager.motion=False

    def test_category_indicator_interrupts_from_current_position_and_runs_with_file_entrance(self):
        popup=self.popup;self.manager.motion=True
        with patch('ui_transitions.motion_allowed',return_value=True):
            popup.set_category('files');navigation=popup.sidebar._selection_animation
            self.assertEqual(navigation.state(),QAbstractAnimation.Running)
            navigation.pause();navigation.setCurrentTime(45)
            w.until(lambda:popup.item_transitions.entry_started is not None)
            self.assertEqual(navigation.state(),QAbstractAnimation.Paused)
            self.assertLess(navigation.currentTime(),navigation.duration())
            position=popup.sidebar._selection_rect
            popup.set_category('text')
            self.assertEqual(navigation.state(),QAbstractAnimation.Running)
            self.assertEqual(navigation.startValue(),position)
            self.assertEqual(popup.category,'text')
            self.assertTrue(popup.category_buttons['text'].isChecked())
            navigation.pause();navigation.setCurrentTime(navigation.duration())
            self.assertEqual(popup.sidebar._selection_rect,popup.sidebar._selection_target)
        self.manager.motion=False


if __name__=='__main__':unittest.main(verbosity=2)
