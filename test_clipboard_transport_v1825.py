"""Actual local HTTP/1 connection pressure, not cloud/P95 throughput proof."""
import json,tempfile,threading,time,unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from cloud_api import CloudApi
from clipboard_sync import ClipboardSync
from test_clipboard_sync_v1819 import APP,Account,Clipboard,Events,flush


def spin(predicate,seconds=3):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        APP.processEvents()
        if predicate():return True
        time.sleep(.002)
    return False


class ClipboardTransportTests(unittest.TestCase):
    def test_owned_lane_downloads_exact_png_through_registered_disk_writer_without_echo(self):
        import hashlib
        from PySide6.QtCore import QBuffer,QIODevice
        from PySide6.QtGui import QImage,QColor
        image=QImage(41,23,QImage.Format_RGBA8888);image.fill(QColor('#278fff'))
        buffer=QBuffer();buffer.open(QIODevice.WriteOnly);self.assertTrue(image.save(buffer,'PNG'))
        png=bytes(buffer.data());identity='33333333-3333-4333-8333-333333333333'
        head=dict(id=identity,device='other-device',ready=True,version=1,kind='image',
                  size=len(png),sha256=hashlib.sha256(png).hexdigest(),copied_at=round(time.time()*1000))
        methods=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_GET(self):
                methods.append(('GET',self.path))
                body=png if self.path.endswith('/content') else json.dumps(dict(schema_version=1,clipboard=head)).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(body)))
                self.send_header('Content-Type','image/png' if self.path.endswith('/content') else 'application/json')
                self.end_headers();self.wfile.write(body)
            def do_POST(self):
                methods.append(('POST',self.path));self.send_error(400)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        with tempfile.TemporaryDirectory() as directory:
            primary=CloudApi('http://127.0.0.1:'+str(server.server_port),allow_test_http=True);primary.token='isolated-fixture'
            account=Account(primary,'receiver');account.account={'id':'fixture'};clip=Clipboard()
            client=ClipboardSync(account,Events(),directory,enabled=False,clipboard=clip,independent_transport=True)
            try:
                client.set_enabled(True)
                self.assertTrue(spin(lambda:client._seen==identity and not client._jobs,seconds=5))
                self.assertEqual(bytes(clip.mimeData().data('image/png')),png)
                self.assertEqual(clip.mimeData().imageData().pixelColor(1,1),QColor('#278fff'))
                for _ in range(3):
                    client.refresh();self.assertTrue(spin(lambda:not client._fetching))
                self.assertFalse(any(method=='POST' for method,_ in methods))
                self.assertEqual(sum(path.endswith('/content') for _,path in methods),1)
                self.assertFalse(client.api._writers);self.assertFalse(client.api._requests)
            finally:
                client.close();primary.close();flush();server.shutdown();server.server_close();worker.join(3)

    def test_child_fallback_adopts_primary_origin_and_notifies_events_once(self):
        from cloud_events import CloudEvents
        servers=[];threads=[];arrived=[]
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*_):pass
            def do_GET(self):
                arrived.append(self.server.fixture)
                body=(b'<html>edge unavailable</html>' if self.server.fixture=='first'
                      else b'{"schema_version":1,"ok":true}')
                self.send_response(503 if self.server.fixture=='first' else 200)
                self.send_header('Content-Length',str(len(body)));self.send_header('Connection','close')
                self.close_connection=True;self.end_headers()
                try:self.wfile.write(body)
                except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
        for name in ('first','second'):
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
            server.fixture=name;servers.append(server)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();threads.append(thread)
        origins=tuple('http://127.0.0.1:'+str(s.server_port) for s in servers)
        try:
            with tempfile.TemporaryDirectory() as directory,patch('cloud_api.CLOUD_ORIGINS',origins):
                primary=CloudApi(origins[0],allow_test_http=True);primary.token='fixture'
                account=Account(primary,'device');account.account={'id':'fixture'}
                events=CloudEvents(account,enabled=False) # No WebSocket/network attempt.
                client=ClipboardSync(account,events,directory,enabled=False,clipboard=Clipboard(),independent_transport=True)
                client.enabled=True;changed=[];results=[]
                primary.origin_changed.connect(lambda origin:changed.append(origin))
                try:
                    client._request('GET','/v1/probe',lambda r,e:results.append((r,e)))
                    self.assertTrue(spin(lambda:bool(results)))
                    self.assertIsNone(results[0][1]);self.assertEqual(arrived,['first','second'])
                    self.assertEqual(primary.origin,origins[1]);self.assertEqual(client.api.origin,origins[1])
                    self.assertEqual(changed,[origins[1]]);self.assertEqual(events._identity,'fixture')
                    # A consumer may switch off during the relayed primary
                    # signal. Preserve CloudApi's original-key handoff fence.
                    primary.origin=origins[0];primary.origin_changed.emit(primary.origin)
                    arrived.clear();changed.clear();results.clear()
                    primary.origin_changed.connect(lambda _:client.set_enabled(False))
                    client._request('GET','/v1/cancel-in-fallback',lambda r,e:results.append((r,e)))
                    self.assertTrue(spin(lambda:not client.enabled))
                    self.assertFalse(spin(lambda:len(arrived)>1,seconds=.08))
                    self.assertEqual(arrived,['first']);self.assertFalse(results)
                    self.assertFalse(client._keys);self.assertFalse(client.api._requests)
                    self.assertFalse(client.api._handoffs);self.assertFalse(client.api._fallback_aliases)
                finally:client.close();events.close();primary.close();flush()
        finally:
            for server,thread in zip(servers,threads):server.shutdown();server.server_close();thread.join(3)

    def test_child_origin_relay_rejects_signal_only_unlisted_stale_and_inactive_choices(self):
        from cloud_api import CLOUD_ORIGINS
        first,second=CLOUD_ORIGINS
        with tempfile.TemporaryDirectory() as directory:
            primary=CloudApi(first);primary.token='fixture'
            account=Account(primary,'device');account.account={'id':'fixture'}
            client=ClipboardSync(account,Events(),directory,enabled=False,clipboard=Clipboard(),independent_transport=True)
            client.enabled=True;changed=[];primary.origin_changed.connect(lambda origin:changed.append(origin))
            try:
                client.api.origin_changed.emit(second) # No actual transport origin change.
                self.assertEqual(primary.origin,first);self.assertEqual(changed,[])
                client.api.origin='https://unlisted.invalid';client.api.origin_changed.emit(client.api.origin)
                self.assertEqual(primary.origin,first);self.assertEqual(changed,[])
                client._primary_origin_changed(first)
                # A newer primary choice cannot be overwritten by an old hint.
                primary.origin=second;client.api.origin=second;client.api.origin_changed.emit(second)
                self.assertEqual(primary.origin,second);self.assertEqual(changed,[])
                self.assertEqual(client._transport_origin,second)
                client.set_enabled(False);client.api.origin=first;client.api.origin_changed.emit(first)
                self.assertEqual(primary.origin,second);self.assertEqual(changed,[])
                client.set_enabled(True);self.assertEqual(client.api.origin,second)
                self.assertEqual(client._transport_origin,second)
                client.close();client.api.origin_changed.emit(first)
                self.assertEqual(primary.origin,second);self.assertEqual(changed,[])
            finally:client.close();primary.close();flush()

    def test_old_401_cancelled_during_account_switch_cannot_expire_new_session(self):
        """Actual 401 headers arrive before the old request is cancelled."""
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*_):pass
            def do_GET(self):
                body=b'{"schema_version":1,"error":{"code":"unauthorized"}}'
                self.send_response(401);self.send_header('Content-Length',str(len(body)))
                self.send_header('Connection','close');self.close_connection=True
                self.send_header('Content-Type','application/json');self.end_headers()
                try:self.wfile.write(body)
                except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        with tempfile.TemporaryDirectory() as directory:
            primary=CloudApi('http://127.0.0.1:'+str(server.server_port),allow_test_http=True);primary.token='old-fixture'
            account=Account(primary,'device');account.account={'id':'old'}
            client=ClipboardSync(account,Events(),directory,enabled=False,clipboard=Clipboard(),independent_transport=True)
            client.enabled=True;expired=[];switched=[]
            primary.unauthorized.connect(lambda:expired.append(account.account['id']))
            try:
                key=client._request('GET','/v1/old-request',lambda *_:None)
                reply=client.api._requests[key]['reply']
                def switch_after_headers():
                    if switched:return
                    switched.append(True);primary.token='new-fixture';account.account={'id':'new'}
                    account.account_changed.emit(account.account)
                reply.metaDataChanged.connect(switch_after_headers)
                self.assertTrue(spin(lambda:bool(switched)))
                self.assertFalse(expired,'cancelling old 401 must not expire current account')
                self.assertEqual(client.api.token,primary.token)
            finally:
                client.close();primary.close();flush()
                server.shutdown();server.server_close();thread.join(3)

    def test_clipboard_request_does_not_wait_for_six_bulk_connections(self):
        released=threading.Event();lock=threading.Lock();arrived=[]
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*_):pass
            def do_GET(self):
                with lock:arrived.append(self.path)
                if self.path.startswith('/v1/bulk/') and not released.wait(8):return
                body=json.dumps({'schema_version':1,'accepted':True}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(body)))
                self.send_header('Connection','close');self.close_connection=True
                self.send_header('Content-Type','application/json');self.end_headers()
                try:self.wfile.write(body)
                except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        with tempfile.TemporaryDirectory() as directory:
            primary=CloudApi('http://127.0.0.1:'+str(server.server_port),allow_test_http=True);primary.token='test-only'
            account=Account(primary,'local-fixture');account.account={'id':'fixture'}
            shared=ClipboardSync(account,Events(),Path(directory)/'shared',enabled=False,clipboard=Clipboard())
            priority=ClipboardSync(account,Events(),Path(directory)/'priority',enabled=False,clipboard=Clipboard(),independent_transport=True)
            shared.enabled=priority.enabled=True;shared_results=[];priority_results=[]
            try:
                self.assertIsNot(primary.network,priority.api.network)
                for i in range(6):primary.request('GET',f'/v1/bulk/{i}',lambda *_:None)
                self.assertTrue(spin(lambda:sum(p.startswith('/v1/bulk/') for p in arrived)==6))
                shared._request('GET','/v1/shared-probe',lambda *r:shared_results.append(r))
                self.assertFalse(spin(lambda:bool(shared_results),seconds=.25),'control must actually reproduce shared pool starvation')
                self.assertNotIn('/v1/shared-probe',arrived)
                priority._request('GET','/v1/priority-probe',lambda *r:priority_results.append(r))
                self.assertTrue(spin(lambda:bool(priority_results),seconds=2),'independent clipboard lane must reach server while all six bulk replies remain held')
                self.assertIsNone(priority_results[0][1]);self.assertIn('/v1/priority-probe',arrived)
                self.assertFalse(shared_results);self.assertFalse(released.is_set())
                priority.set_enabled(False);after=len(arrived)
                priority._request('GET','/v1/must-not-send',lambda *_:self.fail('disabled request callback'))
                self.assertFalse(spin(lambda:len(arrived)>after,seconds=.08))
                self.assertEqual(priority._keys,set())
            finally:
                released.set();shared.close();priority.close();primary.close();flush()
                self.assertTrue(priority.api._closed);self.assertFalse(priority.api._requests)
                server.shutdown();server.server_close();thread.join(3)

    def test_owned_transport_uses_current_account_credentials_and_closes_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            primary=CloudApi('https://example.invalid');primary.token='first-fixture'
            account=Account(primary,'device');account.account={'id':'first'}
            client=ClipboardSync(account,Events(),directory,enabled=False,clipboard=Clipboard(),independent_transport=True)
            try:
                client.enabled=True;calls=[]
                client.api.request=lambda method,path,callback,**options:calls.append(client.api.token)
                primary.token='second-fixture';account.account={'id':'second'};account.account_changed.emit(account.account)
                client._request('GET','/v1/probe',lambda *_:None)
                self.assertEqual(calls,['second-fixture']);self.assertEqual(client._identity[0],'second')
                primary.origin='https://new-origin.invalid';primary.origin_changed.emit(primary.origin)
                self.assertEqual(client.api.origin,primary.origin)
                errors=[];primary.unauthorized.connect(lambda:errors.append(True));client.api.unauthorized.emit()
                self.assertEqual(errors,[True])
                client.close();self.assertTrue(client.api._closed);self.assertFalse(primary._closed)
                self.assertEqual(client._subscriptions,[])
            finally:client.close();primary.close();flush()

    def test_unauthorized_requires_current_enabled_session_and_disconnects_on_close(self):
        with tempfile.TemporaryDirectory() as directory:
            primary=CloudApi('https://example.invalid');primary.token='first-fixture'
            account=Account(primary,'device');account.account={'id':'first'}
            client=ClipboardSync(account,Events(),directory,enabled=False,clipboard=Clipboard(),independent_transport=True)
            errors=[];primary.unauthorized.connect(lambda:errors.append(True))
            try:
                client.api.unauthorized.emit();self.assertEqual(errors,[])
                client.enabled=True;client.api.unauthorized.emit();self.assertEqual(errors,[True])
                # Credentials can be assigned just before account_changed; old
                # tasks must neither send under them nor expire their session.
                primary.token='second-fixture';account.account={'id':'second'}
                with patch.object(client.api,'request') as request:
                    self.assertIsNone(client._request('GET','/v1/stale-task',lambda *_:None))
                    request.assert_not_called()
                client.api.unauthorized.emit();self.assertEqual(errors,[True])
                account.account_changed.emit(account.account)
                self.assertEqual(client.api.token,primary.token)
                client.set_enabled(False);self.assertEqual(client.api.token,'')
                client.api.unauthorized.emit();self.assertEqual(errors,[True])
                client.set_enabled(True);self.assertEqual(client.api.token,primary.token)
                client.close();self.assertEqual(client.api.token,'')
                client.api.unauthorized.emit();self.assertEqual(errors,[True])
                before=client.api.origin;primary.origin_changed.emit('https://after-close.invalid')
                self.assertEqual(client.api.origin,before);self.assertFalse(primary._closed)
            finally:client.close();primary.close();flush()


if __name__=='__main__':unittest.main()
