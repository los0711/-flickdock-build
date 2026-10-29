"""The bridge can exit before Python has consumed its final traffic frame."""
import io,json,os,struct,tempfile,threading,time,unittest
os.environ['QT_QPA_PLATFORM']='offscreen'
from pathlib import Path
from unittest.mock import patch
from webrtc_stream import WebRTCStream

def frame(value):
    data=b'J'+json.dumps(value).encode();return struct.pack('!I',len(data))+data

class CloseTests(unittest.TestCase):
    def test_cancel_before_socket_assignment_waits_for_stats_without_blocking_gui(self):
        from PySide6.QtWidgets import QApplication
        from wan_transfer import WanTransferService
        from lan_transfer import _Transfer
        app=QApplication.instance() or QApplication([])
        entered=threading.Event();release=threading.Event();writer_failed=threading.Event();notified=threading.Event()
        class BrokenWriter(io.BytesIO):
            def write(self,_):writer_failed.set();raise BrokenPipeError()
        class Process:
            def __init__(self):
                self.stdin=BrokenWriter();self.stdout=io.BytesIO(frame({'type':'barrier'})+frame({'type':'stats','sent':65536,'received':128}));self.stopped=False
            def poll(self):return 0 if self.stopped else None
            def wait(self,timeout=None):self.stopped=True;return 0
            def terminate(self):self.stopped=True
            def kill(self):self.stopped=True
        def signal(value):
            if value['type']=='barrier':entered.set();release.wait(2)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'p2p').mkdir();node=root/'node';node.touch()
            service=WanTransferService(root/'wan',enabled=False);service._send_signal=lambda _:True
            try:
                with patch('webrtc_stream.subprocess.Popen',return_value=Process()):
                    stream=WebRTCStream(signal,node=node,root=root);stream.route='relay'
                    self.assertTrue(entered.wait(1));stream.control({'type':'stats'});self.assertTrue(writer_failed.wait(1))
                    deadline=time.monotonic()+1
                    while not stream.closed and time.monotonic()<deadline:time.sleep(.001)
                    self.assertTrue(stream.closed)
                    identifier='a'*32;transfer=_Transfer(identifier,'send','Mac',active=True)
                    self.assertIsNone(transfer.socket)
                    service._transfers[identifier]=transfer
                    service._pending[identifier]=dict(peer=dict(name='Mac'),role='send',ready=threading.Event(),bridge=stream,route='relay')
                    events=[];service.progress.connect(events.append)
                    start=time.monotonic();service.cancel(identifier);self.assertLess(time.monotonic()-start,.1)
                    def finish():service._notify(transfer,'cancelled');notified.set()
                    worker=threading.Thread(target=finish);worker.start()
                    self.assertFalse(notified.wait(.05))
                    start=time.monotonic();app.processEvents();self.assertLess(time.monotonic()-start,.1)
                    release.set();worker.join(2);self.assertFalse(worker.is_alive())
                    start=time.monotonic();app.processEvents();self.assertLess(time.monotonic()-start,.1)
                    self.assertEqual(events[-1]['relay_bytes'],65664)
                    self.assertNotIn(identifier,service._pending)
            finally:release.set();service.close();app.processEvents()
    def test_graceful_close_keeps_terminal_stats_queued_behind_an_earlier_event(self):
        self.exercise()
    def test_broken_writer_does_not_discard_buffered_terminal_stats(self):
        self.exercise(broken=True)
    def test_abort_drains_terminal_stats_and_shutdown_returns_immediately(self):
        self.exercise(abort=True)
    def exercise(self,broken=False,abort=False):
        entered=threading.Event();release=threading.Event()
        class Process:
            def __init__(self):
                self.stdin=io.BytesIO();self.stdout=io.BytesIO(frame({'type':'barrier'})+frame({'type':'stats','sent':65536,'received':128,'delivered':65536}));self.stopped=False
            def poll(self):return 0 if self.stopped else None
            def wait(self,timeout=None):self.stopped=True;threading.Timer(.02,release.set).start();return 0
            def terminate(self):self.stopped=True
            def kill(self):self.stopped=True
        class BrokenWriter(io.BytesIO):
            def write(self,_):raise BrokenPipeError()
        def signal(value):
            if value['type']=='barrier':entered.set();release.wait(2)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'p2p').mkdir();node=root/'node';node.touch()
            process=Process()
            if broken:process.stdin=BrokenWriter()
            with patch('webrtc_stream.subprocess.Popen',return_value=process):
                stream=WebRTCStream(signal,node=node,root=root)
                self.assertTrue(entered.wait(1))
                if abort:
                    import time
                    start=time.monotonic();stream.shutdown();self.assertLess(time.monotonic()-start,.1)
                    self.assertTrue(stream._reader_done.wait(1));stream.close(abort=True)
                else:stream.close()
                self.assertEqual(stream.traffic(),dict(sent=65536,received=128))
                self.assertTrue(stream._reader_done.is_set())

if __name__=='__main__':unittest.main()
