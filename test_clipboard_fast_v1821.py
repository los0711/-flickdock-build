"""Bounded silent text push, latest-wins and legacy transport fallback."""
import base64,copy,hashlib,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QTimer
from cloud_events import CloudEvents
from clipboard_sync import ClipboardSync,inline_text,unpack_inline
from test_clipboard_sync_v1819 import Account,Clipboard,Hub,APP,flush,until


class InlineHub(Hub):
    def __init__(self):super().__init__();self.version=0
    def api(self,device):
        classic=super().api(device);hub=self
        class Api:
            def cancel(self,key):classic.cancel(key)
            def request(self,method,path,callback,**options):
                data=options.get('data',{})
                if method!='POST' or not data.get('inline'):return classic.request(method,path,callback,**options)
                hub.calls.append((method,path));key=object()
                def deliver():
                    hub.version+=1;hub.body=base64.b64decode(data['inline']['body'])
                    hub.head=dict(id=data['id'],device=device,ready=True,version=hub.version,
                        copied_at=data['copied_at'],kind='text',size=len(hub.body),sha256=hashlib.sha256(hub.body).hexdigest(),
                        inline=data['inline']['body'])
                    value=dict(schema_version=1,clipboard=copy.deepcopy(hub.head),server_time=round(time.time()*1000))
                    callback(value,None)
                    for event in hub.events:
                        if event.account.device_id!=device:
                            event._message(json.dumps(dict(value,type='clipboard_inline')))
                            event._message(json.dumps(dict(schema_version=1,type='clipboard',version=hub.version)))
                QTimer.singleShot(0,deliver);return key
        return Api()


class ClipboardFastTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.hub=InlineHub();self.clients=[];self.clips=[]
        for index in range(2):
            clip=Clipboard();account=Account(self.hub.api(str(index)),str(index))
            events=CloudEvents(account,enabled=False);self.hub.events.append(events)
            self.clients.append(ClipboardSync(account,events,Path(self.temp.name)/str(index),clipboard=clip));self.clips.append(clip)
        flush()

    def tearDown(self):
        for event in self.hub.events:event.close()
        for client in self.clients:client.close()
        until(lambda:all(not client._jobs for client in self.clients));flush();self.temp.cleanup()

    def test_small_text_uses_single_publish_no_body_or_disk_and_never_echoes(self):
        sender,receiver=self.clients
        until(lambda:not sender._fetching and not receiver._fetching)
        before=len(self.hub.calls)
        with (patch.object(sender,'_directory',side_effect=AssertionError('text must not write a body file')),
              patch.object(receiver,'_directory',side_effect=AssertionError('inline receive must stay in memory'))):
            self.clips[0].text('instant 中文\r\nhttps://example.invalid')
            until(lambda:self.clips[1].mimeData().text()=='instant 中文\r\nhttps://example.invalid')
        calls=self.hub.calls[before:]
        self.assertEqual(sum(method=='POST' for method,path in calls),1)
        self.assertFalse(any(path.endswith('/content') for method,path in calls))
        for _ in range(20):self.assertEqual(self.clips[1].mimeData().text(),'instant 中文\r\nhttps://example.invalid')
        deadline=time.monotonic()+.12
        while time.monotonic()<deadline:APP.processEvents()
        self.assertEqual(sum(method=='POST' for method,path in self.hub.calls[before:]),1)

    def test_new_committed_text_preempts_old_receive_and_ignores_reordered_push(self):
        receiver=self.clients[1];until(lambda:not receiver._fetching)
        item=inline_text(('text',dict(text='newest',html='<b>newest</b>')))
        self.hub.head=dict(id='12345678-1234-4234-8234-123456789abc',device='0',ready=True,version=9,
            copied_at=round(time.time()*1000),kind='text',size=item['size'],sha256=item['sha256'],inline=item['inline']['body'])
        receiver._fetching=True;receiver._receiving_version=3
        receiver._pushed(dict(clipboard=copy.deepcopy(self.hub.head)))
        until(lambda:self.clips[1].mimeData().text()=='newest')
        old=dict(self.hub.head,version=8,id='22345678-1234-4234-8234-123456789abc')
        receiver._pushed(dict(clipboard=old));flush()
        self.assertEqual(receiver._seen,self.hub.head['id'])
        self.assertEqual(self.clips[1].mimeData().html(),'<b>newest</b>')

    def test_inline_integrity_and_threshold_preserve_original_text(self):
        item=inline_text(('text',dict(text='a'*32756,html='')))
        self.assertEqual(item['size'],32768)
        self.assertEqual(unpack_inline(dict(item,inline=item['inline']['body']))[1]['text'],'a'*32756)
        self.assertIsNone(inline_text(('text',dict(text='a'*32757,html=''))))
        with self.assertRaises(ValueError):unpack_inline(dict(item,inline=item['inline']['body'],sha256='0'*64))

    def test_duplicate_notification_does_not_invalidate_an_inflight_receive(self):
        receiver=self.clients[1];until(lambda:not receiver._fetching)
        original=receiver._request
        def request(method,path,callback,**options):
            if self.hub.head and method=='GET' and path=='/v1/clipboard':
                self.clips[1].dataChanged.emit()
            return original(method,path,callback,**options)
        with patch.object(receiver,'_request',side_effect=request):
            self.clips[0].text('copy after updating')
            until(lambda:self.clips[1].mimeData().text()=='copy after updating')

    def test_confirmation_error_retries_without_waiting_for_poll(self):
        receiver=self.clients[1];until(lambda:not receiver._fetching)
        original=receiver._request;failed=[]
        def request(method,path,callback,**options):
            if self.hub.head and method=='GET' and path=='/v1/clipboard' and not failed:
                failed.append(True);QTimer.singleShot(0,lambda:callback(None,OSError('connection changed')));return None
            return original(method,path,callback,**options)
        with patch.object(receiver,'_request',side_effect=request):
            self.clips[0].text('retry final confirmation')
            until(lambda:failed and receiver.retry.isActive())
            receiver.retry.stop();receiver._retry()
            until(lambda:self.clips[1].mimeData().text()=='retry final confirmation')

    def test_native_write_refusal_does_not_mark_applied_and_recovers(self):
        receiver=self.clients[1];until(lambda:not receiver._fetching)
        with patch.object(self.clips[1],'setMimeData',return_value=None):
            self.clips[0].text('copy while native clipboard is busy')
            until(lambda:receiver.retry.isActive() and not receiver._fetching)
            self.assertNotEqual(receiver._seen,self.hub.head['id']);self.assertEqual(receiver._applied_version,0)
        receiver.retry.stop();receiver._retry()
        until(lambda:self.clips[1].mimeData().text()=='copy while native clipboard is busy')

    def test_restart_recovers_own_latest_cloud_clipboard(self):
        self.clips[0].text('own copy before updating')
        until(lambda:self.clips[1].mimeData().text()=='own copy before updating')
        sender=self.clients[0];sender.close()
        clip=Clipboard();events=self.hub.events[0]
        restarted=ClipboardSync(sender.account,events,Path(self.temp.name)/'restart',clipboard=clip)
        self.clients.append(restarted)
        until(lambda:clip.mimeData().text()=='own copy before updating')
        self.assertEqual(restarted._seen,self.hub.head['id'])

    def test_temporarily_unreadable_native_copy_keeps_sequence_for_retry(self):
        sender=self.clients[0];until(lambda:not sender._fetching)
        sender._sequence=lambda:12;sender._last_sequence=11
        self.clips[0].text('delayed native rendering')
        original=self.clips[0].mimeData
        with patch.object(self.clips[0],'mimeData',return_value=None):
            sender._changed()
            self.assertEqual(sender._last_sequence,11);self.assertTrue(sender.change_debounce.isActive())
        sender._changed()
        until(lambda:self.clips[1].mimeData().text()=='delayed native rendering')

    def test_sender_feedback_only_after_commit_and_no_recipient_feedback(self):
        sent=[];received=[]
        self.clients[0].copy_result.connect(lambda ok,data:sent.append((ok,data)))
        self.clients[1].copy_result.connect(lambda ok,data:received.append((ok,data)))
        self.clips[0].text('sender acknowledgement')
        self.assertEqual(sent,[])
        until(lambda:self.clips[1].mimeData().text()=='sender acknowledgement')
        self.assertEqual(len(sent),1);self.assertTrue(sent[0][0]);self.assertEqual(sent[0][1]['id'],self.hub.head['id'])
        self.assertEqual(received,[])

    def test_failed_publish_reports_failure_then_success_after_real_retry(self):
        sender=self.clients[0];results=[];sender.copy_result.connect(lambda ok,data:results.append(ok))
        original=sender._request;failed=[]
        def request(method,path,callback,**options):
            if method=='POST' and not failed:
                failed.append(True);QTimer.singleShot(0,lambda:callback(None,OSError('offline')));return None
            return original(method,path,callback,**options)
        with patch.object(sender,'_request',side_effect=request):
            self.clips[0].text('retry publication')
            until(lambda:sender.retry.isActive());self.assertEqual(results,[False]);self.assertIsNone(self.hub.head)
            sender.retry.stop();sender._retry()
            until(lambda:self.clips[1].mimeData().text()=='retry publication')
        self.assertEqual(results,[False,True])

    def test_new_copy_suppresses_synchronous_cancel_error_before_view_is_replaced(self):
        sender=self.clients[0];results=[];sender.copy_result.connect(lambda ok,data:results.append((ok,data['id'])))
        until(lambda:not sender._fetching)
        original=sender._prepare_local
        # Keep the first real copy pending, then inject the synchronous cancel
        # callback at the exact replacement point, before _set_view(new id).
        with patch.object(sender,'_prepare_local',return_value=None):
            self.clips[0].text('first copy waiting to upload')
            until(lambda:sender._local_retry is not None)
        old=sender._local_retry;supersede=sender.file_jobs.supersede
        def interrupted():
            supersede();sender._task_error(old,RuntimeError('superseded by newer copy'))
        with patch.object(sender.file_jobs,'supersede',side_effect=interrupted):
            self.clips[0].text('latest copy')
            until(lambda:self.clips[1].mimeData().text()=='latest copy')
        self.assertEqual(results,[(True,self.hub.head['id'])])
        self.assertNotEqual(self.hub.head['id'],old['id'])


if __name__=='__main__':unittest.main()
