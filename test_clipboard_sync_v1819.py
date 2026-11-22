"""Isolated clipboards, original bytes, screenshot pixels and replacement races."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import hashlib,json,tempfile,unittest,zipfile,time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PySide6.QtCore import QObject,Signal,QMimeData,QTimer,QUrl
from PySide6.QtGui import QImage,QColor
from PySide6.QtTest import QTest
from test_html_native_workspace import APP,until,flush
from clipboard_sync import ClipboardSync,package,unpack,snapshot,MAX_BYTES,inline_text
from cloud_api import CloudError
from uuid import uuid4
import base64


class Clipboard(QObject):
    dataChanged=Signal()
    def __init__(self):super().__init__();self.mime=QMimeData()
    def mimeData(self):return self.mime
    def setMimeData(self,mime):self.mime=mime;self.dataChanged.emit()
    def clear(self):self.setMimeData(QMimeData())
    def text(self,text):mime=QMimeData();mime.setText(text);self.setMimeData(mime)


class Account(QObject):
    account_changed=Signal(object)
    def __init__(self,api,device):super().__init__();self.api=api;self.device_id=device;self.signed_in=True


class Events(QObject):
    clipboard_changed=Signal()
    clipboard_delivery=Signal(dict)
    connected=Signal()
    disconnected=Signal()
    errorOccurred=Signal(object)
    def __init__(self):super().__init__();self.socket=self
    def state(self):
        from PySide6.QtNetwork import QAbstractSocket
        return QAbstractSocket.SocketState.ConnectedState


class Hub:
    def __init__(self):self.head=None;self.pending=None;self.body=None;self.events=[];self.calls=[];self.delivery=None;self.version=0
    def api(self,device):
        hub=self
        class Api:
            def request(self,method,path,callback,**kw):
                key=object();hub.calls.append((method,path));
                def deliver():
                    result=None;error=None
                    if path.endswith('/delivery') and method=='POST':
                        if not hub.head or hub.head['id'] not in path:error=RuntimeError('replaced')
                        else:
                            if not hub.delivery or hub.delivery['id']!=hub.head['id']:
                                hub.delivery=dict(id=hub.head['id'],sha256=hub.head['sha256'],origin=hub.head['device'],plan={},received={},receipts={})
                            data=kw['data']
                            if 'plan' in data:hub.delivery['plan']=dict(data['plan'])
                            if 'route' in data:
                                hub.delivery['received'][device]=data['route']
                                if data.get('stage')!='received':hub.delivery['receipts'][device]=data['route']
                            result={'delivery':dict(hub.delivery)}
                    elif path=='/v1/clipboard' and method=='POST':
                        hub.version+=1
                        hub.pending=dict(id=kw['data']['id'],device=device,ready=False,version=hub.version,copied_at=kw['data']['copied_at']);result={'clipboard':dict(hub.pending)}
                    elif path=='/v1/clipboard':result={'clipboard':dict(hub.head) if hub.head else None,'delivery':hub.delivery}
                    elif method=='PUT':
                        if not hub.pending or hub.pending['id'] not in path:error=RuntimeError('replaced')
                        else:
                            hub.head=hub.pending;hub.pending=None
                            hub.body=Path(kw['source']).read_bytes();hub.head.update(ready=True,kind=kw['headers']['X-Clipboard-Kind'],size=len(hub.body),sha256=hashlib.sha256(hub.body).hexdigest());result={'clipboard':dict(hub.head)}
                    else:
                        if not hub.head or hub.head['id'] not in path or hub.body is None:error=RuntimeError('replaced')
                        else:
                            assert len(hub.body)==kw['expected']['size'] and hashlib.sha256(hub.body).hexdigest()==kw['expected']['sha256']
                            Path(kw['destination']).write_bytes(hub.body);result=str(kw['destination'])
                    if isinstance(result,dict):result['server_time']=round(time.time()*1000)
                    callback(result,error)
                    if path.endswith('/delivery') and result is not None:
                        for event in hub.events:event.clipboard_delivery.emit(dict(hub.delivery))
                    elif method in ('POST','PUT'):
                        for event in hub.events:event.clipboard_changed.emit()
                QTimer.singleShot(0,deliver);return key
            def cancel(self,key):pass # Deliberately deliver late: generation guards must protect the slot.
        return Api()


class ClipboardTests(unittest.TestCase):
    def _hold_conflicting_copy(self,*,old_head=False,index=0):
        client=self.clients[index];request=client.api.request;rejected=[]
        def dispatch(method,path,callback,**options):
            if method=='POST' and path=='/v1/clipboard' and not rejected:
                rejected.append(True)
                if old_head:
                    item=inline_text(('text',dict(text='the previous committed copy',html='')))
                    self.hub.version+=1;self.hub.body=base64.b64decode(item['inline']['body'])
                    self.hub.head=dict(id=str(uuid4()),device=str(1-index),version=self.hub.version,ready=True,kind='text',
                        size=item['size'],sha256=item['sha256'],inline=item['inline']['body'],copied_at=options['data']['copied_at']-1)
                QTimer.singleShot(0,lambda:callback(None,CloudError('reservation replaced',409,'CLIPBOARD_REPLACED')))
                return object()
            return request(method,path,callback,**options)
        client.api.request=dispatch;self.clips[index].text('keep this actual copy while the other commit is pending')
        until(lambda:bool(rejected) and (client._pending is None or bool(client._pending.get('replacement_deadline'))))
        return client

    def test_conflicting_reservation_waits_for_a_verified_committed_body(self):
        client=self._hold_conflicting_copy();self.assertIsNotNone(client._pending)
        pending=client._pending;self.assertEqual(client.view['cloud_transfer']['phase'],'verifying')
        self.assertEqual(client.view['phase'],'uploading')
        item=pending['package'];self.hub.version+=1
        self.hub.head=dict(id=str(uuid4()),device='1',version=self.hub.version,ready=True,kind='text',
            size=item['size'],sha256=item['sha256'],inline=item['inline']['body'],copied_at=pending['intent']['at']+1)
        self.hub.body=base64.b64decode(item['inline']['body']);client.request_refresh()
        until(lambda:client._pending is None and client.view.get('backed_up'))
        self.assertEqual(client.view['phase'],'synced');self.assertEqual(client.view['copy_id'],pending['id'])
        self.assertEqual(self.clips[0].mimeData().text(),'keep this actual copy while the other commit is pending')

    def test_previous_committed_body_is_not_misreported_as_a_newer_copy(self):
        client=self._hold_conflicting_copy(old_head=True)
        self.assertIsNotNone(client._pending);self.assertEqual(client.view['phase'],'uploading')
        self.assertEqual(client.view['cloud_transfer']['phase'],'verifying')
        self.assertEqual(self.clips[0].mimeData().text(),'keep this actual copy while the other commit is pending')

    def test_same_millisecond_lower_device_head_is_not_the_committed_winner(self):
        client=self._hold_conflicting_copy(old_head=True,index=1);pending=client._pending
        self.hub.head['copied_at']=pending['intent']['at'];client.request_refresh()
        QTest.qWait(40);flush()
        self.assertIs(client._pending,pending);self.assertEqual(client.view['cloud_transfer']['phase'],'verifying')

    def test_unconfirmed_replacement_stops_checks_and_an_actual_recopy_can_retry(self):
        client=self._hold_conflicting_copy();self.assertIsNotNone(client._pending)
        identity=client._pending['id'];client._pending['replacement_deadline']=time.monotonic()-1
        before=sum(method=='GET' and path=='/v1/clipboard' for method,path in self.hub.calls)
        client._scheduled_check();flush()
        self.assertIsNone(client._pending);self.assertEqual(client.view['phase'],'error')
        self.assertIsNone(client.schedule.periodic_due);self.assertFalse(client.retry.isActive())
        self.assertEqual(sum(method=='GET' and path=='/v1/clipboard' for method,path in self.hub.calls),before)
        self.clips[0].text('keep this actual copy while the other commit is pending')
        until(lambda:client.view.get('backed_up') and client.view['id']!=identity)
        self.assertEqual(client.view['phase'],'synced')

    def test_duplicate_delayed_native_notification_resumes_interrupted_cloud_receive(self):
        receiver=self.clients[1];native_snapshot=receiver._snapshot;api=receiver.api.request
        held=[];sequence=[0];receiver._sequence=lambda:sequence[0];receiver._last_sequence=0
        def request(method,path,callback,**options):
            if method=='GET' and path.endswith('/content') and not held:
                held.append(lambda:api(method,path,callback,**options));return object()
            return api(method,path,callback,**options)
        receiver.api.request=request
        source=self.base/'delayed-native.bin';source.write_bytes(b'actual received bytes')
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))]);self.clips[0].setMimeData(mime)
        until(lambda:bool(held))
        def unavailable():receiver._snapshot_available=False;return None
        sequence[0]=1
        with patch.object(receiver,'_snapshot',side_effect=unavailable):receiver._schedule_change()
        receiver.change_debounce.stop()
        receiver._refresh_again=False
        held[0]();flush()
        self.assertFalse(self.clips[1].mimeData().hasUrls())
        # The native owner is readable again and still has the same old data.
        receiver._changed();until(lambda:self.clips[1].mimeData().hasUrls(),seconds=3)
        received=Path(self.clips[1].mimeData().urls()[0].toLocalFile())
        self.assertEqual(received.read_bytes(),source.read_bytes())
        self.assertEqual(receiver.view['phase'],'synced')

    def _replace_first_copy(self,remote_text,*,same_clock=False):
        client=self.clients[0];original=client.api.request;replaced=[]
        def request(method,path,callback,**options):
            if method=='POST' and path=='/v1/clipboard' and not replaced:
                item=inline_text(('text',dict(text=remote_text,html='')))
                self.hub.version+=1
                self.hub.head=dict(id=str(uuid4()),device='1',version=self.hub.version,ready=True,
                    kind='text',size=item['size'],sha256=item['sha256'],inline=item['inline']['body'],
                    copied_at=options['data']['copied_at']+(0 if same_clock else 1))
                self.hub.body=base64.b64decode(item['inline']['body']);replaced.append(True)
                QTimer.singleShot(0,lambda:callback(None,CloudError('replaced',409,'CLIPBOARD_REPLACED')))
                return object()
            return original(method,path,callback,**options)
        client.api.request=request
        return client

    def test_identical_cloud_winner_confirms_actual_content_without_false_failure_or_native_rewrite(self):
        client=self._replace_first_copy('same clipboard from both devices');results=[];changes=[]
        client.copy_result.connect(lambda ok,context:results.append((ok,context)))
        self.clips[0].dataChanged.connect(lambda:changes.append(True))
        self.clips[0].text('same clipboard from both devices')
        until(lambda:client._pending is None and client.view['phase']=='synced')
        self.assertEqual(client.view['id'],self.hub.head['id']);self.assertTrue(client.view['backed_up'])
        self.assertEqual([ok for ok,_ in results],[True]);self.assertEqual(len(changes),1)
        self.assertEqual(sum(method=='PUT' for method,path in self.hub.calls),0)

    def test_different_newer_copy_retires_old_intent_without_failure_or_republishing(self):
        client=self._replace_first_copy('newer remote contents');results=[]
        client.copy_result.connect(lambda ok,context:results.append((ok,context)))
        self.clips[0].text('older local contents')
        until(lambda:self.clips[0].mimeData().text()=='newer remote contents' and client._pending is None)
        self.assertTrue(results);self.assertTrue(all(context['phase']=='superseded' for ok,context in results))
        self.assertFalse(client.retry.isActive());self.assertTrue(client.view['backed_up'])

    def test_same_millisecond_winner_is_applied_using_the_server_device_order(self):
        client=self._replace_first_copy('winning device contents',same_clock=True)
        self.clips[0].text('local content from the lower device')
        until(lambda:self.clips[0].mimeData().text()=='winning device contents' and client._pending is None)
        self.assertEqual(client._seen,self.hub.head['id']);self.assertTrue(client.view['backed_up'])

    def test_same_text_with_remote_format_conversion_is_not_a_newer_copy(self):
        client=self._replace_first_copy('same text from a clipboard bridge');results=[];changes=[]
        client.copy_result.connect(lambda ok,context:results.append((ok,context)))
        self.clips[0].dataChanged.connect(lambda:changes.append(True))
        mime=QMimeData();mime.setText('same text from a clipboard bridge')
        original_html='<p><b>same text from a clipboard bridge</b></p>';mime.setHtml(original_html)
        self.clips[0].setMimeData(mime)
        until(lambda:client._pending is None and client.view.get('backed_up'))
        self.assertEqual([ok for ok,_ in results],[True])
        self.assertTrue(all(context['phase']=='synced' for _,context in results))
        self.assertEqual(self.clips[0].mimeData().html(),original_html)
        self.assertEqual(len(changes),1);self.assertEqual(client.view['id'],self.hub.head['id'])
        self.assertEqual(sum(method=='PUT' for method,_ in self.hub.calls),0)

    def test_real_transport_failure_still_reports_failure_and_keeps_native_copy(self):
        client=self.clients[0];original=client.api.request;results=[]
        def request(method,path,callback,**options):
            if method=='POST' and path=='/v1/clipboard':
                QTimer.singleShot(0,lambda:callback(None,CloudError('server failure',503,'UNAVAILABLE')));return object()
            return original(method,path,callback,**options)
        client.api.request=request;client.copy_result.connect(lambda ok,context:results.append((ok,context)))
        self.clips[0].text('native copy survives a real error');until(lambda:bool(results))
        self.assertEqual(results[0][0],False);self.assertEqual(results[0][1]['phase'],'error')
        self.assertEqual(self.clips[0].mimeData().text(),'native copy survives a real error')

    def test_nonempty_unreadable_native_formats_never_erase_the_confirmed_file_preview(self):
        sender=self.clients[0];source=self.base/'confirmed-file.bin';source.write_bytes(b'confirmed')
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))]);self.clips[0].setMimeData(mime)
        until(lambda:sender._pending is None and sender.view['phase']=='synced')
        previous=sender.view['value'];identity=sender.view['id'];sender._sequence=lambda:100;sender._last_sequence=99
        with patch.object(sender,'_is_native',True),patch.object(self.clips[0],'mimeData',return_value=QMimeData()),patch('clipboard_sync.native_format_count',return_value=4):
            for _ in range(8):sender._changed()
            self.assertIs(sender.view['value'],previous);self.assertEqual(sender.view['id'],identity)
            self.assertTrue(sender.view['native_read_pending']);self.assertEqual(sender._last_sequence,99)
        sender.change_debounce.stop()

    def test_stalled_clipboard_upload_releases_priority_and_real_progress_renews_it(self):
        sender=self.clients[0];original=sender.api.request;held=[];events=[]
        sender.activity_changed.connect(events.append)
        def request(method,path,callback,**options):
            if method=='PUT':held.append((method,path,callback,options));return object()
            return original(method,path,callback,**options)
        sender.api.request=request
        source=self.base/'stalled-priority.bin';source.write_bytes(os.urandom(128*1024))
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))]);self.clips[0].setMimeData(mime)
        until(lambda:bool(held));self.assertTrue(sender._priority_active())
        until(lambda:not sender._priority_active(),seconds=2)
        method,path,callback,options=held.pop();size=Path(options['source']).stat().st_size
        options['progress'](min(size-1,8192),size)
        self.assertTrue(sender._priority_active());self.assertEqual(events[-3:],[True,False,True])
        options['progress'](size,size);self.assertFalse(sender._priority_active())
        original(method,path,callback,**options)
        until(lambda:sender._pending is None)

    def test_unreadable_native_clipboard_does_not_erase_a_confirmed_preview(self):
        sender=self.clients[0];self.clips[0].text('confirmed preview')
        until(lambda:sender._pending is None and sender.view.get('phase')=='synced')
        previous=sender.view['value'];sender._sequence=lambda:123456
        with patch.object(self.clips[0],'mimeData',return_value=None):
            for _ in range(7):sender._changed()
            self.assertEqual(sender.view['value'],previous)
            self.assertTrue(sender.view['native_read_pending'])
        self.clips[0].text('readable new copy');sender._changed()
        until(lambda:sender._pending is None and sender.view.get('value')==('text',dict(text='readable new copy',html='')))
        self.assertFalse(sender.view['native_read_pending'])

    def test_waiting_for_clipboard_metadata_does_not_throttle_bulk_transfers(self):
        sender=self.clients[0];original=sender.api.request;held=[];activity=[]
        sender.activity_changed.connect(activity.append)
        def request(method,path,callback,**options):
            if method=='POST' and path=='/v1/clipboard':held.append((method,path,callback,options));return object()
            return original(method,path,callback,**options)
        sender.api.request=request
        self.clips[0].text('metadata latency must not limit a different file transfer')
        until(lambda:bool(held));self.assertIsNotNone(sender._pending)
        self.assertFalse(sender._priority_active());self.assertNotIn(True,activity)
        method,path,callback,options=held.pop();original(method,path,callback,**options)
        until(lambda:sender._pending is None)

    def test_uploaded_body_waiting_for_confirmation_releases_bulk_bandwidth_priority(self):
        sender=self.clients[0];original=sender.api.request;held=[]
        def request(method,path,callback,**options):
            if method=='PUT':held.append((method,path,callback,options));return object()
            return original(method,path,callback,**options)
        sender.api.request=request
        source=self.base/'priority.bin';source.write_bytes(os.urandom(128*1024))
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))]);self.clips[0].setMimeData(mime)
        until(lambda:bool(held));self.assertTrue(sender._priority_active())
        method,path,callback,options=held.pop();size=Path(options['source']).stat().st_size
        options['progress'](size,size)
        self.assertIsNotNone(sender._pending);self.assertFalse(sender._priority_active())
        original(method,path,callback,**options)
        until(lambda:sender._pending is None and self.clips[1].mimeData().hasUrls())

    def test_oversize_file_reports_policy_limit_once_without_upload_or_losing_local_clipboard(self):
        self.clips[0].text('previous usable clipboard')
        until(lambda:self.hub.head is not None and self.hub.head['ready'])
        previous=self.hub.head['id'];before=len(self.hub.calls);results=[]
        sender=self.clients[0];sender.copy_result.connect(lambda ok,context:results.append((ok,context)))
        source=self.base/'large-video.mp4'
        with source.open('wb') as stream:stream.truncate(MAX_BYTES+1)
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))]);self.clips[0].setMimeData(mime)
        until(lambda:any(context.get('phase')=='size_limit' for _,context in results))
        self.assertEqual(len(results),1);self.assertFalse(results[0][0])
        self.assertEqual(sender.view['phase'],'size_limit');self.assertIsNone(sender._pending)
        self.assertFalse(sender.retry.isActive());self.assertEqual(self.hub.head['id'],previous)
        self.assertFalse(any(method in ('POST','PUT') for method,_ in self.hub.calls[before:]))
        self.assertEqual(Path(self.clips[0].mimeData().urls()[0].toLocalFile()),source)
        for _ in range(4):self.clips[0].dataChanged.emit();flush()
        self.assertEqual(len(results),1);self.assertEqual(source.stat().st_size,MAX_BYTES+1)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.hub=Hub();self.clients=[];self.clips=[]
        for i in range(3):
            clip=Clipboard();events=Events();self.hub.events.append(events);account=Account(self.hub.api(str(i)),str(i))
            self.clients.append(ClipboardSync(account,events,self.base/str(i),enabled=True,clipboard=clip));self.clips.append(clip)
        flush()
    def tearDown(self):
        for client in self.clients:client.close()
        until(lambda:all(not client._jobs and not client.inbox._thread.is_alive() for client in self.clients));flush();self.temp.cleanup()

    def test_newer_remote_copy_receives_while_an_older_local_upload_is_stalled(self):
        receiver=self.clients[1];original=receiver.api.request;held=[]
        def stalled(method,path,callback,**options):
            if method=='POST' and path=='/v1/clipboard':held.append(callback);return object()
            return original(method,path,callback,**options)
        receiver.api.request=stalled
        self.clips[1].text('older local copy awaiting upload')
        until(lambda:bool(held) and receiver._pending is not None)
        old=receiver._pending;time.sleep(.02)
        self.clips[0].text('newer remote copy')
        until(lambda:self.clips[2].mimeData().text()=='newer remote copy')
        until(lambda:self.clips[1].mimeData().text()=='newer remote copy')
        self.assertIsNone(receiver._pending);self.assertEqual(receiver.view['id'],self.hub.head['id'])
        held[0]({'clipboard':dict(id=old['id'],ready=True,copied_at=old['intent']['at'])},None);flush()
        self.assertEqual(self.clips[1].mimeData().text(),'newer remote copy')

    def test_latest_copy_from_any_device_replaces_and_paste_never_consumes(self):
        self.clips[0].text('first 中文\r\n');until(lambda:self.clips[1].mimeData().text()=='first 中文\r\n')
        first=self.hub.head['id'];self.clips[1].text('second https://example.invalid')
        until(lambda:all(clip.mimeData().text()=='second https://example.invalid' for clip in self.clips))
        self.assertNotEqual(self.hub.head['id'],first);identity=self.hub.head['id'];body=self.hub.body
        for _ in range(20):
            for clip in self.clips:self.assertEqual(clip.mimeData().text(),'second https://example.invalid')
        self.assertEqual(self.hub.head['id'],identity);self.assertEqual(self.hub.body,body)
        self.assertEqual(sum(method=='PUT' for method,_ in self.hub.calls),2) # no echo upload loop

    def test_screenshot_keeps_resolution_and_every_pixel(self):
        image=QImage(640,480,QImage.Format_ARGB32);image.fill(QColor(20,40,70,160));image.setPixelColor(2,3,QColor(1,222,33,255))
        mime=QMimeData();mime.setImageData(image);self.clips[0].setMimeData(mime)
        until(lambda:all(clip.mimeData().hasImage() for clip in self.clips))
        result=self.clips[1].mimeData().imageData();self.assertEqual(result.size(),image.size())
        self.assertEqual(result.convertToFormat(QImage.Format_RGBA8888),image.convertToFormat(QImage.Format_RGBA8888))
        self.assertTrue(self.clips[2].mimeData().hasImage())

    def test_copied_original_file_and_native_image_bytes_are_unchanged(self):
        original=self.base/'原图.png';image=QImage(800,600,QImage.Format_RGB32);image.fill(QColor('#abcdef'));image.save(str(original),'PNG')
        expected=original.read_bytes();mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(original))]);self.clips[0].setMimeData(mime)
        until(lambda:self.clips[1].mimeData().hasUrls())
        path=Path(self.clips[1].mimeData().urls()[0].toLocalFile());self.assertEqual(path.name,original.name);self.assertEqual(path.read_bytes(),expected)
        self.assertEqual(original.read_bytes(),expected)
        mime=QMimeData();mime.setImageData(image);mime.setData('image/png',expected);self.clips[1].setMimeData(mime)
        until(lambda:bytes(self.clips[0].mimeData().data('image/png'))==expected)
        self.assertEqual(self.hub.body,expected)

    def test_new_copy_during_preparation_cannot_restore_old_data_and_limit_preserves_slot(self):
        self.clips[0].text('old');self.clips[0].text('latest')
        until(lambda:self.clips[1].mimeData().text()=='latest')
        previous=self.hub.head['id']
        previous_fence=self.clients[0]._local_fence
        self.clips[0].text('x'*MAX_BYTES)
        until(lambda:self.clients[0]._local_fence is not previous_fence and self.clients[0]._pending is None)
        self.assertEqual(self.hub.head['id'],previous)
        self.assertEqual(self.clips[0].mimeData().text(),'x'*MAX_BYTES)
        self.assertEqual(self.clips[1].mimeData().text(),'latest')
        self.assertTrue(self.hub.head['ready'])

    def test_untrusted_archive_cannot_escape_owned_download_directory(self):
        path=self.base/'unsafe.zip'
        with zipfile.ZipFile(path,'w') as archive:archive.writestr('../escape.txt','bad')
        with self.assertRaises(ValueError):unpack('files',path,self.base)
        self.assertFalse((self.base.parent/'escape.txt').exists())

    def test_modifying_and_copying_the_same_file_path_replaces_its_bytes(self):
        source=self.base/'same.txt';source.write_bytes(b'old')
        def copy_file():
            mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))]);self.clips[0].setMimeData(mime)
        copy_file();until(lambda:self.clips[1].mimeData().hasUrls());first=self.hub.head['id']
        source.write_bytes(b'changed original');copy_file()
        until(lambda:self.hub.head['id']!=first and self.clips[1].mimeData().hasUrls() and Path(self.clips[1].mimeData().urls()[0].toLocalFile()).read_bytes()==b'changed original')

    def test_startup_reaps_expired_owned_cache_and_preserves_unmarked_directories(self):
        base=self.base/'restart';old=base/'clipboard-cache'/'clip-owned';old.mkdir(parents=True)
        (old/'.lease.json').write_text(json.dumps({'flickdock_clipboard':1,'created_at':time.time()-90000}))
        (old/'private.bin').write_bytes(b'old private copy');unmarked=old.parent/'clip-unrelated';unmarked.mkdir();(unmarked/'keep').write_bytes(b'original')
        account=Account(self.hub.api('restart'),'restart');account.signed_in=False
        client=ClipboardSync(account,Events(),base,enabled=True,clipboard=Clipboard());self.clients.append(client)
        until(lambda:not old.exists());self.assertTrue((unmarked/'keep').exists())

    def test_a_file_just_below_20m_keeps_its_bytes_despite_archive_headers(self):
        source=self.base/'near-limit.bin';source.write_bytes(b'x'*(MAX_BYTES-1));out=self.base/'pack';out.mkdir()
        item=package(('files',[str(source)]),out);self.assertIsNotNone(item)
        extracted=unpack('files',item['path'],out)[1][0]
        self.assertEqual(Path(extracted).read_bytes(),source.read_bytes())

    def test_old_pending_copy_is_not_replayed_after_a_long_sleep(self):
        client=self.clients[0];client._pending=dict(id='old',created_at=time.time()-90000,staged=False,prepared=False)
        before=sum(method=='POST' for method,_ in self.hub.calls);client._retry();flush()
        self.assertIsNone(client._pending);self.assertEqual(sum(method=='POST' for method,_ in self.hub.calls),before)

    def test_same_session_profile_refresh_cannot_cancel_a_copy_in_progress(self):
        self.clips[0].text('copy while profile is loading')
        self.clients[0].account.account_changed.emit({'id':'same-session-profile'})
        until(lambda:self.clips[1].mimeData().text()=='copy while profile is loading')
        self.assertTrue(self.hub.head['ready'])

    def test_duplicate_file_notifications_create_one_upload_without_touching_source_clipboard(self):
        source=self.base/'original.txt';source.write_text('original bytes',encoding='utf8')
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))]);self.clips[0].setMimeData(mime)
        until(lambda:self.clips[1].mimeData().hasUrls())
        before=len(self.hub.calls)
        for _ in range(8):self.clips[0].dataChanged.emit()
        flush();time.sleep(.08);flush()
        self.assertEqual(len([m for m,p in self.hub.calls[before:] if m in ('POST','PUT')]),0)
        self.assertIs(self.clips[0].mimeData(),mime)
        self.assertEqual(Path(mime.urls()[0].toLocalFile()).read_text(), 'original bytes')

    def test_temporary_history_paste_and_restoration_are_never_observed_or_uploaded(self):
        client=self.clients[0]
        client._is_native=True
        sequence=[601]
        client._sequence=lambda:sequence[0]
        active=[True]
        observed=[]
        client.copy_observed.connect(observed.append)
        image=QImage(18,12,QImage.Format_ARGB32)
        image.fill(QColor('#82a74e'))
        temporary=QMimeData()
        temporary.setImageData(image)
        with patch('clipboard_sync.image_paste_active',side_effect=lambda:active[0]), \
             patch('clipboard_sync.image_paste_change',side_effect=lambda seq:seq in (601,603)):
            self.clips[0].setMimeData(temporary)
            client._changed()
            self.assertIsNone(client._snapshot())
            client._observed_copy(('image',{'image':image}),601)
            self.assertEqual(observed,[])
            self.assertFalse(any(method=='POST' and path=='/v1/clipboard' for method,path,*_ in self.hub.calls))
            sequence[0]=603
            active[0]=False
            self.clips[0].text('clipboard content retained after paste')
            client._changed()
            self.assertEqual(observed,[])
            self.assertFalse(any(method=='POST' and path=='/v1/clipboard' for method,path,*_ in self.hub.calls))
            sequence[0]=604
            client._observed_copy(('text',{'text':'new explicit user copy'}),604)
            self.assertEqual(observed[0][1]['text'],'new explicit user copy')

    def test_history_payload_replay_without_private_roots_is_never_observed_or_uploaded(self):
        """A card's payload re-announced by the platform must not sync.

        The pasteboard can advance without a sequence this process marked
        (native republishing, delayed restore), and a relocated profile can
        leave the private history roots unconfigured. The pasted payload has to
        stay local in both cases.
        """
        import clipboard_history_native as native
        client=self.clients[0]
        client._is_native=True
        observed=[]
        client.copy_observed.connect(observed.append)
        source=self.base/'fence-card.txt';source.write_text('history card bytes',encoding='utf8')
        value=('files',[str(source)])
        native._remember_mac_history_payload(value)
        sequence=[9101]
        client._sequence=lambda:sequence[0]
        try:
            with patch.object(client,'_history_file_roots',()):
                sequence[0]=9102
                mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))])
                self.clips[0].setMimeData(mime)
                client._changed()
                client._observed_copy(value,9102)
                until(lambda:not client._jobs)
                QTest.qWait(80)
                self.assertEqual(observed,[])
                self.assertFalse(any(method in ('POST','PUT') for method,_ in self.hub.calls))
                self.assertIsNone(client._pending)
        finally:
            native._MAC_HISTORY_ECHOES.clear();native._HISTORY_PASTE_STAMPS.clear()

    def test_history_paste_fence_defers_observation_until_the_restore_settles(self):
        import clipboard_history_native as native
        client=self.clients[0]
        client._is_native=True
        observed=[]
        client.copy_observed.connect(observed.append)
        source=self.base/'fence-settle.txt';source.write_text('settle payload',encoding='utf8')
        value=('files',[str(source)])
        try:
            with native.history_paste_scope(value):
                self.assertTrue(native.history_paste_active())
                self.assertIsNone(client._snapshot())
                self.assertEqual(client._native_format_issue,'HISTORY_PASTE_IN_PROGRESS')
                self.assertTrue(native.history_paste_echo(value))
            # The fence outlives the paste itself for late native notifications.
            self.assertTrue(native.history_paste_active())
            sequence=[9301]
            client._sequence=lambda:sequence[0]
            mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))])
            self.clips[0].setMimeData(mime)
            client._changed()
            client._observed_copy(value,9301)
            self.assertEqual(observed,[])
            self.assertFalse(any(method in ('POST','PUT') for method,_ in self.hub.calls))
        finally:
            native._HISTORY_PASTE_SETTLE_UNTIL=0.
            native._MAC_HISTORY_ECHOES.clear();native._HISTORY_PASTE_STAMPS.clear()

    def test_copy_unrelated_to_a_history_paste_still_reaches_the_other_device(self):
        import clipboard_history_native as native
        client=self.clients[0]
        client._is_native=True
        sequence=[9201]
        client._sequence=lambda:sequence[0]
        card=self.base/'fence-card-source.txt';card.write_text('card payload',encoding='utf8')
        with native.history_paste_scope(('files',[str(card)])):pass
        native._HISTORY_PASTE_SETTLE_UNTIL=0.
        ordinary=self.base/'fence-ordinary.txt';ordinary.write_text('ordinary copy',encoding='utf8')
        self.assertFalse(native.history_paste_echo(('files',[str(ordinary)])))
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(ordinary))])
        self.clips[0].setMimeData(mime)
        until(lambda:self.clients[1].view['phase']=='synced' and self.clients[1].view['value'][0]=='files')
        self.assertEqual(Path(self.clients[1].view['value'][1][0]).read_bytes(),b'ordinary copy')
        native._MAC_HISTORY_ECHOES.clear();native._HISTORY_PASTE_STAMPS.clear()

    def test_reencoded_history_image_cannot_sync_as_a_new_png_file(self):
        from collections import deque
        import clipboard_history_native as native
        from clipboard_history_store import encode_png

        sender,receiver=self.clients[:2]
        sender._is_native=True
        sequence=[741]
        sender._sequence=lambda:sequence[0]
        observed=[];sender.copy_observed.connect(observed.append)
        image=QImage(24,16,QImage.Format_ARGB32);image.fill(QColor('#3a84c1'))
        payload=encode_png(image)
        with patch.object(native,'_MAC_HISTORY_ECHOES',deque(maxlen=32)),patch.object(native,'_HISTORY_PASTE_STAMPS',deque(maxlen=64)):
            native._remember_mac_history_payload(('image',dict(raw=payload)))
            for index in range(3):
                alias=self.base/f'bridge-saved-{index}_d.png'
                image.setText('bridge-export',str(index))
                self.assertTrue(image.save(str(alias),'PNG'))
                self.assertNotEqual(alias.read_bytes(),payload)
                sequence[0]+=1
                mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(alias))]);self.clips[0].setMimeData(mime)
                until(lambda:sender._last_sequence==sequence[0])
            self.assertEqual(observed,[])
            self.assertIsNone(receiver.view['value'])
            self.assertFalse(any(path=='/v1/clipboard' and method=='POST' for method,path in self.hub.calls))

    def test_copying_the_same_history_text_with_a_new_shortcut_still_syncs(self):
        from collections import deque
        import clipboard_history_native as native

        sender,receiver=self.clients[:2]
        sender._is_native=True
        sequence=[831];sender._sequence=lambda:sequence[0]
        value='same history text independently copied by the user'
        with (patch.object(native,'_MAC_HISTORY_ECHOES',deque(maxlen=32)),
              patch.object(native,'_HISTORY_PASTE_STAMPS',deque(maxlen=64)),
              patch.object(native,'_HISTORY_COPY_SHORTCUT',None),patch.object(native,'_HISTORY_COPY_SEQUENCE',None)):
            native._remember_mac_history_payload(('text',{'text':value}))
            self.clips[0].text(value)
            until(lambda:sender._last_sequence==831)
            self.assertIsNone(receiver.view['value'])
            native.note_history_copy_shortcut(sequence[0])
            sequence[0]+=1;self.clips[0].text(value)
            until(lambda:receiver.view['phase']=='synced' and receiver.view['value'][1]['text']==value)

    def test_wetype_native_rejection_does_not_cancel_a_valid_following_copy(self):
        sender,receiver=self.clients[:2]
        sender._is_native=True
        sequence=[901];sender._sequence=lambda:sequence[0]
        sender._native_read_failed('WETYPE_CLIPBOARD_RELAY')
        self.assertEqual(sender._last_sequence,sequence[0])
        self.assertIsNone(receiver.view['value'])
        sequence[0]+=1;self.clips[0].text('ordinary independent Windows copy')
        until(lambda:receiver.view['phase']=='synced')
        self.assertEqual(receiver.view['value'][1]['text'],'ordinary independent Windows copy')

    def test_delayed_received_image_notifications_do_not_echo(self):
        image=QImage(33,22,QImage.Format_ARGB32);image.fill(QColor('red'))
        mime=QMimeData();mime.setImageData(image);self.clips[0].setMimeData(mime)
        until(lambda:self.clips[1].mimeData().hasImage())
        before=sum(m=='PUT' for m,p in self.hub.calls)
        for _ in range(4):self.clips[1].dataChanged.emit()
        time.sleep(.08);flush()
        self.assertEqual(sum(m=='PUT' for m,p in self.hub.calls),before)

    def test_disabling_sync_preserves_native_copy_and_stops_uploads(self):
        self.clients[0].set_enabled(False);self.clips[0].text('local only')
        time.sleep(.08);flush()
        self.assertEqual(self.clips[0].mimeData().text(),'local only');self.assertIsNone(self.hub.head)

    def test_failed_local_staging_preserves_copy_against_older_cloud_and_can_retry(self):
        self.clips[1].text('older remote');until(lambda:self.hub.head and self.hub.head['ready'])
        client=self.clients[0];client._seen=''
        for failure in ('_next_sequence','_directory'):
            expected='new local '+failure+('x'*33000 if failure=='_directory' else '')
            with patch.object(client,failure,side_effect=OSError('temporary disk failure')):
                self.clips[0].text(expected)
                until(lambda:client._local_retry is not None)
                self.assertIsNone(client._pending);self.assertFalse(client._priority_active())
                client.refresh();until(lambda:not client._fetching)
                self.assertEqual(self.clips[0].mimeData().text(),expected)
            client._retry();until(lambda:client._pending is None and client._local_retry is None)
            until(lambda:self.clips[2].mimeData().text()==expected)

    def test_metadata_poll_never_preempts_bulk_and_received_file_survives_disable_expiry(self):
        client=self.clients[0];changes=[];client.activity_changed.connect(changes.append)
        client.refresh();until(lambda:not client._fetching)
        self.assertEqual(changes,[])
        source=self.base/'native.dat';source.write_bytes(b'keep pasteable')
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))]);self.clips[1].setMimeData(mime)
        until(lambda:self.clips[0].mimeData().hasUrls())
        path=Path(self.clips[0].mimeData().urls()[0].toLocalFile());client.set_enabled(False)
        with patch('clipboard_sync.time.time',return_value=time.time()+90000):client._cleanup()
        until(lambda:not client._jobs)
        self.assertEqual(path.read_bytes(),b'keep pasteable')

    def test_near_limit_control_text_is_not_inflated_by_json_escaping(self):
        folder=self.base/'control';folder.mkdir();text='\x00'*(MAX_BYTES-1)
        item=package(('text',dict(text=text,html='')),folder)
        self.assertIsNotNone(item);self.assertEqual(unpack('text',item['path'],folder)[1]['text'],text)

    def test_received_cloud_files_survive_history_restart_and_cache_expiry(self):
        from clipboard_history_runtime import ClipboardHistory
        from clipboard_history_store import ClipboardHistoryStore

        receiver=self.clients[0]
        base=self.base/'0'
        history=ClipboardHistory(SimpleNamespace(app=APP,clipboard_sync=receiver),base)
        history.set_enabled(True)
        try:
            source=self.base/'文件 with spaces.txt';source.write_bytes(b'cloud fixture')
            observed=[];receiver.copy_observed.connect(observed.append)
            mime=QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(source))])
            self.clips[1].setMimeData(mime)
            until(lambda:history.total==1)
            row=history.entries[0]
            cached=Path(self.clips[0].mimeData().urls()[0].toLocalFile())
            self.assertEqual(row['kind'],'file')
            self.assertEqual(observed,[])
            history.close()

            receiver.set_enabled(False);self.clips[0].clear()
            with patch('clipboard_sync.time.time',return_value=time.time()+90000):
                receiver._cleanup()
            until(lambda:not receiver._jobs)
            self.assertFalse(cached.exists())
            reopened=ClipboardHistoryStore(base)
            kind,retained=reopened.execute('payload',row['id'])
            self.assertEqual(kind,'file')
            self.assertEqual(Path(retained[0]).read_bytes(),b'cloud fixture')
            reopened.execute('remove',row['id'])
            self.assertFalse(any(Path(path).exists() for path in retained))
            self.assertTrue(source.is_file())
        finally:
            history.close()

    def test_copies_from_each_device_enter_the_other_devices_history_without_being_republished(self):
        from clipboard_history_runtime import ClipboardHistory
        from clipboard_history_store import ClipboardHistoryStore

        histories=[ClipboardHistory(SimpleNamespace(app=APP,clipboard_sync=client),self.base/str(index))
                   for index,client in enumerate(self.clients)]
        observed=[[],[],[]]
        for index,(client,history) in enumerate(zip(self.clients,histories)):
            client.copy_observed.connect(observed[index].append)
            history.set_enabled(True)

        try:
            with patch('clipboard_history_runtime.private_clipboard',return_value=False):
                text=QMimeData();text.setText('Shared note');text.setHtml('<p><b>Shared note</b></p>')
                self.clips[0].setMimeData(text)
                until(lambda:all(history.total==1 for history in histories))
                self.assertEqual([len(events) for events in observed],[1,0,0])

                address='https://example.test/clipboard'
                self.clips[1].text(address)
                until(lambda:all(history.total==2 for history in histories))
                self.assertEqual([len(events) for events in observed],[1,1,0])

                image=QImage(23,17,QImage.Format_ARGB32);image.fill(QColor('#3a84c1'))
                mime=QMimeData();mime.setImageData(image);self.clips[2].setMimeData(mime)
                until(lambda:all(history.total==3 for history in histories))
                self.assertEqual([len(events) for events in observed],[1,1,1])

            for client in self.clients:client.request_refresh()
            QTest.qWait(150)
            self.assertEqual(self.hub.version,3)
            for index,history in enumerate(histories):
                self.assertEqual(history.total,3)
                rows={row['kind']:row['id'] for row in history.entries}
                self.assertEqual(set(rows),{'text','link','image'})
                history.close()
                store=ClipboardHistoryStore(self.base/str(index))
                self.assertEqual(store.execute('payload',rows['text']),
                                 ('text',dict(text='Shared note',html='<p><b>Shared note</b></p>')))
                self.assertEqual(store.execute('payload',rows['link'])[1]['text'],address)
                restored=QImage.fromData(store.execute('payload',rows['image'])[1])
                self.assertEqual(restored.size(),image.size())
                self.assertEqual(restored.pixelColor(10,10),image.pixelColor(10,10))
        finally:
            for history in histories:history.close()

    def test_remote_clipboard_is_not_saved_while_history_is_disabled(self):
        from clipboard_history_runtime import ClipboardHistory

        receiver=self.clients[0]
        history=ClipboardHistory(SimpleNamespace(app=APP,clipboard_sync=receiver),self.base/'0')
        try:
            self.clips[1].text('Copy while history is disabled')
            until(lambda:self.clips[0].mimeData().text()=='Copy while history is disabled')
            self.assertFalse(history.jobs)
            self.assertFalse(history.store.path.exists())

            history.set_enabled(True)
            self.clips[1].text('Copy after history is enabled')
            until(lambda:history.total==1)
            self.assertEqual(history.entries[0]['title'],'Copy after history is enabled')
        finally:
            history.close()

    def test_mac_history_reissued_file_references_never_reach_another_client(self):
        from clipboard_history_native import _remember_mac_history_payload

        sender,receiver=self.clients[:2]
        private=self.base/'0'/'clipboard-history-files'
        private.mkdir()
        sender._history_file_roots=(Path(str(private.absolute()).casefold()),)
        retained=private/'retained.txt';retained.write_text('clipboard history fixture',encoding='utf8')
        alias=self.base/'bare-reference.txt';os.link(retained,alias)
        _remember_mac_history_payload(('files',[str(retained)]))

        def copy_file(path):
            mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(path))]);self.clips[0].setMimeData(mime)

        ordinary=self.base/'ordinary.txt';ordinary.write_text('normal copy fixture',encoding='utf8')
        copy_file(ordinary)
        until(lambda:receiver.view['phase']=='synced' and receiver.view['value'][0]=='files')
        head=self.hub.head['id']
        self.hub.calls.clear()
        for i in range(3):
            copy_file(alias)
            QTest.qWait(320)
            self.assertEqual(self.hub.head['id'],head)
        self.assertFalse(any(method in ('POST','PUT') and (path=='/v1/clipboard' or path.endswith('/content'))
                             for method,path in self.hub.calls))
        self.assertEqual(Path(receiver.view['value'][1][0]).read_text(encoding='utf8'),'normal copy fixture')

        ordinary.write_text('normal copy still syncs',encoding='utf8')
        copy_file(ordinary)
        until(lambda:receiver.view['phase']=='synced' and receiver.view['id']!=head)
        self.assertEqual(Path(receiver.view['value'][1][0]).read_text(encoding='utf8'),'normal copy still syncs')

    def test_receive_cache_failure_releases_fetch_and_retries_without_changing_native_copy(self):
        client=self.clients[0]
        with patch.object(client,'_directory',side_effect=OSError('temporary cache permission')):
            self.clips[1].text('remote copy after cache recovery')
            until(lambda:self.hub.head and self.hub.head['ready'])
            until(lambda:client.receive_retry.isActive())
            self.assertFalse(client._fetching);self.assertFalse(client._priority_active())
            self.assertFalse(self.clips[0].mimeData().hasText())
        client.receive_retry.stop();client.refresh();until(lambda:self.clips[0].mimeData().text()=='remote copy after cache recovery')

    def test_observed_remote_clock_is_floor_for_only_future_copy(self):
        client=self.clients[0];mono=time.monotonic()
        client._clock_anchor=(mono,10000);client._observed_clock=20000
        captured=dict(mono=mono,at=None,floor=9000)
        client._calibrate(dict(server_time=10001,clipboard={'copied_at':30000}),mono)
        self.assertLess(client._copy_timestamp(captured),30000)
        new=dict(mono=mono,at=None,floor=client._observed_clock)
        self.assertEqual(client._copy_timestamp(new),30001)

    def test_native_write_retry_reuses_verified_file_without_downloading_again(self):
        receiver=self.clients[1];source=self.base/'write-busy.bin';source.write_bytes(b'original fixture bytes')
        with patch.object(self.clips[1],'setMimeData',return_value=None):
            mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))]);self.clips[0].setMimeData(mime)
            until(lambda:receiver.retry.isActive() and receiver._prepared_receive is not None)
            identity=self.hub.head['id'];path='/v1/clipboard/'+identity+'/content'
            before=sum(method=='GET' and endpoint==path for method,endpoint in self.hub.calls)
        receiver.retry.stop();receiver._retry()
        until(lambda:self.clips[1].mimeData().hasUrls())
        self.assertEqual(sum(method=='GET' and endpoint==path for method,endpoint in self.hub.calls),before)
        received=Path(self.clips[1].mimeData().urls()[0].toLocalFile())
        self.assertEqual(received.read_bytes(),source.read_bytes());self.assertEqual(receiver._seen,identity)

    def test_bulk_payload_yields_to_clipboard_and_cancellation_still_works(self):
        import threading
        from transfer_priority import set_clipboard_priority,yield_bulk_payload
        started=threading.Event();finished=threading.Event();cancel=threading.Event()
        def bulk():started.set();yield_bulk_payload(cancel);finished.set()
        set_clipboard_priority(True);thread=threading.Thread(target=bulk);thread.start()
        try:
            self.assertTrue(started.wait(.5))
            set_clipboard_priority(False);self.assertTrue(finished.wait(.5))
            set_clipboard_priority(True);cancel.set()
            with self.assertRaises(InterruptedError):yield_bulk_payload(cancel)
        finally:set_clipboard_priority(False);thread.join(1)


if __name__=='__main__':unittest.main()
