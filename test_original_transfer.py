"""Actual original file streams, current consent boundaries and cloud isolation."""
import ast,json,os,queue,subprocess,sys,tempfile,threading,unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication
from wan_transfer import WanTransferService
from lan_transfer import LanTransferService
APP=QApplication.instance() or QApplication([])
ROOT=Path(__file__).parent

class OriginalTransferTests(unittest.TestCase):
    def test_remote_application_stream_via_real_signaling_and_native_datachannel(self):
        node=ROOT/'p2p/runtime'/('node.exe' if sys.platform=='win32' else 'node')
        if not node.is_file():self.fail('Prepare p2p_assets.py before verification')
        worker=subprocess.Popen([str(node),str(ROOT/'cloud/test/qa-server.mjs')],cwd=ROOT,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        ready=queue.Queue()
        output_tail=deque(maxlen=40)
        def read_origin():
            announced=False
            for line in worker.stdout:
                output_tail.append(line.decode('utf8','replace'))
                if not announced and line.startswith(b'http://127.0.0.1:'):
                    announced=True
                    ready.put(line.decode().strip())
            if not announced:ready.put(None)
        reader=threading.Thread(target=read_origin,daemon=True)
        reader.start()
        try:
            try:origin=ready.get(timeout=100)
            except queue.Empty:self.fail('Isolated Worker startup timed out:\n'+''.join(tuple(output_tail)))
            self.assertIsNotNone(origin,'Isolated Worker exited before startup:\n'+''.join(tuple(output_tail)))
            with tempfile.TemporaryDirectory(prefix='flickdock-original-pipeline-') as directory:
                output=Path(directory)/'verified.json'
                command=[sys.executable,'-c',"import verify_wan,sys;raise SystemExit(verify_wan.run(sys.argv,lambda _:None))",'--verify-output',str(output)]
                result=subprocess.run(command,cwd=ROOT,env=dict(os.environ,PYTHONUTF8='1',QT_QPA_PLATFORM='offscreen',FLICKDOCK_QA_WAN_ORIGIN=origin),capture_output=True,text=True,encoding='utf8',timeout=210)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr+(output.read_text(encoding='utf8') if output.exists() else ''))
                report=json.loads(output.read_bytes());self.assertTrue(report['ok'])
                self.assertEqual(report['verified_bytes'],32*1024**2)
                self.assertEqual(report['route'],'direct');self.assertTrue(report['receiver_consent'])
                self.assertTrue(report['empty_folders_preserved']);self.assertTrue(report['code_pairing'])
        finally:
            worker.stdin.close()
            try:worker.wait(timeout=12)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=5)
            reader.join(timeout=5)
            worker.stdout.close()

    def test_incompatible_kernel_is_rejected_before_reading_source(self):
        with tempfile.TemporaryDirectory() as directory:
            service=WanTransferService(directory,enabled=False);service._connected=True
            service._peers={'a'*64:dict(id='a'*64,name='Old kernel',online=True,features=['syncthing-v2'])}
            try:
                with patch.object(service,'_thread') as worker:
                    with self.assertRaisesRegex(ValueError,'两端更新'):service.send('a'*64,[Path(directory)/'missing'])
                    worker.assert_not_called()
                self.assertFalse(service._transfers)
            finally:service.close()

    def test_application_uses_original_direct_lan_and_no_retired_sidecar(self):
        self.assertEqual(WanTransferService.__bases__,(LanTransferService,))
        source=(ROOT/'transfer_runtime.py').read_text(encoding='utf8')
        self.assertIn('LanTransferService(app,',source)
        for name in ('syncthing_lan.py','syncthing_engine.py','syncthing_jobs.py','manual_payload.py','manual_cloud.py'):
            self.assertFalse((ROOT/name).exists(),name)
        for name in ('clipboard_sync.py','shelf_sync.py','sync_scheduler.py'):
            imports=[n.module for n in ast.walk(ast.parse((ROOT/name).read_text(encoding='utf8'))) if isinstance(n,ast.ImportFrom)]
            self.assertFalse({'syncthing_engine','webrtc_stream','wan_transfer','lan_transfer'}&set(imports),name)

if __name__=='__main__':unittest.main()
