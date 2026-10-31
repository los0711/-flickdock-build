"""Owned-process shutdown boundaries; never terminate a user's application."""
import os,sys,tempfile,time,unittest,subprocess
os.environ['QT_QPA_PLATFORM']='offscreen'
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import mac_updates
import webrtc_stream
from flickdock import ShelfManager
from test_html_native_workspace import APP,flush
from test_html_native_lan import FakeLan
from app_settings import SettingsStore
from lan_panel import LanDialog

class UpdateExitTests(unittest.TestCase):
    def test_sync_shutdown_cancels_work_before_persist_without_scanning_files(self):
        import threading
        from shelf_sync import ShelfSync
        cancel=threading.Event();events=[]
        service=SimpleNamespace(account_changed=SimpleNamespace(disconnect=lambda slot:events.append('disconnect')))
        item=SimpleNamespace(closed=False,epoch=0,small_lane=SimpleNamespace(reset=lambda:events.append('lane')),
            service=service,select_account=lambda:None,requests={'request'},api=SimpleNamespace(cancel=lambda key:events.append(key)),
            jobs=[SimpleNamespace(cancel=cancel)],pool=SimpleNamespace(clear=lambda:events.append('clear')),
            local_changed=Mock(side_effect=AssertionError('shutdown must not scan files')))
        for name in ('timer','debounce','remote_poll','_cleanup_retry'):
            setattr(item,name,SimpleNamespace(stop=lambda:events.append('stop')))
        def persist():
            self.assertTrue(cancel.is_set());self.assertTrue(item.closed);self.assertIn('clear',events)
            events.append('persist')
        item._persist=persist
        ShelfSync.close(item);ShelfSync.close(item)
        item.local_changed.assert_not_called();self.assertEqual(events.count('persist'),1)
        self.assertEqual(item.epoch,1)

    def test_cancelled_queued_disk_job_does_no_file_io(self):
        from sync_files import DiskWork
        operation=Mock();results=[];job=DiskWork(operation)
        job.signals.done.connect(lambda result,error:results.append((result,error)))
        job.cancel.set();job.run()
        operation.assert_not_called();self.assertEqual(len(results),1)
        self.assertIsInstance(results[0][1],InterruptedError)

    def test_one_service_error_cannot_prevent_final_application_exit(self):
        events=[]
        def close(name,fail=False):
            def action():
                events.append(name)
                if fail:raise RuntimeError('fixture')
            return SimpleNamespace(close=action)
        manager=SimpleNamespace(_quitting=False,lan=close('lan',True),wan=close('wan'),clipboard_sync=close('clipboard'),
            lan_dialog=None,wan_dialog=None,settings_dialog=None,outgoing_drag=False,shelves=[],
            save=lambda:events.append('save'),tray=SimpleNamespace(hide=lambda:events.append('tray')),
            edge=SimpleNamespace(hide=lambda:events.append('edge')),dock_preview=SimpleNamespace(hide=lambda:events.append('preview')),
            app=SimpleNamespace(quit=lambda:events.append('quit')))
        ShelfManager.quit(manager);ShelfManager.quit(manager)
        self.assertEqual(events[-1],'quit');self.assertEqual(events.count('quit'),1)
        self.assertIn('wan',events);self.assertIn(('lan','RuntimeError'),manager._shutdown_errors)

    def test_mac_timeout_retry_checks_actual_exit_before_returning(self):
        alive=[True];events=[];prepared=SimpleNamespace(owner_pid=123)
        mac_updates._wait_for_owner_exit(prepared,0,lambda pid:alive[0],retry=lambda:events.append('confirm') or True,
            request_exit=lambda receipt:(events.append(receipt.owner_pid),alive.__setitem__(0,False)))
        self.assertEqual(events,['confirm',123]);self.assertFalse(alive[0])

    def test_mac_cancel_never_requests_termination(self):
        terminate=Mock()
        with self.assertRaises(TimeoutError):mac_updates._wait_for_owner_exit(SimpleNamespace(owner_pid=123),0,lambda pid:True,retry=lambda:False,request_exit=terminate)
        terminate.assert_not_called()

    def test_mac_identity_mismatch_cannot_terminate_another_process(self):
        application=Mock();application.executableURL.return_value.path.return_value='/Other.app/Contents/MacOS/FlickDock'
        kit=SimpleNamespace(NSRunningApplication=SimpleNamespace(runningApplicationWithProcessIdentifier_=lambda pid:application))
        prepared=SimpleNamespace(owner_pid=123,bundle_id=mac_updates.BUNDLE_ID,installed_app=Path('/Target.app'))
        with patch.dict(sys.modules,AppKit=kit),self.assertRaises(ValueError):mac_updates._request_owner_exit(prepared)
        application.terminate.assert_not_called();application.forceTerminate.assert_not_called()

    def test_final_barrier_reaps_only_children_created_by_this_module(self):
        # A deliberately unresponsive fixture child owns no FlickDock/user data.
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
        stream=SimpleNamespace(_process=child,shutdown=Mock())
        class Owned:
            _process=child
            shutdown=stream.shutdown
        owned=Owned()
        try:
            with patch.object(webrtc_stream,'_owned_streams',{owned}):
                self.assertTrue(webrtc_stream.close_owned_streams(timeout=.02))
            stream.shutdown.assert_called_once();self.assertIsNotNone(child.poll())
        finally:
            if child.poll() is None:child.kill()
            child.wait(timeout=3)

    def test_dropped_transfer_temporarily_raises_without_activation_and_releases_on_hide(self):
        with tempfile.TemporaryDirectory() as directory:
            d=LanDialog(FakeLan(),SettingsStore(Path(directory)));d.show();flush()
            try:
                with patch('platform_native.raise_shelf_for_reveal',return_value=True) as raise_native,patch('platform_native.lower_shelf_after_reveal',return_value=True) as lower_native,patch.object(d,'activateWindow') as activate:
                    d.present_dropped_files();flush();self.assertGreaterEqual(raise_native.call_count,1);activate.assert_not_called()
                    d.hide();flush();lower_native.assert_called_with(d);self.assertFalse(d._drop_order_raised)
            finally:d.hide();d.deleteLater();flush()

if __name__=='__main__':unittest.main()
