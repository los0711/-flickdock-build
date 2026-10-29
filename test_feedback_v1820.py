"""Observable native UI, network classification and conservative cleanup."""
import json,os,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QPoint,QRect,Qt,QMimeData
from PySide6.QtGui import QDragEnterEvent,QDropEvent
from test_html_native_workspace import APP,flush,until
import test_html_native_workspace as workspace
from drop_bridge import DropBridge,bridge_rect
from network_environment import NetworkEnvironment,parse_mac_vpn,vpn_status
from runtime_cleanup import cleanup_updates
from mac_updates import consume_update_failure
import flickdock

class FeedbackTests(unittest.TestCase):
    def test_vpn_states_do_not_treat_virtual_adapters_as_vpn(self):
        self.assertEqual(parse_mac_vpn('utun0: flags=UP\nvEthernet VirtualBox'),'unknown')
        self.assertEqual(parse_mac_vpn('* (Connected) VPN "private"'),'connected')
        self.assertEqual(parse_mac_vpn('* (Disconnected) VPN "private"'),'disconnected')
        self.assertEqual(parse_mac_vpn('* (Connecting) VPN "private"'),'unknown')
        def runner(states,failed=False):
            return lambda *args,**kwargs:SimpleNamespace(returncode=0,stdout=json.dumps(dict(states=states,failed=failed)))
        self.assertEqual(vpn_status('win32',runner([])),'unknown')
        self.assertEqual(vpn_status('win32',runner(['Disconnected'])),'disconnected')
        self.assertEqual(vpn_status('win32',runner(['Disconnected'],True)),'unknown')
        self.assertEqual(vpn_status('win32',runner(['Connected'])),'connected')

    def test_network_probe_is_background_and_preserves_unknown_states(self):
        import threading
        entered=threading.Event();release=threading.Event()
        def detect():entered.set();release.wait(2);return dict(vpn='connected',proxy='disabled')
        monitor=NetworkEnvironment(enabled=False,detect=detect)
        try:
            start=time.monotonic();monitor.check();self.assertLess(time.monotonic()-start,.1)
            self.assertTrue(entered.wait(1));self.assertEqual(monitor.state['vpn'],'unknown')
            release.set();until(lambda:monitor.job is None)
            self.assertEqual(monitor.state,dict(vpn='connected',proxy='disabled'))
        finally:release.set();monitor.close();monitor.pool.waitForDone(2500)

    def test_gap_drop_is_supported_on_every_edge_and_keeps_overview(self):
        fixture=workspace.WorkspaceTests();fixture.setUp();shelf=fixture.shelf;manager=fixture.manager
        manager.settings_open=lambda:False;manager.outgoing_drag=False
        strip=QRect(1900,300,8,90)
        manager.edge=SimpleNamespace(activation_region=lambda:strip)
        bridge=DropBridge(manager)
        try:
            surface=QRect(1200,200,428,427)
            for edge,strip_rect in [('right',QRect(1900,300,8,90)),('left',QRect(0,300,8,90)),
                                    ('top',QRect(1400,0,90,8)),('bottom',QRect(1400,1000,90,8))]:
                rect=bridge_rect(strip_rect,surface,edge)
                self.assertGreater(rect.width(),0);self.assertGreater(rect.height(),0)
                self.assertTrue(rect.intersects(surface) or rect.intersects(surface.adjusted(-1,-1,1,1)))
            shelf.expanded=True;shelf.presentation_kind='dock';shelf.visible_state='floating'
            bridge.sync(shelf,dragging=True);self.assertTrue(bridge.isVisible())
            mime=QMimeData();mime.setText('drop gap original text')
            enter=QDragEnterEvent(QPoint(1,1),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier)
            bridge.dragEnterEvent(enter);self.assertTrue(enter.isAccepted())
            drop=QDropEvent(QPoint(1,1),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier)
            bridge.dropEvent(drop);self.assertTrue(drop.isAccepted())
            until(lambda:not shelf.copy_jobs)
            self.assertTrue(shelf.expanded)
            self.assertIn('drop gap original text',shelf.content.texts.values())
        finally:bridge.close();bridge.deleteLater();fixture.tearDown()

    def test_shake_static_held_drag_expires_but_deliberate_movement_rearms(self):
        shelf=SimpleNamespace(visual_geometry=lambda:QRect(100,100,428,427),
            _shake_pointer=QPoint(200,200),_shake_last_interaction=10.,last_activity=100.)
        self.assertFalse(flickdock.ShelfWindow.shake_idle_expired(shelf,QPoint(200,200),12.9,True))
        self.assertTrue(flickdock.ShelfWindow.shake_idle_expired(shelf,QPoint(200,200),13.1,True))
        self.assertFalse(flickdock.ShelfWindow.shake_idle_expired(shelf,QPoint(220,210),13.2,True))
        self.assertEqual(shelf._shake_last_interaction,13.2)
        self.assertTrue(flickdock.ShelfWindow.shake_idle_expired(shelf,QPoint(1000,1000),14.1,False))

    def test_completed_old_update_cleanup_retains_unknown_current_and_unfinished(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();updates=root/'updates';updates.mkdir()
            for version,name in [('1.8.16','setup.exe'),('1.8.17','setup.exe'),('1.8.18','setup.exe'),('1.8.20','setup.exe'),('1.8.21','setup.exe')]:
                folder=updates/version;folder.mkdir();path=folder/name;path.write_bytes(b'X'*4096);os.utime(path,(1,1))
            (updates/'1.8.17'/'setup-ongoing.part').write_bytes(b'partial')
            (updates/'1.8.18'/'.flickdock-update-fixture').mkdir()
            shelf=root/'cloud-shelves';shelf.mkdir();(shelf/'original').write_bytes(b'keep')
            before=sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
            self.assertEqual(cleanup_updates(root,'1.8.20'),4096)
            after=sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
            self.assertEqual(before-after,4096)
            self.assertFalse((updates/'1.8.16').exists())
            for version in ('1.8.17','1.8.18','1.8.20','1.8.21'):self.assertTrue((updates/version/'setup.exe').is_file())
            self.assertEqual((shelf/'original').read_bytes(),b'keep')

    def test_old_install_failure_is_consumed_without_reporting_current_success_as_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'mac-update-failure.json'
            path.write_text(json.dumps(dict(version='1.8.19',recorded_at=time.time(),error='old helper failed')))
            self.assertEqual(consume_update_failure(root,'1.8.20'),'');self.assertFalse(path.exists())

if __name__=='__main__':unittest.main()
