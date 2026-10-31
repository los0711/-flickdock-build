"""Production Qt layout/motion, disposable files and mocked file-manager launch."""
import os,tempfile,unittest
os.environ['QT_QPA_PLATFORM']='offscreen'
from pathlib import Path
from unittest.mock import patch,Mock
from PySide6.QtCore import QPoint,QMimeData,QUrl,Qt
from PySide6.QtGui import QDragEnterEvent,QDropEvent
from PySide6.QtWidgets import QWidget,QPushButton
from PySide6.QtTest import QTest
import test_html_native_workspace as ws
from test_html_native_lan import FakeLan
from app_settings import SettingsStore,SettingsDialog
from lan_panel import LanDialog
from transfer_entry import TransferEntryRouter

def insets(case,frame,buttons):
    ws.flush();frame.layout().activate()
    for button in buttons:
        case.assertEqual(button.y(),3)
        case.assertEqual(frame.height()-button.geometry().bottom()-1,3)
        case.assertTrue(frame.rect().contains(button.geometry()))
    case.assertEqual(buttons[0].x(),3)
    case.assertEqual(frame.width()-buttons[-1].geometry().right()-1,3)

class OverviewPolishTests(unittest.TestCase):
    def setUp(self):
        self.fixture=ws.WorkspaceTests('runTest');self.fixture.setUp();self.view=self.fixture.overview()
    def tearDown(self):self.fixture.tearDown()

    def test_even_segment_gutters_and_compact_account(self):
        p=self.view;insets(self,p.view_segment,[p.view_list,p.view_grid])
        insets(self,p.sort_segment,[p.selection_hint,*p.sort_buttons.values()])
        self.assertEqual(p.select_all_button.height(),p.sort_segment.height())
        self.assertEqual(p.account_nav.height(),42)

    def test_deleting_across_scrollbar_and_partial_row_boundaries_preserves_widths(self):
        paths=[]
        for index in range(12):
            path=self.fixture.base/f'grid{index}.txt';path.write_text('original');paths.append(str(path))
        self.fixture.shelf.add_paths('permanent',paths);p=self.view;p.change_view_mode('icons');p.resume_content()
        ws.until(lambda:p._build_cursor==len(p.visible_refs()))
        expected=next(iter(p._item_widgets.values())).width();viewport=p.scroll.viewport().width()
        while len(p.visible_refs())>1:
            p.remove_path(p.visible_refs()[0]);p.resume_content();ws.until(lambda:p._build_cursor==len(p.visible_refs()))
            self.assertEqual(p.scroll.viewport().width(),viewport)
            self.assertTrue(all(widget.width()==expected for widget in p._item_widgets.values()))
        self.assertTrue(all(Path(path).exists() for path in paths))

    def test_segment_retargets_then_stops_when_hidden(self):
        p=self.view
        with patch('ui_transitions.motion_allowed',return_value=True):
            p.cycle_sort('time');ws.flush();QTest.qWait(20);p.cycle_sort('size');ws.flush()
            ws.until(lambda:p.sort_indicator.animation.state()==p.sort_indicator.animation.State.Stopped)
            self.assertEqual(p.sort_indicator._rect.toRect(),p.sort_buttons['size'].geometry())
            p.cycle_sort('time');ws.flush();p.hide();ws.flush()
            self.assertEqual(p.sort_indicator.animation.state(),p.sort_indicator.animation.State.Stopped)

class TransferPolishTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.profile=SettingsStore(Path(self.temp.name))
        self.dialog=LanDialog(FakeLan(),self.profile);self.dialog.show();ws.flush();self.panel=self.dialog.panel
    def tearDown(self):self.dialog.hide();self.dialog.deleteLater();ws.flush();self.temp.cleanup()

    def test_drop_band_empty_full_width_then_compact_and_reversible(self):
        p=self.panel;area=p.queue_area
        self.assertEqual(p.drop_zone.width(),area.width()-40);self.assertFalse(p.send_heading_label.isVisible())
        path=Path(self.temp.name)/'file.txt';path.write_text('original')
        with patch('lan_panel.reduced_motion',return_value=False):
            p.queue_paths([str(path)]);ws.flush();QTest.qWait(30)
            self.assertGreater(area.value,0);self.assertLess(area.value,1)
            p.queue_paths([],replace=True);ws.until(lambda:area.value==0.)
            self.assertEqual(p.drop_zone.width(),area.width()-40)
        with patch('lan_panel.reduced_motion',return_value=True):
            p.queue_paths([str(path)]);ws.flush()
            self.assertEqual(area.value,1.)
            self.assertEqual(p.drop_zone.width(),p.send_column.width()-40)
            self.assertTrue(p.send_heading_label.isVisible());self.assertTrue(path.exists())

    def test_mode_segment_status_and_destination_alignment(self):
        d=self.dialog;p=self.panel;insets(self,d.mode_segment,list(d.mode_buttons.values()))
        self.assertIs(p.status_label.parentWidget(),d.mode_bar)
        self.assertTrue(p.status_label.isVisible());self.assertEqual(d.mode_segment.width(),218)
        self.assertLess(d.mode_segment.geometry().right(),p.status_label.geometry().left())
        p.status_label.setText('远程互传已就绪。')
        from native_ui_common import colors
        self.assertIn(colors(self.profile.theme)['green'],p.status_label.styleSheet())
        p.status_label.setText('传输失败')
        self.assertIn(colors(self.profile.theme)['red'],p.status_label.styleSheet())
        self.assertLessEqual(abs(p.receive_path.mapTo(p,QPoint(0,p.receive_path.height())).y()-p.send_button.mapTo(p,QPoint(0,p.send_button.height())).y()),2)

    def test_escape_invalidates_pending_presentation_before_fade_completes(self):
        intents=[];d=self.dialog;d.closing_intent.connect(lambda:intents.append(True))
        QTest.keyClick(d,Qt.Key_Escape)
        self.assertTrue(intents)
        ws.until(lambda:not d.isVisible())

    def test_history_has_no_leading_icon_and_reveals_only_real_record_paths(self):
        p=self.panel;path=Path(self.temp.name)/'original.txt';path.write_text('original')
        p._progress(dict(id='received',name=path.name,peer='Peer',direction='receive',state='complete',total=8,received=8))
        p._completed(dict(id='received',direction='receive',paths=[str(path)]));ws.flush()
        row=p.history_rows[0];self.assertFalse(row.icon.isVisible());self.assertEqual(row.detail.x(),row.contentsRect().left()+14)
        self.assertTrue(row.reveal_button.isEnabled())
        with patch.object(p.file_metadata,'request',return_value={'exists':True}),patch('subprocess.Popen') as launch:
            row.reveal_button.click();launch.assert_called_once_with(['explorer.exe','/select,',str(path)])
        self.assertEqual(path.read_text(),'original')
        with patch.object(p.file_metadata,'request',return_value=None),patch('subprocess.Popen') as launch:
            row.reveal_button.click();p.hide();ws.flush()
            p._history_file_ready(str(path));launch.assert_not_called()
        p.show();ws.flush()
        with patch.object(p.file_metadata,'request',return_value=None),patch('subprocess.Popen') as launch:
            p.history_rows[0].reveal_button.click();p._delete_history_record(0)
            p._history_file_ready(str(path));launch.assert_not_called()

class SharedMotionTests(unittest.TestCase):
    def test_update_button_recomputes_real_colors_without_private_style_wrapper(self):
        with tempfile.TemporaryDirectory() as directory,patch('app_settings._startup_runtime_allowed',return_value=False):
            dialog=SettingsDialog(SettingsStore(Path(directory)));dialog.show();ws.flush()
            try:
                with patch.object(dialog.update_button,'style',side_effect=AssertionError('private wrapper must not be retrieved')):
                    for theme,expected in (('dark','#30d158'),('light','#168545')):
                        dialog.apply_theme(theme);ws.flush();colors={}
                        for state in ('idle','available','latest','ready','error','idle'):
                            offered=state in ('available','ready','error')
                            dialog.set_update_state(state,'1.8.24' if offered else '');ws.flush()
                            rendered=dialog.update_button.grab().toImage();scale=rendered.devicePixelRatio()
                            colors[state]=rendered.pixelColor(round(8*scale),round(dialog.update_button.height()/2*scale)).name()
                            self.assertEqual(dialog.update_button.property('updateAvailable'),offered)
                        self.assertEqual(colors['available'],expected)
                        self.assertNotEqual(colors['idle'],colors['available'])
                        self.assertEqual(colors['latest'],colors['idle'])
            finally:dialog.hide();dialog.deleteLater();ws.flush()

    def test_staged_drop_accepts_immediately_but_presents_after_feedback(self):
        root=QWidget();root.resize(440,420);button=QPushButton('文件互传',root);button.setGeometry(20,360,180,34)
        calls=[]
        def stage(mode,paths):calls.append(('queue',mode,paths));return lambda:calls.append(('present',mode))
        router=TransferEntryRouter(root,button,stage);root.show();ws.flush()
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(Path(tempfile.gettempdir())/'original.txt'))])
        try:
            with patch('transfer_entry.reduced_motion',return_value=False):
                ws.APP.sendEvent(button,QDragEnterEvent(QPoint(20,20),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier))
                self.assertTrue(button.property('transferDropActive'));self.assertEqual(router.surface.height(),138)
                drop=QDropEvent(QPoint(30,25),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier)
                router.eventFilter(router.surface,drop);self.assertTrue(drop.isAccepted())
                self.assertEqual(len(calls),1);self.assertEqual(calls[0][0],'queue')
                ws.until(lambda:len(calls)==2);self.assertEqual(calls[1],('present','lan'))
                self.assertFalse(router.surface.isVisible());self.assertFalse(button.property('transferDropActive'))
        finally:root.hide();root.deleteLater();ws.flush()

if __name__=='__main__':unittest.main()
