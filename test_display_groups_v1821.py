"""Full per-display drafts and pointer targets, without native display changes."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import tempfile,unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock
from PySide6.QtCore import Qt,QPoint,QRect
from PySide6.QtTest import QTest
from test_html_native_workspace import APP,flush
from app_settings import SettingsStore,SettingsDialog
from display_triggers import DisplayTriggerEditor,display_id,display_values,validate_profiles


class Screen:
    def __init__(self,name,serial,area):self._name=name;self._serial=serial;self.area=area
    def name(self):return self._name
    def manufacturer(self):return 'QA'
    def model(self):return 'Display'
    def serialNumber(self):return self._serial
    def geometry(self):return self.area
    def availableGeometry(self):return self.area.adjusted(0,0,0,-40)


class DisplayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=SettingsStore(Path(self.temp.name))
        self.main=Screen('Screen1','111',QRect(0,0,1920,1080));self.second=Screen('Screen2','222',QRect(-1280,120,1280,1024))
        self.primary=patch('display_triggers.QGuiApplication.primaryScreen',return_value=self.main);self.primary.start()
        self.screens=patch('display_triggers.QGuiApplication.screens',return_value=[self.main,self.second]);self.screens.start()
        self.editor=DisplayTriggerEditor(self.store);self.editor.resize(720,620);self.editor.show();flush()
    def tearDown(self):
        self.editor.close();self.editor.deleteLater();flush();self.screens.stop();self.primary.stop();self.temp.cleanup()

    def test_header_blank_toggles_expansion_but_only_track_changes_enabled(self):
        first=self.editor.rows[display_id(self.main)];row=self.editor.rows[display_id(self.second)]
        self.assertTrue(first.expanded);self.assertFalse(row.expanded);self.assertFalse(row.toggle.isChecked())
        QTest.mouseClick(row.header,Qt.LeftButton,pos=QPoint(80,16));flush()
        self.assertTrue(row.expanded);self.assertFalse(row.toggle.isChecked());self.assertFalse(self.editor.profiles())
        row.toggle.resize(44,32)
        QTest.mouseClick(row.toggle,Qt.LeftButton,pos=QPoint(20,16));flush()
        self.assertTrue(row.toggle.isChecked());self.assertTrue(row.expanded)
        QTest.mouseClick(row.header,Qt.LeftButton,pos=QPoint(100,16));flush()
        self.assertFalse(row.expanded);self.assertTrue(row.toggle.isChecked())
        self.assertFalse(self.store.display_triggers)

    def test_complete_independent_profiles_roundtrip_hotplug_and_cancel(self):
        row=self.editor.rows[display_id(self.second)];row.toggle.setChecked(True)
        row.edge.setCurrentIndex(row.edge.findData('bottom'))
        for key,value in dict(position=73,length=246,thickness=12,opacity=61).items():row.controls[key].setValue(value)
        row.color='#12ab34';row.changed.emit()
        draft=self.editor.profiles();values=display_values(self.store,self.second,dict(display_triggers=draft))
        self.assertEqual([values[k] for k in ('enabled','dock_edge','dock_position','dock_length','dock_thickness','dock_opacity','dock_color')],[True,'bottom',73,246,12,61,'#12ab34'])
        self.assertEqual(display_values(self.store,self.main)['dock_color'],self.store.dock_color)
        self.assertFalse(self.store.display_triggers)
        self.screens.return_value=[self.main];self.editor.refresh();flush()
        self.screens.return_value=[self.second,self.main];self.editor.refresh();flush()
        self.assertEqual(self.editor.rows[display_id(self.second)].profile(),draft[display_id(self.second)])
        self.store.update(display_triggers=draft);loaded=SettingsStore(Path(self.temp.name))
        self.assertEqual(loaded.display_triggers,draft)
        row=self.editor.rows[display_id(self.second)];row.controls['length'].setValue(400)
        self.editor.load_values();self.assertEqual(row.controls['length'].value(),246)
        self.assertTrue(self.editor.rows[display_id(self.main)].expanded);self.assertFalse(row.expanded)
        for field,value in (('length',601),('thickness',True),('opacity',-1),('color','red')):
            with self.subTest(field=field),self.assertRaises(ValueError):validate_profiles({display_id(self.second):dict(draft[display_id(self.second)],**{field:value})})

    def test_settings_save_cancel_and_toggle_blank_area(self):
        view=SettingsDialog(self.store);view.show();flush()
        try:
            control=view.keep_empty_handle_checkbox;before=control.isChecked();holder=control.parentWidget()
            QTest.mouseClick(holder,Qt.LeftButton,pos=QPoint(10,holder.height()//2));flush();self.assertEqual(control.isChecked(),before)
            QTest.mouseClick(control,Qt.LeftButton,pos=QPoint(20,16));flush();self.assertNotEqual(control.isChecked(),before)
            row=view.display_trigger_editor.rows[display_id(self.second)];row.toggle.setChecked(True);row.controls['length'].setValue(256)
            self.assertFalse(self.store.display_triggers);view.cancel_button.click();flush();view.load_values();view.show();flush()
            row=view.display_trigger_editor.rows[display_id(self.second)];self.assertFalse(row.toggle.isChecked())
            row.toggle.setChecked(True);row.controls['length'].setValue(256);view.save();flush()
            self.assertEqual(SettingsStore(Path(self.temp.name)).display_triggers[display_id(self.second)]['length'],256)
        finally:view.hide();view.deleteLater();flush()

    def test_interrupted_expansion_fits_sections_and_wrapped_text_without_tail(self):
        view=SettingsDialog(self.store);view.show();view.show_settings_category('displays');flush()
        try:
            rows=list(view.display_trigger_editor.rows.values())
            with ExitStack() as overrides:
                for row in rows:overrides.enter_context(patch.object(row,'_motion_enabled',return_value=True))
                for width,height in ((892,632),(1100,760),(700,500)):
                    view.resize(width,height);flush()
                    rows[0].set_expanded(False);rows[1].set_expanded(True);QTest.qWait(30)
                    rows[0].set_expanded(True);rows[1].set_expanded(False);QTest.qWait(30)
                    rows[1].set_expanded(True);QTest.qWait(230)
                    for _ in range(6):flush()
                    for row in rows:
                        self.assertTrue(row.expanded);self.assertEqual(row.animation.state(),row.animation.State.Stopped)
                        for control in (*row.controls.values(),*row.sliders.values(),row.edge,row.color_button):
                            ancestor=row.details
                            while ancestor is not view.body:
                                self.assertTrue(ancestor.rect().contains(control.mapTo(ancestor,control.rect().bottomRight())))
                                ancestor=ancestor.parentWidget()
                    sections=[view.body_layout.itemAt(i).widget() for i in range(view.body_layout.count()) if view.body_layout.itemAt(i).widget().isVisible()]
                    last=sections[-1]
                    self.assertLessEqual(view.body.height(),max(view.scroll.viewport().height(),last.geometry().bottom()+32))
                    for first,second in zip(sections,sections[1:]):self.assertLess(first.geometry().bottom(),second.geometry().top())
                    view.show_settings_category('sync');flush();label=view.retention_description
                    self.assertTrue(label.isVisible());self.assertGreaterEqual(label.height(),label.heightForWidth(label.width()))
                    view.show_settings_category('displays');flush()
                for row in rows:row.set_expanded(False,animate=False)
                for _ in range(6):flush()
                self.assertLessEqual(view.body.height(),max(view.scroll.viewport().height(),last.geometry().bottom()+32))
        finally:view.hide();view.deleteLater();flush()

    def test_all_enabled_draft_handles_receive_geometry_and_are_shown(self):
        from flickdock import ShelfManager,DockHandle
        from dock_geometry import trigger_rect
        row=self.editor.rows[display_id(self.second)];row.toggle.setChecked(True);row.edge.setCurrentIndex(row.edge.findData('left'));row.controls['position'].setValue(73)
        manager=SimpleNamespace(settings=self.store,_quitting=False,settings_open=lambda:True,_settings_prompt_active=lambda:False,dock_preview=SimpleNamespace(hide=Mock()),settings_edge_hint=SimpleNamespace(hide=Mock()))
        manager.edges={};manager._dock_preview_values=None
        for screen in (self.main,self.second):
            edge=SimpleNamespace(display=screen,visible=False)
            edge.values=lambda edge=edge:display_values(manager.settings,edge.display,manager._dock_preview_values)
            edge.position_at_edge=lambda edge=edge:setattr(edge,'rect',trigger_rect(edge.display.availableGeometry(),edge.values()['dock_edge'],edge.values()['dock_position'],edge.values()['dock_length'],edge.values()['dock_thickness']))
            edge.hide=lambda edge=edge:setattr(edge,'visible',False)
            edge.show_handle=lambda edge=edge:setattr(edge,'visible',edge.values()['enabled'])
            manager.edges[display_id(screen)]=edge
        manager.edge=next(iter(manager.edges.values()))
        manager._position_edges=lambda:ShelfManager._position_edges(manager);manager.update_handles=lambda:ShelfManager.update_handles(manager)
        ShelfManager.preview_dock(manager,dict(display_triggers=self.editor.profiles()))
        for edge in manager.edges.values():self.assertTrue(edge.visible)
        self.assertEqual(manager.edges[display_id(self.second)].rect.left(),-1280)
        ShelfManager.preview_dock(manager,None);self.assertFalse(manager.edges[display_id(self.second)].visible)


if __name__=='__main__':unittest.main()
