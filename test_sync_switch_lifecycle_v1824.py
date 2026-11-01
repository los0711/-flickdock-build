"""Switch fences over durable shared jobs, isolated files/account/clipboard."""
import copy,time,unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QTimer
import test_shared_clipboard_files_v1821 as shared
from test_html_native_workspace import APP,until,flush


def settle(seconds=.18):
    end=time.monotonic()+seconds
    while time.monotonic()<end:APP.processEvents();time.sleep(.002)


class SharedSwitchTests(unittest.TestCase):
    def setUp(self):
        self.f=shared.SharedFilesTests();self.f.setUp()
    def tearDown(self):self.f.tearDown()
    def held_entry(self):
        f=self.f;path=f.source();identity=f.copy(path);until(lambda:identity in f.peer.held)
        entry=f.add(path);until(lambda:entry.get('shared_clipboard')==identity)
        return path,identity,entry

    def test_both_switches_off_never_restore_or_retry_clipboard_protocol(self):
        f=self.f;path,identity,entry=self.held_entry();native=f.clip.mimeData();expected=path.read_bytes()
        f.engine.set_directions(False,False);before=len(f.peer.calls)
        f.client.set_enabled(False);settle()
        f.client.file_jobs._retry();settle(.03)
        calls=f.peer.calls[before:]
        self.assertFalse(any('/v1/clipboard' in p for m,p,d in calls),calls)
        self.assertFalse(any(p=='/v1/uploads' for m,p,d in calls),calls)
        self.assertFalse(f.client.file_jobs.retry.isActive())
        self.assertIs(f.clip.mimeData(),native);self.assertEqual(path.read_bytes(),expected)
        self.assertTrue(entry['dirty'])

    def test_clipboard_off_releases_shelf_wait_and_uses_only_shelf_upload(self):
        f=self.f;path,identity,entry=self.held_entry();ordinary=[];original=f.peer.request
        def request(method,url,callback,**options):
            data=options.get('data') or {}
            if method=='POST' and url=='/v1/uploads' and not data.get('reuse_only'):
                ordinary.append(copy.deepcopy(data));key=str(shared.uuid4());f.peer.active[key]=True
                record=dict(id=data['item_id'],version=data['base_version']+1,
                    **{name:data[name] for name in ('kind','name','size','sha256','mime_type')})
                QTimer.singleShot(0,lambda:callback(dict(instant=True,record=record),None));return key
            return original(method,url,callback,**options)
        with patch.object(f.peer,'request',side_effect=request):
            before=len(f.peer.calls);f.client.set_enabled(False)
            until(lambda:bool(ordinary) and not entry['dirty'])
        self.assertNotIn('shared_clipboard',entry)
        self.assertFalse(any('/v1/clipboard' in p for m,p,d in f.peer.calls[before:]))
        self.assertEqual(ordinary[0]['sha256'],shared.hashlib.sha256(path.read_bytes()).hexdigest())

    def test_reenable_does_not_publish_cancelled_journal_as_global_latest(self):
        f=self.f;path,identity,entry=self.held_entry()
        journal=f.client.file_jobs.journal
        f.engine.set_directions(False,False);f.client.set_enabled(False);settle()
        import json
        saved=json.loads(journal.read_text(encoding='utf-8'))
        self.assertTrue(any(row['id']==identity and entry['id'] in row.get('releases',{}) for row in saved['tasks']))
        before=len(f.peer.calls);f.client.set_enabled(True);settle()
        self.assertFalse(any(m in ('POST','PUT') and '/v1/clipboard' in p and not p.endswith('/release')
            for m,p,d in f.peer.calls[before:]),f.peer.calls[before:])
        self.assertTrue(any(p.endswith('/release') for m,p,d in f.peer.calls[before:]))
        self.assertTrue(path.exists());self.assertTrue(Path(f.clip.mimeData().urls()[0].toLocalFile()).samefile(path))
        f.clip.text('new copy after enable');until(lambda:f.peer.head and f.peer.head.get('kind')=='text')
        self.assertEqual(f.client.view['value'][1]['text'],'new copy after enable')

    def test_shelf_upload_off_cancels_only_old_shared_payload_not_latest_clipboard(self):
        f=self.f;path,identity,entry=self.held_entry()
        f.clip.text('new independent clipboard')
        until(lambda:f.peer.head and f.peer.head.get('kind')=='text' and f.client._pending is None)
        head=f.peer.head['id'];old_key=f.peer.held[identity][0]
        self.assertIn(old_key,f.peer.active)
        f.engine.set_directions(False,False)
        until(lambda:old_key in f.peer.cancelled)
        self.assertNotIn('shared_clipboard',entry)
        self.assertTrue(entry['dirty']);self.assertEqual(f.peer.head['id'],head)
        self.assertEqual(f.clip.mimeData().text(),'new independent clipboard')
        before=len(f.peer.puts);f.client.file_jobs._retry();settle(.03)
        self.assertEqual(len(f.peer.puts),before);self.assertTrue(path.exists())

    def test_old_retain_reply_cannot_release_a_new_hold_after_quick_direction_toggle(self):
        f=self.f;path=f.source();identity=f.copy(path);until(lambda:identity in f.peer.held)
        f.peer.finish(identity);until(lambda:f.client._pending is None)
        original=f.peer.request;delayed=[]
        def request(method,url,callback,**options):
            if url.endswith('/retain'):
                return original(method,url,lambda result,error:delayed.append(lambda:callback(result,error)),**options)
            return original(method,url,callback,**options)
        with patch.object(f.peer,'request',side_effect=request),patch.object(f.engine.clipboard_bridge,'_link'):
            entry=f.add(path);until(lambda:len(delayed)==1)
            f.engine.set_directions(False,False);settle(.03)
            f.engine.set_directions(True,False);until(lambda:len(delayed)==2)
            delayed[1]();flush()
            self.assertIn(entry['id'],f.client.file_jobs.tasks[identity]['holders'])
            delayed[0]();flush()
            self.assertIn(entry['id'],f.client.file_jobs.tasks[identity]['holders'])
            self.assertEqual(entry.get('shared_clipboard'),identity)

    def test_old_hash_claim_cannot_touch_new_probe_after_clipboard_toggle(self):
        from types import SimpleNamespace
        import threading
        f=self.f;path=f.source();identity=f.copy(path);until(lambda:identity in f.peer.held)
        f.peer.finish(identity);until(lambda:f.client._pending is None)
        duplicate=f.base/'duplicate.bin';duplicate.write_bytes(path.read_bytes());callbacks=[]
        def delayed_work(action,callback):
            callbacks.append(callback);return SimpleNamespace(cancelled=threading.Event())
        with patch.object(f.client,'_work',side_effect=delayed_work):
            entry=f.add(duplicate);until(lambda:len(callbacks)==1)
            owner_epoch=f.engine.epoch
            f.client.set_enabled(False);f.engine.debounce.stop()
            f.client.set_enabled(True);f.engine.debounce.stop()
            self.assertEqual(f.engine.epoch,owner_epoch)
            self.assertGreaterEqual(len(callbacks),2)
            self.assertIn(entry['id'],f.engine.clipboard_bridge.probing)
            with patch.object(f.client.file_jobs,'_hold') as held:
                callbacks[0](shared.hashlib.sha256(duplicate.read_bytes()).hexdigest())
                held.assert_not_called()
            self.assertIn(entry['id'],f.engine.clipboard_bridge.probing)


class NoticeSwitchTests(unittest.TestCase):
    def test_disabled_feature_rejects_late_thumbnail_without_render_or_revival(self):
        from types import SimpleNamespace
        from PySide6.QtCore import QObject,Signal
        from intake_notice import ClipboardNotice
        class Signals(QObject):done=Signal(object,object)
        class Work:
            def __init__(self,action,*args):
                import threading
                self.signals=Signals();self.cancel=threading.Event()
        manager=SimpleNamespace(settings=SimpleNamespace(theme='light',clipboard_sync_enabled=True,
            clipboard_success_notice=True),_quitting=False,_clipboard_runtime_enabled=lambda:True)
        notice=ClipboardNotice(manager)
        try:
            # Synthetic descriptor; the intercepted worker never touches disk.
            with patch('sync_files.DiskWork',Work),patch('PySide6.QtCore.QThreadPool.globalInstance') as pool:
                pool.return_value.start=lambda *args:None
                notice.show_result(True,dict(value=('files',['synthetic-path.bin']),point=(0,0)))
                job=notice._job;self.assertIsNotNone(job)
                manager.settings.clipboard_sync_enabled=False
                with patch.object(notice,'_render') as rendered:
                    job.signals.done.emit((None,123),None);flush()
                    rendered.assert_not_called()
                self.assertFalse(notice.isVisible());self.assertFalse(notice.motion.active)
                self.assertIsNone(notice._pending_result)
        finally:notice.supersede();notice.deleteLater();flush()


if __name__=='__main__':unittest.main()
