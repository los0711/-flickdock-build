"""Latest utility, sorting, notification and per-display paths, isolated Qt."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PySide6.QtCore import QRect,Qt,QPoint
from PySide6.QtTest import QTest
from test_html_native_workspace import APP,flush,until
import test_html_native_workspace as fixture
from app_settings import SettingsStore,SettingsDialog
from display_triggers import display_id,display_values,DisplayTriggerEditor
from wan_transfer import WanTransferService
from lan_panel import LanPanel


class OrderingTests(unittest.TestCase):
    setUp=fixture.WorkspaceTests.setUp
    tearDown=fixture.WorkspaceTests.tearDown
    overview=fixture.WorkspaceTests.overview

    def test_each_sort_cycles_in_place_without_mutating_source_or_selection(self):
        for ref,size in zip(self.paths,(90,10,40)):Path(ref).write_bytes(b'x'*size)
        popup=self.overview();popup.select_item(self.paths[1]);flush()
        initial=list(self.shelf.files['permanent']);metadata=json.dumps(self.shelf.content.metadata,sort_keys=True)
        rows=dict(popup._item_widgets);preview=popup.preview.current_path
        self.assertEqual(popup.visible_refs(),list(reversed(initial)))
        for key,ascending in (('time',initial),('size',[self.paths[1],self.paths[2],self.paths[0]])):
            for expected in (ascending,list(reversed(ascending)),list(reversed(initial))):
                popup.sort_buttons[key].click();flush()
                until(lambda:popup.visible_refs()==expected and not (hasattr(popup,'_size_sort_timer') and popup._size_sort_timer.isActive()))
                self.assertEqual(popup.visible_refs(),expected)
                self.assertEqual(popup.selected,{self.paths[1]});self.assertEqual(popup.preview.current_path,preview)
                self.assertTrue(all(popup._item_widgets[ref] is rows[ref] for ref in self.paths))
                self.assertEqual(self.shelf.files['permanent'],initial)
                self.assertEqual(json.dumps(self.shelf.content.metadata,sort_keys=True),metadata)
        self.assertEqual(popup.sort_direction,0)

    def test_sort_remains_live_for_new_item_and_equal_sizes_are_stable(self):
        popup=self.overview();popup.cycle_sort('size');flush()
        path=self.base/'new.bin';path.write_bytes(b'1');self.shelf.add_paths('permanent',[str(path)]);flush()
        self.assertEqual(popup.visible_refs()[0],str(path))
        self.assertEqual(popup.sort_key,'size')

    def test_last_chosen_sort_resets_other_direction(self):
        popup=self.overview()
        for key in ('time','size','time','size'):
            popup.sort_buttons[key].click();flush()
            other='size' if key=='time' else 'time'
            self.assertEqual(popup.sort_key,key)
            self.assertEqual(popup.sort_direction,1)
            self.assertEqual(popup.sort_buttons[key].direction,1)
            self.assertEqual(popup.sort_buttons[other].direction,0)
        popup.sort_buttons['size'].click();flush()
        self.assertEqual(popup.sort_buttons['size'].direction,2)
        self.assertEqual(popup.sort_buttons['time'].direction,0)
        popup.selection_hint.click();flush()
        self.assertIsNone(popup.sort_key)
        self.assertTrue(all(control.direction==0 for control in popup.sort_buttons.values()))

    def test_sort_pair_arrows_and_default_reset_even_when_order_is_unchanged(self):
        from reference_widgets import palette
        popup=self.overview();initial=list(popup.visible_refs())
        self.assertEqual(popup.selection_hint.text(),'默认')
        for key in ('time','size'):
            control=popup.sort_buttons[key]
            self.assertEqual(control.geometry().center().y(),popup.selection_hint.geometry().center().y())
            for direction in (1,2,0):
                control.click();flush();self.assertEqual(control.direction,direction)
                self.assertEqual(control.arrow_colors(),tuple(palette()['link'] if direction==value else palette()['muted'] for value in (1,2)))
                self.assertEqual(control.text(),'时间' if key=='time' else '大小')
        popup.cycle_sort('time');popup.cycle_sort('time');flush()
        self.assertEqual(popup.visible_refs(),initial)
        popup.selection_hint.click();flush()
        self.assertEqual(popup.visible_refs(),initial);self.assertIsNone(popup.sort_key)
        self.assertTrue(all(control.direction==0 for control in popup.sort_buttons.values()))

    def test_shelf_limit_rejects_only_over_limit_and_directory_scan_precedes_import(self):
        from shelf_intake_limit import MAX_SHELF_BYTES,classify
        import threading
        self.assertEqual(MAX_SHELF_BYTES,1024**3)
        small=self.base/'at-limit.bin';small.write_bytes(b'12345678')
        large=self.base/'over-limit.bin';large.write_bytes(b'123456789')
        directory=self.base/'oversize-folder';directory.mkdir();(directory/'one').write_bytes(b'12345');(directory/'two').write_bytes(b'1234')
        rejected=[];self.manager.show_oversize_notice=lambda paths:rejected.extend(paths)
        with patch('shelf_intake_limit.MAX_SHELF_BYTES',8):
            self.assertEqual(classify([str(small),str(large),str(directory)],threading.Event()),([str(small)],[str(large),str(directory)]))
            self.shelf.import_paths('permanent',[str(small),str(large),str(directory)])
            until(lambda:not getattr(self.shelf,'_intake_limit_jobs',set()))
            self.assertIn(str(small),self.shelf.files['permanent'])
            self.assertNotIn(str(large),self.shelf.files['permanent']);self.assertNotIn(str(directory),self.shelf.files['permanent'])
        self.assertEqual(set(rejected),{str(large),str(directory)})
        self.assertEqual(large.read_bytes(),b'123456789');self.assertTrue((directory/'one').exists())


class UtilityPreferenceTests(unittest.TestCase):
    def test_live_handle_repaints_color_and_opacity_when_geometry_does_not_change(self):
        from PySide6.QtCore import QObject,QEvent
        from unittest.mock import Mock
        from flickdock import DockHandle,ShelfManager
        class PaintCounter(QObject):
            def __init__(self):super().__init__();self.paints=0
            def eventFilter(self,widget,event):
                if event.type()==QEvent.Paint:self.paints+=1
                return False
        with tempfile.TemporaryDirectory() as directory:
            manager=SimpleNamespace(settings=SettingsStore(Path(directory)),motion=False,_dock_preview_values=None,
                _quitting=False,settings_open=lambda:True,_settings_prompt_active=lambda:False,
                dock_preview=SimpleNamespace(hide=Mock()),settings_edge_hint=SimpleNamespace(hide=Mock()))
            edge=DockHandle(manager,APP.primaryScreen());manager.edge=edge;manager.edges={'primary':edge}
            manager._position_edges=lambda:ShelfManager._position_edges(manager)
            manager.update_handles=lambda:ShelfManager.update_handles(manager)
            try:
                edge.show_handle();flush();counter=PaintCounter();edge.installEventFilter(counter)
                geometry=edge.geometry();ShelfManager.preview_dock(manager,dict(dock_color='#ff0000',dock_opacity=50));flush()
                self.assertEqual(edge.geometry(),geometry);self.assertGreater(counter.paints,0)
                self.assertEqual((edge.values()['dock_color'],edge.values()['dock_opacity']),('#ff0000',50))
            finally:edge.hide();edge.deleteLater();flush()

    def test_settings_and_transfer_reset_independent_defaults_before_show(self):
        from test_html_native_lan import FakeLan
        from lan_panel import LanDialog
        from window_chrome import WindowChrome
        with tempfile.TemporaryDirectory() as directory,patch.object(WindowChrome,'_available',return_value=QRect(0,0,1920,1080)):
            store=SettingsStore(Path(directory));settings=SettingsDialog(store);transfer=LanDialog(FakeLan(),store)
            try:
                for view,default in ((settings,(892,632)),(transfer,(980,704))):
                    view.show();flush();surface=view.surface if view is settings else view.card
                    handle=id(surface);view.resize(1120,820);flush();view.hide()
                    view.show();self.assertEqual(view.size().toTuple(),default);self.assertEqual(id(surface),handle)
                    view.showMaximized();flush();view.hide();view.show();flush()
                    self.assertFalse(view.isMaximized());self.assertEqual(view.size().toTuple(),default)
                    self.assertIsNone(surface.graphicsEffect())
                self.assertFalse((Path(directory)/'window-sizes.json').exists())
                self.assertIn('上传成功后保留 24 小时',settings.retention_description.text())
                settings.clipboard_success_checkbox.setChecked(False)
                self.assertFalse(SettingsStore(Path(directory)).clipboard_success_notice)
                self.assertTrue(store.clipboard_sync_enabled)
            finally:
                for view in (settings,transfer):view.hide();view.deleteLater()
                flush()

    def test_display_profiles_survive_hotplug_order_and_resolution(self):
        class Screen:
            def __init__(self,name,serial):self._name=name;self.serial=serial
            def name(self):return self._name
            def manufacturer(self):return 'Maker'
            def model(self):return 'Panel'
            def serialNumber(self):return self.serial
        main=Screen('DISPLAY1','111');second=Screen('DISPLAY2','222');third=Screen('DISPLAY3','333')
        with tempfile.TemporaryDirectory() as directory,patch('display_triggers.QGuiApplication.primaryScreen',return_value=main),patch('display_triggers.QGuiApplication.screens',return_value=[main,second]) as screens:
            store=SettingsStore(Path(directory));editor=DisplayTriggerEditor(store)
            try:
                self.assertTrue(display_values(store,main)['enabled']);self.assertFalse(display_values(store,second)['enabled'])
                row=editor.rows[display_id(second)];row.toggle.setChecked(True);row.edge.setCurrentIndex(row.edge.findData('left'));row.controls['position'].setValue(73)
                self.assertFalse(store.display_triggers);store.update(display_triggers=editor.profiles())
                self.assertEqual(display_values(store,second)['dock_position'],73);self.assertEqual(display_values(store,main)['dock_position'],8)
                screens.return_value=[main,third];editor.refresh();self.assertNotIn(display_id(second),editor.rows)
                screens.return_value=[third,second,main];editor.refresh()
                self.assertEqual(editor.rows[display_id(second)].controls['position'].value(),73)
                loaded=SettingsStore(Path(directory));self.assertEqual(loaded.display_triggers,store.display_triggers)
                self.assertEqual(display_id(Screen('DISPLAY2','222')),display_id(second))
                # A newly-primary display without a profile must update both
                # its UI and runtime before any position-only edit is saved.
                with patch('display_triggers.QGuiApplication.primaryScreen',return_value=third):
                    editor.refresh();self.assertTrue(editor.rows[display_id(third)].toggle.isChecked())
                    editor.rows[display_id(third)].controls['position'].setValue(61)
                    store.update(display_triggers=editor.profiles())
                    self.assertTrue(display_values(store,third)['enabled'])
            finally:editor.deleteLater();flush()

    def test_copy_card_coalesces_and_uses_real_preview_types_at_top_right(self):
        from intake_notice import ClipboardNotice
        from PySide6.QtGui import QImage,QColor
        with tempfile.TemporaryDirectory() as directory,patch('native_ui_common.reduced_motion',return_value=True):
            manager=SimpleNamespace(settings=SimpleNamespace(theme='dark'));notice=ClipboardNotice(manager)
            path=Path(directory)/'original.txt';path.write_text('original')
            image=QImage(8,8,QImage.Format_RGB32);image.fill(QColor('red'))
            try:
                for value in (('text',{'text':'hello'}),('text',{'text':'https://example.com'}),('image',{'image':image}),('files',[str(path)])):
                    notice.show_result(True,dict(value=value,point=(10,10)));flush()
                    self.assertEqual(notice.message.text(),'复制已同步');self.assertTrue(notice.preview.text())
                    area=APP.primaryScreen().availableGeometry()
                    self.assertEqual(notice.pos(),QPoint(area.right()-notice.width()-15,area.top()+16))
                    self.assertTrue(notice.windowFlags()&Qt.WindowStaysOnTopHint);self.assertTrue(notice.testAttribute(Qt.WA_ShowWithoutActivating))
                notice.dismiss();self.assertFalse(notice.isVisible())
            finally:
                until(lambda:not notice.jobs);notice.deleteLater();flush()

    def test_rapid_copy_notice_discards_superseded_results(self):
        from intake_notice import ClipboardNotice
        with patch('native_ui_common.reduced_motion',return_value=True):
            notice=ClipboardNotice(SimpleNamespace(settings=SimpleNamespace(theme='dark')))
            try:
                with patch.object(notice,'show_result',wraps=notice.show_result) as display:
                    notice.schedule_result(False,dict(id='old',value=('text',{'text':'old'})))
                    notice.supersede()
                    notice.schedule_result(True,dict(id='latest',value=('text',{'text':'latest'})))
                    self.assertFalse(notice.isVisible());QTest.qWait(190);flush()
                    display.assert_called_once();self.assertEqual(display.call_args.args[1]['id'],'latest')
                    self.assertEqual(notice.message.text(),'复制已同步')
                    self.assertEqual(notice.preview.text(),'latest')
                    notice.supersede();self.assertFalse(notice.isVisible())
            finally:notice.hide();notice.deleteLater();flush()


class CustomClientTests(unittest.TestCase):
    def test_server_ack_only_custom_state_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            service=WanTransferService(directory,enabled=False);sent=[];service._connected=True
            service._send_signal=lambda value:sent.append(value) or True
            try:
                service.set_custom_code('0123');operation=sent[-1]['id']
                self.assertNotEqual(service.custom_identity().get('status'),'available')
                with patch('wan_transfer.time.monotonic',return_value=10):
                    service._message(json.dumps(dict(schema_version=1,type='custom_code',operation=operation,code='0123',nonce='a'*32,status='available',expires=610000,serverNow=10000)))
                    self.assertEqual(service.custom_identity()['remaining'],600)
                with patch('wan_transfer.time.monotonic',return_value=611):self.assertEqual(service.custom_identity()['status'],'expired')
                service.cancel_custom_code();self.assertEqual(sent[-1]['nonce'],'a'*32)
                with self.assertRaises(ValueError):service.set_custom_code('中文123456')
            finally:service.close();service.deleteLater();flush()

    def test_each_dropdown_delete_does_not_change_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            service=WanTransferService(directory,enabled=False);service._connected=True
            peers=[dict(id=c*64,name=c,online=True) for c in ('a','b')];service._peers={row['id']:row for row in peers}
            service._send_signal=lambda _:True;removed=[]
            service.remove_peer=removed.append
            panel=LanPanel(service,settings=SettingsStore(Path(directory)));panel.show();flush()
            try:
                panel.devices.setCurrentIndex(0);panel.devices.showPopup();flush()
                view=panel.devices.view();index=panel.devices.model().index(1,0);rect=view.visualRect(index)
                QTest.mouseClick(view.viewport(),Qt.LeftButton,pos=QPoint(rect.right()-14,rect.center().y()));flush()
                self.assertEqual(removed,['b'*64]);self.assertEqual(panel.devices.currentData(),'a'*64)
                self.assertTrue(view.isVisible());service.peers_changed.emit(peers[:1]);flush()
                self.assertEqual(panel.devices.count(),1);self.assertTrue(view.isVisible())
            finally:panel.devices.hidePopup();panel.hide();panel.deleteLater();service.close();service.deleteLater();flush()


class AutomaticUpdateTests(unittest.TestCase):
    def test_default_downloads_in_background_and_waits_for_explicit_install(self):
        import test_update_inline_v171 as update_fixture
        case=update_fixture.InlineUpdates();case.setUp()
        try:
            self.assertTrue(case.store.automatic_updates)
            case.controller.check_automatically();case.client.available(case.manifest,False)
            self.assertEqual(case.controller.state,'downloading')
            self.assertEqual([row[0] for row in case.client.calls],['check','download'])
            self.assertFalse(case.dialog.isVisible());self.assertEqual(case.events,[])
            case.client.complete(case.path,case.manifest)
            self.assertEqual(case.controller.state,'ready');self.assertEqual(case.events,[])
            previous=list(case.client.calls);case.controller.check_automatically()
            self.assertEqual(case.client.calls,previous)
            case.store.update(automatic_updates=False)
            self.assertFalse(case.controller.check_automatically())
            self.assertFalse(SettingsStore(case.store.base).automatic_updates)
            self.assertTrue(case.controller.open(manual=True))
        finally:case.tearDown()


if __name__=='__main__':unittest.main()
