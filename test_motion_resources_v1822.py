"""Finite glyph motion and display-only asynchronous filesystem probes."""
import threading,unittest
from unittest.mock import patch
from pathlib import Path
from PySide6.QtCore import QAbstractAnimation,QEvent,QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget
from test_html_native_workspace import APP,flush,until,WorkspaceTests
from ui_transitions import ToolbarEntrance,DwellHint


class MotionTests(unittest.TestCase):
    def test_toolbar_reversal_keeps_hit_geometry_and_hide_stops_clock(self):
        root=QWidget();controls=[QWidget(root) for _ in range(4)]
        for i,control in enumerate(controls):control.setGeometry(i*32,0,32,32)
        root.show();flush();bounds=[control.geometry() for control in controls]
        effect=ToolbarEntrance(root,lambda:controls)
        try:
            with patch('ui_transitions.motion_allowed',return_value=True):
                effect.reveal(reset=True);effect.animation.setCurrentTime(90)
                progress=effect.progress;effect.conceal()
                self.assertAlmostEqual(effect.animation.startValue(),progress)
                effect.animation.setCurrentTime(60);effect.reveal()
                until(lambda:effect.progress==1.)
                self.assertEqual([control.geometry() for control in controls],bounds)
                effect.conceal();root.hide();flush()
                self.assertEqual(effect.animation.state(),QAbstractAnimation.Stopped)
                self.assertTrue(all(control._entry_alpha==1. for control in controls))
            with patch('ui_transitions.motion_allowed',return_value=False):
                effect.reveal(reset=True);self.assertEqual(effect.progress,1.)
                self.assertEqual(effect.animation.state(),QAbstractAnimation.Stopped)
        finally:root.deleteLater();flush()

    def test_dwell_hint_is_once_finite_and_hidden_or_reduced_motion_cancels(self):
        owner=QWidget();owner.show();flush();effect=DwellHint(owner)
        try:
            with patch('ui_transitions.motion_allowed',return_value=True):
                effect.enter();self.assertTrue(effect.wait.isSingleShot())
                effect.wait.stop();effect._start();effect.animation.setCurrentTime(120)
                self.assertGreater(effect.value,0)
                effect.animation.setCurrentTime(240);flush()
                self.assertEqual(effect.value,0.);self.assertFalse(effect.wait.isActive())
                effect.enter();owner.hide();flush()
                self.assertFalse(effect.wait.isActive());self.assertEqual(effect.value,0.)
            owner.show();flush()
            with patch('ui_transitions.motion_allowed',return_value=False):
                effect.enter();self.assertFalse(effect.wait.isActive())
        finally:owner.deleteLater();flush()


class DisplayMetadataTests(unittest.TestCase):
    setUp=WorkspaceTests.setUp
    tearDown=WorkspaceTests.tearDown

    def test_late_missing_candidate_gets_its_own_full_observation_interval(self):
        first,second=self.paths[:2]
        with patch('file_metadata._local_volume',return_value=True):
            Path(first).unlink();self.shelf.file_metadata.refresh(first)
            until(lambda:first in self.shelf._missing_refs);QTest.qWait(400)
            Path(second).unlink();self.shelf.file_metadata.refresh(second)
            until(lambda:second in self.shelf._missing_refs)
            until(lambda:first not in self.shelf.files['permanent'])
            self.assertIn(second,self.shelf.files['permanent'])
            self.assertFalse(self.shelf._missing_refs[second]['confirming'])
            Path(second).write_bytes(b'restored');self.shelf.file_metadata.refresh(second)
            until(lambda:second not in self.shelf._missing_refs);QTest.qWait(550);flush()
            self.assertIn(second,self.shelf.files['permanent'])

    def test_refresh_does_not_stat_on_gui_and_missing_local_file_is_reconciled(self):
        ref=self.paths[0];self.shelf.file_metadata.invalidate(ref)
        original=Path.stat;gui=threading.get_ident();threads=[]
        def probe(path,*args,**kwargs):
            if str(path)==ref:
                threads.append(threading.get_ident())
                if threading.get_ident()==gui:raise AssertionError('GUI performed file stat during refresh')
            return original(path,*args,**kwargs)
        with patch('pathlib.Path.stat',autospec=True,side_effect=probe):
            self.shelf.refresh();until(lambda:self.shelf.file_metadata.request(ref) is not None)
        self.assertTrue(threads);self.assertNotIn(gui,threads)
        Path(ref).unlink();self.shelf.file_metadata.refresh(ref)
        until(lambda:ref not in self.shelf.files['permanent'])
        self.assertTrue(Path(self.paths[1]).exists())

    def test_old_missing_result_does_not_remove_restored_path_or_unknown_volume(self):
        ref=self.paths[0];path=Path(ref);original=Path.stat;entered=threading.Event();release=threading.Event()
        path.unlink();self.shelf.file_metadata.invalidate(ref)
        def missing(candidate,*args,**kwargs):
            try:return original(candidate,*args,**kwargs)
            except FileNotFoundError:
                if str(candidate)==ref:entered.set();release.wait(3)
                raise
        try:
            with patch('pathlib.Path.stat',autospec=True,side_effect=missing),patch('file_metadata._local_volume',return_value=True):
                self.shelf.refresh();until(entered.is_set)
                path.write_bytes(b'restored original');release.set()
                until(lambda:ref in self.shelf._missing_refs)
                until(lambda:ref not in self.shelf._missing_refs)
                self.assertIn(ref,self.shelf.files['permanent']);self.assertTrue(path.exists())
            path.unlink()
            with patch('file_metadata._local_volume',return_value=False):
                self.shelf.file_metadata.refresh(ref)
                until(lambda:self.shelf.file_metadata.request(ref).get('local_volume') is False)
                QTest.qWait(600);flush()
                self.assertIn(ref,self.shelf.files['permanent']);self.assertNotIn(ref,self.shelf._missing_refs)
        finally:release.set()


if __name__=='__main__':unittest.main()
