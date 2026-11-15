"""Shelf timestamps, account-device sending, and popup indicators in isolation."""
import math
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

from PySide6.QtCore import QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget
import test_html_native_workspace as fixture
from app_settings import SettingsComboBox
from flickdock import ShelfManager
from lan_panel import _DevicesCombo
from shelf_content import ContentRepository,shelf_added_time
from shelf_sync import ShelfSync


class ShelfActionsTests(unittest.TestCase):
    def setUp(self):
        self.workspace=fixture.WorkspaceTests();self.workspace.setUp()
        self.shelf=self.workspace.shelf;self.manager=self.workspace.manager

    def tearDown(self):
        self.shelf.image_cache.close()
        self.shelf.image_cache._pool.waitForDone(5000)
        self.workspace.tearDown()

    def test_more_menu_is_nonblocking_pressed_and_toggle_closes_it(self):
        self.shelf.show();fixture.flush()
        with patch('shelf_window.AnimatedMenu.exec',side_effect=AssertionError('nested menu loop')):
            self.shelf.show_menu();fixture.flush()
            menu=self.shelf._more_menu
            self.assertTrue(menu.isVisible());self.assertTrue(self.shelf.menu_button.isDown())
            self.shelf.show_menu();fixture.flush()
            self.assertIsNone(self.shelf._more_menu)
            self.assertFalse(self.shelf._menu_open);self.assertFalse(self.shelf.menu_button.isDown())

    def test_list_and_preview_use_intake_time_for_every_cloud_state(self):
        ref=self.workspace.paths[1];stamp=1760000000.0
        self.shelf.content.metadata[ref]['added_at']=stamp
        state={'value':'local'}
        self.manager.sync=fixture.SyncView(sync_statuses_for_refs=lambda refs:{r:state['value'] for r in refs},
            sync_status_for_ref=lambda _:state['value'],is_upload_paused=lambda _:False,
            cached_upload_time_for_ref=lambda _:'2026-11-04T01:02:03Z')
        popup=self.workspace.overview();popup.select_item(ref);row=popup._item_widgets[ref]
        expected=datetime.fromtimestamp(stamp).strftime('%Y-%m-%d %H:%M:%S')
        for status in ('local','pending','uploading','synced','paused','error'):
            state['value']=status;popup.refresh_cloud_status();fixture.flush()
            self.assertTrue(row.detail_label.text().endswith(expected),status)
            self.assertEqual(popup.preview.upload_time.full_text,'放入时间：'+expected,status)
        self.assertEqual(popup.upload_caption(ref,compact=True),datetime.fromtimestamp(stamp).strftime('%m-%d %H:%M:%S'))
        self.shelf.content.metadata[ref].pop('added_at');popup.preview.refresh_cloud_status();row.refresh_upload_time()
        self.assertEqual(popup.preview.upload_time.full_text,'放入时间：暂无记录')
        self.assertTrue(row.detail_label.text().endswith('放入时间暂无记录'))

    def test_invalid_or_missing_timestamps_are_never_guessed(self):
        content=ContentRepository(metadata={'x':{'added_at':1}})
        for bad in (None,True,'2026',-1,0,math.nan,math.inf,10**100):
            content.metadata['x']['added_at']=bad
            self.assertIsNone(shelf_added_time(content,'x'),repr(bad))
        content.pending['flickdock:transfer:x']={'added_at':1760000000.0}
        self.assertTrue(shelf_added_time(content,'flickdock:transfer:x'))

    def test_resolved_link_keeps_the_original_shelf_time(self):
        ref=self.workspace.paths[1];self.shelf.content.metadata[ref]['added_at']=1760000000.0
        result=self.workspace.base/'resolved.png';result.write_bytes(b'image')
        self.shelf._resolved_content(ref,{'path':str(result),'title':'resolved'})
        self.assertEqual(self.shelf.content.metadata[str(result)]['added_at'],1760000000.0)

    def test_remote_insert_records_local_intake_and_revisions_keep_it(self):
        view={'permanent':[],'texts':{},'metadata':{}}
        subject=SimpleNamespace(state={'entries':{}},adapter=SimpleNamespace(),_change_view=lambda fn:fn(view))
        with patch('shelf_sync.time.time',return_value=1760000000.0):
            ShelfSync._add_ref(subject,'one',None,'one',cloud_identity='id')
        self.assertEqual(view['metadata']['one']['added_at'],1760000000.0)
        with patch('shelf_sync.time.time',return_value=1761000000.0):
            ShelfSync._add_ref(subject,'two',None,'two',cloud_identity='id',replacing='one')
        self.assertEqual(view['metadata']['two']['added_at'],1760000000.0)
        view['metadata']['two'].pop('added_at')
        ShelfSync._add_ref(subject,'three',None,'three',cloud_identity='id',replacing='two')
        self.assertNotIn('added_at',view['metadata']['three'])

    def test_menu_only_lists_other_same_account_devices_and_preserves_fallback(self):
        account=SimpleNamespace(signed_in=True,account={'username':'Roger'},device_id='self',api=SimpleNamespace(token='token'))
        rows=[dict(id='remote',name='MacBook Air',account_device=True,account_device_id='mac',online=True),
              dict(id='offline',name='离线电脑',account_device=True,account_device_id='pc',online=False),
              dict(id='other',name='陌生设备',account_device=False,online=True),
              dict(id='self-peer',name='本机',account_device=True,account_device_id='self',online=True)]
        self.manager.account=account
        self.manager.wan=SimpleNamespace(_bound_identity=('self','token'),peers=lambda:rows,nearby_peer=lambda _:None)
        self.manager.send_to_account_device=Mock()
        popup=self.workspace.overview();ref=self.workspace.paths[0];popup.select_item(ref);preview=popup.preview
        self.assertEqual(preview.send_menu_button.accessibleName(),'选择同账号设备')
        self.assertTrue(preview.send_button.icon().isNull())
        self.assertFalse(preview.send_menu_button.icon().isNull())
        self.assertIs(preview.send_button.parentWidget(),preview.send_control)
        self.assertGreater(preview.send_menu_button.geometry().left(),preview.send_button.geometry().left())
        preview.send_button.click();self.assertEqual(self.workspace.events[-1],('lan',[str(Path(ref).absolute())]))
        preview.send_menu_button.click();menu=preview._send_menu
        names=[action.text() for action in menu.actions()]
        self.assertIn('MacBook Air',names);self.assertIn('离线电脑 · 离线',names)
        self.assertNotIn('陌生设备',names);self.assertNotIn('本机',names)
        offline=next(a for a in menu.actions() if a.text().startswith('离线电脑'))
        self.assertFalse(offline.isEnabled())
        next(a for a in menu.actions() if a.text()=='MacBook Air').trigger()
        self.manager.send_to_account_device.assert_called_once_with('remote',[str(Path(ref).absolute())])
        menu.close();fixture.flush()
        account.signed_in=False;preview.send_menu_button.click();menu=preview._send_menu
        self.assertEqual(menu.actions()[0].text(),'登录后可选择同账号设备')
        menu.actions()[-1].trigger();menu.close()
        self.assertEqual(self.workspace.events[-1],('lan',[str(Path(ref).absolute())]))
        account.signed_in=True;other=self.workspace.paths[1]
        popup.select_item(ref);popup.select_item(other,toggle=True);fixture.flush()
        self.assertTrue(preview.group_view.isVisible())
        preview.group_send_menu.click();menu=preview._send_menu
        next(a for a in menu.actions() if a.text()=='MacBook Air').trigger();menu.close()
        self.manager.send_to_account_device.assert_called_with('remote',[str(Path(path).absolute()) for path in popup.chosen_refs()])

    def test_send_routes_lan_first_then_remote_and_keeps_failed_paths(self):
        account=SimpleNamespace(signed_in=True,device_id='self',api=SimpleNamespace(token='token'))
        peer=dict(id='wan-peer',account_device=True,account_device_id='mac',online=True)
        routes=[];near={'id':'lan-peer'}
        wan=SimpleNamespace(_bound_identity=('self','token'),peers=lambda:[peer],nearby_peer=lambda _:near)
        lan=object();panel=SimpleNamespace(devices=SimpleNamespace(findData=lambda _: -1),
            send_paths=Mock(return_value='transfer-id'),queue_paths=Mock(),status_label=SimpleNamespace(setText=Mock()))
        manager=SimpleNamespace(account=account,wan=wan,lan=lan,lan_dialog=SimpleNamespace(panel=panel),
            _show_transfer_dialog=lambda service:routes.append(service),show_wan_dialog=Mock())
        ShelfManager.send_to_account_device(manager,'wan-peer',['file.txt'])
        self.assertIs(routes[-1],lan);panel.send_paths.assert_called_with('lan-peer',['file.txt'])
        near=None;panel.send_paths.return_value=''
        ShelfManager.send_to_account_device(manager,'wan-peer',['file.txt'])
        self.assertIs(routes[-1],wan);panel.send_paths.assert_called_with('wan-peer',['file.txt'])
        panel.queue_paths.assert_called_once_with(['file.txt'])
        peer['account_device']=False;panel.send_paths.reset_mock()
        ShelfManager.send_to_account_device(manager,'wan-peer',['file.txt'])
        panel.send_paths.assert_not_called()

    def test_combo_arrows_animate_and_reverse_on_interruption(self):
        for combo_type in (SettingsComboBox,_DevicesCombo):
            combo=combo_type();combo.addItems(['一','二']);combo.resize(180,32);combo.show();fixture.flush()
            with patch('native_ui_common.reduced_motion',return_value=False):
                combo.showPopup();fixture.flush();motion=combo._popup_chevron
                self.assertEqual(motion.animation.state(),motion.animation.Running)
                motion.animation.pause();motion.animation.setCurrentTime(55)
                self.assertGreater(motion.angle,0);self.assertLess(motion.angle,180)
                current=motion.angle;combo.hidePopup();fixture.flush()
                self.assertEqual(motion.animation.state(),motion.animation.Running)
                self.assertAlmostEqual(motion.animation.startValue(),current,places=1)
                motion.animation.pause();motion.animation.setCurrentTime(motion.animation.duration())
                self.assertAlmostEqual(motion.angle,0,places=1)
            combo.close();combo.deleteLater();fixture.flush()


if __name__=='__main__':unittest.main()
