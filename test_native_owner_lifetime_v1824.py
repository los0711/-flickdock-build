"""Late pipe callbacks and display shutdown; isolated Qt, no desktop/network."""
import json,os,subprocess,sys,tempfile,threading,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock
os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication
from shiboken6 import delete
from wan_transfer import WanTransferService
from webrtc_stream import WebRTCStream
import flickdock
APP=QApplication.instance() or QApplication([])


class Bridge:
    def __init__(self,callback,**_):self.on_signal=callback;self.opened=threading.Event();self.closed=False;self.route='';self.shutdown_calls=0
    def control(self,value):pass
    def shutdown(self):self.shutdown_calls+=1


class LifetimeTests(unittest.TestCase):
    def service(self,base):
        service=WanTransferService(base,enabled=False)
        state=dict(role='receive',peer=dict(id='peer',name='QA'),ready=threading.Event())
        service._pending['fixture']=state
        with patch('wan_transfer.WebRTCStream',Bridge):service._begin('fixture',state,dict(generation=1,iceServers=[],relayAllowed=False))
        return service,state['bridge']

    def test_pipe_finally_after_actual_qobject_destroy_does_not_emit_to_dead_owner(self):
        with tempfile.TemporaryDirectory() as base:
            service,bridge=self.service(Path(base));service.close();delete(service)
            done=threading.Event();errors=[]
            def eof(_):raise EOFError()
            pipe=SimpleNamespace(_read_pipe=eof,_condition=threading.Condition(),_closed=False,
                _close_sockets=lambda:None,_reader_done=done,_closed_event=lambda:dict(type='closed',exit_code=0),on_signal=bridge.on_signal)
            def reader():
                try:WebRTCStream._reader(pipe)
                except Exception as error:errors.append(type(error).__name__+': '+str(error))
            worker=threading.Thread(target=reader);worker.start();worker.join(2)
            self.assertFalse(worker.is_alive());self.assertTrue(done.is_set());self.assertEqual(errors,[])

    def test_normal_native_delivery_survives_and_close_fences_pending_signal_once(self):
        with tempfile.TemporaryDirectory() as base:
            service,bridge=self.service(Path(base));seen=[];service._native.connect(seen.append)
            bridge.on_signal(dict(type='fixture'));self.assertEqual(len(seen),1)
            service.close();bridge.on_signal(dict(type='fixture'));self.assertEqual(len(seen),1)
            # A callback queued before close must not mutate the live session.
            original=service._pending['fixture'].get('relay_transport')
            service._handle_native(dict(id='fixture',generation=1,event=dict(type='transport',relayTransport='late')))
            self.assertEqual(service._pending['fixture'].get('relay_transport'),original)
            delete(service)

    def test_destroy_without_explicit_close_also_fences_retired_pipe_callback(self):
        with tempfile.TemporaryDirectory() as base:
            service,bridge=self.service(Path(base));delete(service)
            bridge.on_signal(dict(type='closed',exit_code=0))

    def test_native_delivery_can_reenter_close_without_deadlock(self):
        with tempfile.TemporaryDirectory() as base:
            service,bridge=self.service(Path(base));seen=[]
            service._native.connect(lambda value:(seen.append(value),service.close()))
            bridge.on_signal(dict(type='fixture'));bridge.on_signal(dict(type='fixture'))
            self.assertEqual(len(seen),1);self.assertEqual(bridge.shutdown_calls,1)
            delete(service)

    def test_quitting_display_slots_do_not_access_screens_or_edges(self):
        edge=SimpleNamespace(position_at_edge=Mock(side_effect=AssertionError('dead screen accessed')))
        manager=SimpleNamespace(_quitting=True,edges={'old':edge})
        with patch('flickdock.QGuiApplication.screens',side_effect=AssertionError('Qt screens queried after quit')):
            flickdock.ShelfManager._position_edges(manager)
            flickdock.ShelfManager._refresh_displays(manager)
        edge.position_at_edge.assert_not_called()

    def test_live_display_position_reconciles_a_removed_qobject_before_using_it(self):
        display=QObject();delete(display)
        edge=SimpleNamespace(display=display,position_at_edge=Mock(side_effect=AssertionError('stale screen used')))
        manager=SimpleNamespace(_quitting=False,edges={'removed':edge},_refresh_displays=Mock())
        flickdock.ShelfManager._position_edges(manager)
        manager._refresh_displays.assert_called_once();edge.position_at_edge.assert_not_called()

    def test_real_qscreen_destroyed_with_app_never_reenters_position_during_quit(self):
        # Only this disposable subprocess destroys its own offscreen app. Never
        # delete a screen belonging to another test or native desktop.
        script='''import json,tempfile
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication
from shiboken6 import delete,isValid
from app_settings import SettingsStore
from flickdock import DockHandle,ShelfManager
app=QApplication([])
with tempfile.TemporaryDirectory() as base:
 manager=SimpleNamespace(settings=SettingsStore(base),motion=False,_quitting=False,_dock_preview_values=None)
 edge=DockHandle(manager);screen=edge.display;manager.edges={'actual':edge};manager._quitting=True
 delete(app)
 error=None
 try:ShelfManager._position_edges(manager)
 except Exception as value:error=type(value).__name__+': '+str(value)
 print(json.dumps(dict(screen_valid=isValid(screen),error=error)),flush=True)
'''
        result=subprocess.run([sys.executable,'-X','utf8','-c',script],cwd=Path(__file__).parent,
                              capture_output=True,text=True,timeout=20,env=dict(os.environ,QT_QPA_PLATFORM='offscreen'))
        self.assertEqual(result.returncode,0,result.stderr)
        row=json.loads(result.stdout.strip().splitlines()[-1]);self.assertFalse(row['screen_valid']);self.assertIsNone(row['error'],row)


if __name__=='__main__':unittest.main(verbosity=2)
