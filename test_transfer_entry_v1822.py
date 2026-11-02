"""Drag the real entries, preserving original paths and mode service state."""
import tempfile,unittest,threading,time
from unittest.mock import patch
from pathlib import Path
from PySide6.QtCore import QPoint,QPointF,QMimeData,QUrl,Qt,QTimer
from PySide6.QtGui import QDragEnterEvent,QDragMoveEvent,QDropEvent,QDragLeaveEvent
from PySide6.QtWidgets import QWidget,QPushButton
from test_html_native_workspace import APP,flush,until
from transfer_entry import TransferEntryRouter
from app_settings import SettingsStore
from lan_panel import LanDialog
from test_html_native_lan import FakeLan
from wan_transfer import WanTransferService


class EntryTests(unittest.TestCase):
    def test_mixed_native_text_does_not_disable_actual_files_or_change_their_mime(self):
        from types import SimpleNamespace,MethodType
        from PySide6.QtWidgets import QMenu
        from shelf_content import ContentRepository
        from flickdock import ShelfWindow
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);original=root/'original.txt';original.write_text('original')
            backed=root/'pasted.txt';backed.write_text('backed')
            content=ContentRepository();text=content.add_text('native plain text');link=content.add_text('https://example.invalid')
            pasted=content.add_text('pasted text');content.metadata[pasted]={'text_file':str(backed)}
            calls=[];shelf=SimpleNamespace(content=content,manager=SimpleNamespace(queue_transfer_paths=lambda *args:calls.append(args)))
            shelf.transfer_paths_for_refs=MethodType(ShelfWindow.transfer_paths_for_refs,shelf)
            before=content.mime_for([text]).text();metadata=dict(content.metadata)
            for refs,enabled in (([str(original),text,link,pasted],True),([text,link],False),([str(original),'flickdock:transfer:pending'],False)):
                menu=QMenu();ShelfWindow.add_transfer_actions(shelf,menu,refs)
                self.assertEqual([a.isEnabled() for a in menu.actions()],[enabled,enabled])
                if enabled:
                    for action in menu.actions():action.trigger()
                menu.deleteLater()
            self.assertEqual(calls,[('lan',[str(original),str(backed)]),('wan',[str(original),str(backed)])])
            self.assertEqual(content.metadata,metadata);self.assertEqual(content.mime_for([text]).text(),before)
            self.assertEqual(sorted(p.name for p in root.iterdir()),['original.txt','pasted.txt'])

    def chooser(self):
        root=QWidget();root.resize(440,420);button=QPushButton('文件互传',root);button.setGeometry(20,360,180,34)
        calls=[];router=TransferEntryRouter(root,button,lambda mode,paths:calls.append((mode,paths)))
        root.show();flush();data=QMimeData();data.setUrls([QUrl.fromLocalFile('/not-mounted/original.bin')])
        APP.sendEvent(button,QDragEnterEvent(QPoint(30,20),Qt.CopyAction,data,Qt.LeftButton,Qt.NoModifier))
        APP.sendEvent(router.surface,QDragEnterEvent(QPoint(60,25),Qt.CopyAction,data,Qt.LeftButton,Qt.NoModifier))
        router.entrance.setCurrentTime(160)
        return root,router,data,calls

    def test_selected_chooser_ants_change_real_border_pixels_without_changing_other_region(self):
        with patch('native_ui_common.reduced_motion',return_value=False),patch('transfer_entry.reduced_motion',return_value=False):
            root,router,data,calls=self.chooser()
            try:
                surface=router.surface
                for y,selected_border,other_border in ((25,4,72),(95,72,4)):
                    APP.sendEvent(surface,QDragMoveEvent(QPoint(60,y),Qt.CopyAction,data,Qt.LeftButton,Qt.NoModifier))
                    motion=surface.drop_motion;self.assertTrue(motion.timer.isActive());geometry=surface.geometry()
                    motion.phase=0.;first=surface.grab().toImage();motion.phase=2.5;second=surface.grab().toImage()
                    selected_changes=sum(first.pixelColor(x,selected_border)!=second.pixelColor(x,selected_border) for x in range(15,surface.width()-15))
                    other_changes=sum(first.pixelColor(x,other_border)!=second.pixelColor(x,other_border) for x in range(15,surface.width()-15))
                    self.assertGreater(selected_changes,8);self.assertEqual(other_changes,0);self.assertEqual(surface.geometry(),geometry)
                self.assertEqual(calls,[])
            finally:root.hide();root.deleteLater();flush()

    def test_chooser_leaving_gap_hidden_and_reduced_motion_stop_one_finite_clock(self):
        with patch('native_ui_common.reduced_motion',return_value=False),patch('transfer_entry.reduced_motion',return_value=False):
            root,router,data,calls=self.chooser()
            try:
                surface=router.surface
                def move(point):APP.sendEvent(surface,QDragMoveEvent(point,Qt.CopyAction,data,Qt.LeftButton,Qt.NoModifier))
                move(QPoint(60,25));motion=surface.drop_motion;identity=motion.timer.timerId();self.assertEqual(len(motion.findChildren(QTimer)),1)
                for _ in range(8):move(QPoint(62,25));self.assertEqual(motion.timer.timerId(),identity)
                move(QPoint(60,95));self.assertEqual(surface.active,'wan');self.assertEqual(motion.timer.timerId(),identity)
                APP.sendEvent(surface,QDragLeaveEvent());self.assertFalse(motion.timer.isActive())
                phase=motion.phase;motion._tick();self.assertEqual(motion.phase,phase)
                APP.sendEvent(surface,QDragEnterEvent(QPoint(60,25),Qt.CopyAction,data,Qt.LeftButton,Qt.NoModifier))
                move(QPoint(60,25));self.assertTrue(motion.timer.isActive())
                move(QPoint(60,69));self.assertIsNone(surface.active);self.assertFalse(motion.timer.isActive())
                move(QPoint(60,25))
                with patch('native_ui_common.reduced_motion',return_value=True):
                    motion._tick();self.assertFalse(motion.timer.isActive());phase=motion.phase;motion._tick();self.assertEqual(motion.phase,phase)
                move(QPoint(60,95));root.hide();flush();self.assertFalse(motion.timer.isActive())
            finally:root.hide();root.deleteLater();flush()

    def test_chooser_drop_stops_ants_while_preserving_original_delivery_and_arc(self):
        with patch('native_ui_common.reduced_motion',return_value=False),patch('transfer_entry.reduced_motion',return_value=False):
            root,router,data,calls=self.chooser()
            try:
                surface=router.surface
                APP.sendEvent(surface,QDragMoveEvent(QPoint(60,95),Qt.CopyAction,data,Qt.LeftButton,Qt.NoModifier))
                self.assertTrue(surface.drop_motion.timer.isActive())
                event=QDropEvent(QPointF(60,95),Qt.CopyAction,data,Qt.LeftButton,Qt.NoModifier)
                APP.sendEvent(surface,event);self.assertTrue(event.isAccepted());self.assertFalse(surface.drop_motion.timer.isActive())
                self.assertEqual(calls,[('wan',['/not-mounted/original.bin'])]);self.assertTrue(router.drop_feedback.isVisible())
            finally:root.hide();root.deleteLater();flush()

    def test_mode_transition_captures_the_painted_old_page_not_an_empty_surface(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);lan=FakeLan();wan=WanTransferService(base/'wan',enabled=False)
            dialog=LanDialog(lan,SettingsStore(base/'profile'));dialog.show();flush()
            try:
                from PySide6.QtCore import QRect
                old=dialog.panel;area=QRect(old.drop_zone.mapTo(dialog.panel_stack,QPoint()),old.drop_zone.size())
                before=dialog.panel_stack.grab().toImage().copy(area)
                with patch('ui_transitions.motion_allowed',return_value=True),patch('native_ui_common.reduced_motion',return_value=False):
                    dialog.set_service(wan)
                self.assertFalse(dialog._page_fade.snapshot.isNull())
                self.assertEqual(dialog._page_fade.snapshot.toImage().copy(area),before)
                until(lambda:not dialog._page_fade.isVisible())
                self.assertIs(dialog.panel.service,wan);self.assertTrue(dialog.panel.drop_zone.isVisible())
            finally:dialog.hide();dialog.deleteLater();wan.close();wan.deleteLater();flush()

    def test_slow_network_path_information_never_blocks_mode_switch_or_duplicates_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);path=base/'slow.bin';path.write_bytes(b'original')
            lan=FakeLan();wan=WanTransferService(base/'wan',enabled=False)
            dialog=LanDialog(lan,SettingsStore(base/'profile'));dialog.show();dialog.set_service(wan);dialog.set_service(lan);flush()
            entered=threading.Event();release=threading.Event();calls=[];native_stat=Path.stat;main=threading.get_ident()
            def slow_stat(candidate,*args,**kwargs):
                if str(candidate)==str(path):
                    calls.append(threading.get_ident())
                    if threading.get_ident()==main:raise AssertionError('File probe blocked GUI')
                    entered.set();release.wait(3)
                return native_stat(candidate,*args,**kwargs)
            try:
                with patch('file_metadata.Path.stat',side_effect=None,autospec=True) as probe:
                    probe.side_effect=slow_stat
                    dialog.panel.queue_paths([str(path)]);until(entered.is_set)
                    before=time.perf_counter();panel=dialog.set_service(wan)
                    self.assertLess(time.perf_counter()-before,.15)
                    self.assertEqual(panel._queued_paths,[str(path)]);self.assertEqual(len(calls),1)
                    release.set();until(lambda:panel.file_metadata.request(str(path)) is not None)
                self.assertEqual(panel.file_metadata.request(str(path))['size'],len(b'original'))
            finally:release.set();dialog.hide();dialog.deleteLater();wan.close();wan.deleteLater();flush()

    def test_drag_choose_modes_preserves_all_original_paths_and_does_not_import_shelf(self):
        root=QWidget();root.resize(440,420);button=QPushButton('文件互传',root);button.setGeometry(20,360,180,34)
        calls=[];router=TransferEntryRouter(root,button,lambda mode,paths:calls.append((mode,paths)))
        root.show();flush()
        mime=QMimeData();paths=[(Path(tempfile.gettempdir())/'flickdock original'/name).as_posix()
                              for name in ('file one.bin','file two.bin')]
        mime.setUrls([QUrl.fromLocalFile(path) for path in paths])
        try:
            for mode,y in (('lan',25),('wan',77)):
                enter=QDragEnterEvent(button.rect().center(),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier)
                APP.sendEvent(button,enter);self.assertTrue(enter.isAccepted());self.assertTrue(router.surface.isVisible())
                point=QPoint(60,y)
                APP.sendEvent(router.surface,QDragEnterEvent(point,Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier))
                move=QDragMoveEvent(point,Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier)
                APP.sendEvent(router.surface,move);self.assertEqual(router.surface.active,mode)
                drop=QDropEvent(QPointF(point),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier)
                APP.sendEvent(router.surface,drop);self.assertTrue(drop.isAccepted())
                self.assertEqual(calls[-1],(mode,paths));until(lambda:not router.surface.isVisible())
            enter=QDragEnterEvent(button.rect().center(),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier)
            APP.sendEvent(button,enter);APP.sendEvent(button,QDragLeaveEvent());until(lambda:not router.surface.isVisible())
            self.assertEqual(len(calls),2)
            text=QMimeData();text.setText('ordinary shelf content')
            enter=QDragEnterEvent(button.rect().center(),Qt.CopyAction,text,Qt.LeftButton,Qt.NoModifier)
            self.assertFalse(router.eventFilter(button,enter));self.assertFalse(router.surface.isVisible())
        finally:root.hide();root.deleteLater();flush()

    def test_cached_panels_share_pending_files_but_keep_inputs_and_services_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);path=base/'original.bin';path.write_bytes(b'original')
            lan=FakeLan();wan=WanTransferService(base/'wan',enabled=False)
            dialog=LanDialog(lan,SettingsStore(base/'profile'));dialog.show();flush()
            try:
                native=dialog.winId();geometry=dialog.geometry();first=dialog.panel
                first.queue_paths([str(path)]);second=dialog.set_service(wan)
                second.peer_code_input.setText('1234')
                self.assertEqual(second._queued_paths,[str(path)])
                self.assertIs(dialog.set_service(lan),first);self.assertIs(first.service,lan)
                self.assertEqual(first._queued_paths,[str(path)])
                self.assertIs(dialog.set_service(wan),second);self.assertEqual(second.peer_code_input.text(),'1234')
                self.assertEqual(dialog.winId(),native);self.assertEqual(dialog.geometry(),geometry)
                self.assertTrue(dialog.mode_buttons['wan'].isChecked());self.assertFalse(dialog.mode_buttons['lan'].isChecked())
            finally:dialog.hide();dialog.deleteLater();wan.close();wan.deleteLater();flush()


if __name__=='__main__':unittest.main()
