"""Regional policy, live editor and asynchronous real-art ownership tests."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from copy import deepcopy
import json
from pathlib import Path
import tempfile,time,unittest
from unittest.mock import patch
from PySide6.QtCore import QCoreApplication,QEvent,QPoint,QRect,QUrl,Qt
from PySide6.QtGui import QImage,QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from app_settings import SettingsStore,SettingsDialog
from edge_absorb import EdgeAbsorb
from edge_file_drag import EdgeFileDrag
from edge_native_icons import NativeIconCache
from magnetic_editor import MagneticEditor
from magnetic_settings import defaults,default_edges,for_display,validate,region_rect
from display_triggers import display_id

APP=QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)

def flush():APP.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
def until(check):
    deadline=time.monotonic()+3
    while not check() and time.monotonic()<deadline:QTest.qWait(5);flush()
    assert check(),'asynchronous operation did not settle'

class Manager:
    def __init__(self,store):self.settings=store

class MagneticPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='flickdock-magnetic-settings-');self.store=SettingsStore(Path(self.temp.name))
        self.manager=Manager(self.store);self.visual=EdgeAbsorb(self.manager);self.area=APP.primaryScreen().geometry()
    def tearDown(self):self.visual.close();flush();self.temp.cleanup()
    def point(self,edge,depth=8):
        a=self.area
        return dict(left=QPoint(a.left()+depth,a.center().y()),right=QPoint(a.right()-depth,a.center().y()),
                    top=QPoint(a.center().x(),a.top()+depth),bottom=QPoint(a.center().x(),a.bottom()-depth))[edge]
    def test_defaults_left_right_half_width_and_half_length(self):
        value=self.store.magnetic_edges;rows=for_display(value,display_id(APP.primaryScreen()))
        self.assertEqual({e for e in rows if rows[e]['enabled']},{'left','right'})
        self.assertEqual(rows['left']['width'],80);self.assertEqual(rows['left']['length'],50)
        self.assertIsNotNone(self.visual.target_at(self.point('right')))
        self.assertIsNone(self.visual.target_at(self.point('top')))
        self.assertIsNone(self.visual.target_at(QPoint(self.area.right()-8,self.area.top()+5)))
        self.assertIsNone(self.visual.target_at(self.point('right',80)))
        self.assertEqual((self.visual.config.glow_width_px,self.visual.config.glow_height_px),(120,300))
    def test_distinct_edge_rectangles_clip_on_negative_origin_and_at_ends(self):
        area=QRect(-2400,-300,2400,1400)
        for edge in default_edges():
            for position in (0,50,100):
                rect=region_rect(area,edge,dict(width=80,length=50,position=position))
                self.assertTrue(area.contains(rect))
                self.assertEqual(rect.width() if edge in ('left','right') else rect.height(),80)
    def test_roundtrip_preserves_other_fields_and_no_mutable_alias(self):
        self.store.update(theme='dark');value=self.store.magnetic_edges
        key=display_id(APP.primaryScreen());value['displays'][key]=default_edges()
        value['displays'][key]['top']=dict(enabled=True,width=121,length=33,position=20)
        self.assertNotIn(key,self.store.magnetic_edges['displays'])
        self.store.update(magnetic_edges=value)
        reloaded=SettingsStore(Path(self.temp.name))
        self.assertEqual(reloaded.magnetic_edges,value);self.assertEqual(reloaded.theme,'dark')
        value['displays'][key]['left']['width']=200
        self.assertEqual(self.store.magnetic_edges['displays'][key]['left']['width'],80)
    def test_invalid_update_is_rejected_before_disk_commit(self):
        self.store.update(theme='dark');original=self.store.path.read_bytes()
        for key,value in (('width',0),('length',101),('position',-1),('enabled',1)):
            rows=default_edges();rows['left'][key]=value
            with self.assertRaises(ValueError):self.store.update(magnetic_edges=dict(enabled=True,displays={'0'*24:rows}))
            self.assertEqual(self.store.path.read_bytes(),original)
    def test_enabled_edges_own_only_their_configured_native_bands(self):
        gate=EdgeFileDrag(self.visual,lambda mime,pos:True)
        try:
            point=self.point('right');gate.update_pointer(self.area.center(),False);gate.update_pointer(point,True)
            visible={edge:window.geometry() for edge,window in gate._windows.items() if window.isVisible()}
            self.assertEqual(set(visible),{'left','right'})
            self.assertEqual(visible['right'].height(),round(self.area.height()*.5))
            self.assertEqual(visible['right'].width(),80)
        finally:gate.close()
    def test_global_off_and_independent_top_edge_take_effect(self):
        value=defaults();key=display_id(APP.primaryScreen());value['displays'][key]=default_edges()
        value['displays'][key]['top']['enabled']=True
        value['displays'][key]['right']['enabled']=False
        self.visual.configure_regions(value)
        self.assertIsNotNone(self.visual.target_at(self.point('top')));self.assertIsNone(self.visual.target_at(self.point('right')))
        value['enabled']=False;self.visual.configure_regions(value)
        self.assertIsNone(self.visual.target_at(self.point('left')))
    def test_editor_toggles_diagram_and_preserves_each_edge(self):
        editor=MagneticEditor(self.store)
        try:
            editor.diagram.buttons['top'].click()
            self.assertTrue(editor.rows()['top']['enabled']);self.assertEqual(editor.selected,'top')
            editor.controls['width'].setValue(120);editor.controls['position'].setValue(23)
            editor.select('left');self.assertEqual(editor.controls['width'].value(),80)
            editor.select('top');self.assertEqual(editor.controls['width'].value(),120)
            self.assertEqual(editor.controls['position'].value(),23)
            editor.toggle.setChecked(False);self.assertFalse(editor.values()['enabled'])
        finally:editor.deleteLater();flush()
    def test_dialog_commits_magnetic_edits_live(self):
        dialog=SettingsDialog(self.store)
        try:
            dialog.load_values();editor=dialog.magnetic_editor
            editor.controls['width'].setValue(96)
            key=editor.key();until(lambda:self.store.magnetic_edges['displays'].get(key,{}).get('left',{}).get('width')==96)
            self.assertEqual(SettingsStore(Path(self.temp.name)).magnetic_edges['displays'][key]['left']['width'],96)
        finally:dialog._preference_writer.close();dialog.deleteLater();flush()

    def test_magnetic_length_can_fill_every_edge(self):
        for edge in default_edges():
            rect=region_rect(self.area,edge,dict(width=80,length=100,position=17))
            self.assertEqual(rect.height() if edge in ('left','right') else rect.width(),self.area.height() if edge in ('left','right') else self.area.width())

    def test_original_trigger_has_monitor_editor_and_full_edge_length(self):
        from display_triggers import DisplayTriggerEditor,display_values
        from dock_geometry import trigger_rect
        editor=DisplayTriggerEditor(self.store)
        try:
            screen=APP.primaryScreen();key=display_id(screen);row=editor.rows[key]
            row.length_percent.setValue(100)
            self.assertEqual(editor.profiles()[key]['length_percent'],100)
            self.store.update(display_triggers=editor.profiles())
            values=display_values(self.store,screen)
            rect=trigger_rect(screen.geometry(),values['dock_edge'],values['dock_position'],values['dock_length'],values['dock_thickness'])
            self.assertEqual(rect.height(),screen.geometry().height())
            row.diagram.buttons['top'].click();self.assertEqual(row.edge.currentData(),'top')
            values=display_values(self.store,screen,dict(display_triggers=editor.profiles()))
            rect=trigger_rect(screen.geometry(),values['dock_edge'],values['dock_position'],values['dock_length'],values['dock_thickness'])
            self.assertEqual(rect.width(),screen.geometry().width())
            self.assertTrue(values['_full_screen_edge'])
        finally:editor.deleteLater();flush()

    def test_theme_presets_apply_live_and_select_on_first_click(self):
        dialog=SettingsDialog(self.store)
        try:
            dialog.load_values()
            for color in ('#0066D6','#7C3AED','#000000'):
                dialog.accent_presets[color].click()
                self.assertEqual(dialog._draft_accent,color)
                self.assertTrue(dialog.accent_presets[color].isChecked())
                self.assertEqual(sum(b.isChecked() for b in dialog.accent_presets.values()),1)
                until(lambda:self.store.accent_color==color)
        finally:dialog._preference_writer.close();dialog.deleteLater();flush()

class NativeIconOwnershipTests(unittest.TestCase):
    def test_per_file_images_arrive_queued_and_are_not_unified(self):
        def reader(ref,size,cancel):
            image=QImage(40,50,QImage.Format_ARGB32_Premultiplied);image.fill(QColor('red' if ref.endswith('.exe') else 'blue'));return image
        cache=NativeIconCache(reader=reader)
        try:
            self.assertIsNone(cache.request('synthetic.exe'));self.assertIsNone(cache.request('synthetic.png'))
            until(lambda:cache.image('synthetic.exe') is not None and cache.image('synthetic.png') is not None)
            self.assertNotEqual(cache.image('synthetic.exe'),cache.image('synthetic.png'))
            cache.pool.waitForDone(1000)
        finally:cache.close();cache.deleteLater();flush()
    def test_release_waits_for_real_icon_and_close_invalidates_completion(self):
        def reader(ref,size,cancel):
            cancel.wait(.06);image=QImage(64,64,QImage.Format_ARGB32_Premultiplied);image.fill(QColor('#fa2948'));return image
        cache=NativeIconCache(reader=reader);visual=EdgeAbsorb(icon_cache=cache)
        try:
            area=APP.primaryScreen().geometry();point=QPoint(area.right()-8,area.center().y())
            visual.release(point,urls=[QUrl.fromLocalFile(str(Path('synthetic.exe').resolve()))])
            self.assertIsNone(visual.drop_artwork)
            until(lambda:visual.drop_artwork is not None)
            self.assertEqual(visual.drop_artwork.image.pixelColor(20,20),QColor('#fa2948'))
            visual.release(point,urls=[QUrl.fromLocalFile(str(Path('another.exe').resolve()))]);visual.close()
            cache.pool.waitForDone(1000);flush();self.assertIsNone(visual.drop_artwork)
        finally:visual.close();cache.deleteLater();flush()
    def test_icon_timeout_does_not_leave_a_pending_effect(self):
        cache=NativeIconCache(reader=lambda ref,size,cancel:QImage());visual=EdgeAbsorb(icon_cache=cache)
        try:
            area=APP.primaryScreen().geometry();point=QPoint(area.right()-8,area.center().y())
            visual.release(point,urls=[QUrl.fromLocalFile(str(Path('missing.exe').resolve()))])
            visual._icon_timeout();self.assertIsNone(visual._pending_drop)
            cache.pool.waitForDone(1000);flush()
            self.assertIsNone(visual.drop_artwork)
        finally:visual.close();cache.deleteLater();flush()

if __name__=='__main__':unittest.main(verbosity=2)
