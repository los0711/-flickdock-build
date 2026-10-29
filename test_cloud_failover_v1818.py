"""Actual Qt HTTP: failover, ambiguous mutations, cancellation and deadlines."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import json,socket,threading,time,unittest
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QCoreApplication,QEvent
import cloud_api
APP=QApplication.instance() or QApplication([])

class FailoverTests(unittest.TestCase):
    def setUp(self):
        self.calls=[[],[]];self.events=[threading.Event(),threading.Event()];self.behavior=['ok','ok'];self.servers=[]
        owner=self
        def handler(index):
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*_):pass
                def request(self):
                    body=self.rfile.read(int(self.headers.get('Content-Length',0)));owner.calls[index].append((self.command,self.path,body))
                    if owner.behavior[index]=='drop':self.close_connection=True;return
                    if owner.behavior[index]=='late-drop':time.sleep(.12);self.close_connection=True;return
                    if owner.behavior[index]=='late-gateway':
                        time.sleep(.12)
                        try:self.send_response(503);self.end_headers();self.wfile.write(b'<html>Gateway unavailable</html>')
                        except OSError:pass
                        return
                    if owner.behavior[index]=='wait':owner.events[index].wait(3)
                    result=json.dumps(dict(schema_version=1,ok=True)).encode()
                    try:self.send_response(200);self.send_header('Content-Length',str(len(result)));self.end_headers();self.wfile.write(result)
                    except OSError:pass
                do_GET=request;do_POST=request
            return Handler
        for index in range(2):
            server=ThreadingHTTPServer(('127.0.0.1',0),handler(index));self.servers.append(server)
            threading.Thread(target=server.serve_forever,daemon=True).start()
        self.origins=tuple('http://127.0.0.1:'+str(server.server_port) for server in self.servers)
        self.patch=patch.object(cloud_api,'CLOUD_ORIGINS',self.origins);self.patch.start()
        self.api=cloud_api.CloudApi(self.origins[0],allow_test_http=True)
    def tearDown(self):
        self.api.close();self.api.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);APP.processEvents()
        self.patch.stop()
        for event in self.events:event.set()
        for server in self.servers:server.shutdown();server.server_close()
    def until(self,predicate,seconds=5):
        deadline=time.monotonic()+seconds
        while not predicate() and time.monotonic()<deadline:APP.processEvents();time.sleep(.002)
        self.assertTrue(predicate())
    def unreachable_primary(self):
        self.servers[0].shutdown();self.servers[0].server_close()
    def test_registration_before_connection_failure_reaches_only_verified_alternate_once(self):
        self.unreachable_primary();result=[]
        self.api.request('POST','/v1/auth/register',lambda value,error:result.append((value,error)),authenticated=False,data=dict(username='fixture',password='test-only'))
        self.until(lambda:result)
        self.assertIsNone(result[0][1]);self.assertEqual(result[0][0]['ok'],True)
        self.assertEqual(len(self.calls[1]),1);self.assertEqual(self.calls[0],[])
        self.assertEqual(self.api.origin,self.origins[1]);self.assertFalse(self.api._fallback_aliases)
    def test_post_received_without_response_is_not_automatically_duplicated(self):
        self.behavior[0]='drop';result=[]
        self.api.request('POST','/v1/auth/register',lambda value,error:result.append((value,error)),authenticated=False,data=dict(username='fixture'))
        self.until(lambda:result)
        self.assertIsNotNone(result[0][1]);self.assertTrue(self.calls[0]);self.assertFalse(self.calls[1])
        self.assertEqual(self.api.origin,self.origins[1])
    def test_original_key_cancels_alternate_request_and_cleans_alias(self):
        self.unreachable_primary();self.behavior[1]='wait';result=[]
        key=self.api.request('GET','/v1/sync/changes',lambda value,error:result.append((value,error)),authenticated=False)
        self.until(lambda:self.calls[1]);self.api.cancel(key);self.until(lambda:result)
        self.assertEqual(len(result),1);self.assertEqual(result[0][1].code,'cancelled')
        self.assertFalse(self.api._requests);self.assertFalse(self.api._fallback_aliases)
    def test_fallback_keeps_original_total_deadline(self):
        # Establish both real loopback entrances before timing their remaining
        # request budget. A cold Qt/proxy bootstrap may consume all 220ms before
        # the alternate can be sent; that case has its own cold deadline test.
        for origin in self.origins:
            self.api.origin=origin;ready=[]
            self.api.request('GET','/v1/health',lambda value,error:ready.append((value,error)),authenticated=False)
            self.until(lambda:ready);self.assertIsNone(ready[0][1])
        self.api.origin=self.origins[0]
        for calls in self.calls:calls.clear()
        self.behavior[0]='late-gateway';self.behavior[1]='wait';result=[];started=time.monotonic()
        self.api.request('GET','/v1/sync/changes',lambda value,error:result.append((value,error)),authenticated=False,timeout_ms=220)
        self.until(lambda:result)
        self.assertIsNotNone(result[0][1]);self.assertLess(time.monotonic()-started,.31)
        self.assertEqual(len(self.calls[1]),1,(self.calls,result[0][1],time.monotonic()-started))

    def test_qt_internal_retry_of_dropped_get_cannot_extend_total_deadline(self):
        self.behavior[0]='late-drop';self.behavior[1]='wait';result=[];started=time.monotonic()
        self.api.request('GET','/v1/sync/changes',lambda value,error:result.append((value,error)),authenticated=False,timeout_ms=220)
        self.until(lambda:result)
        self.assertIsNotNone(result[0][1]);self.assertLess(time.monotonic()-started,.31)
        self.assertTrue(self.calls[0]);self.assertLessEqual(len(self.calls[1]),1)

    def test_cancel_during_origin_changed_does_not_send_alternate(self):
        self.unreachable_primary();result=[];holder={}
        self.api.origin_changed.connect(lambda _:self.api.cancel(holder['key']))
        holder['key']=self.api.request('GET','/v1/sync/changes',lambda value,error:result.append((value,error)),authenticated=False)
        self.until(lambda:result)
        self.assertEqual(len(result),1);self.assertEqual(result[0][1].code,'cancelled')
        self.assertFalse(self.calls[1]);self.assertFalse(self.api._requests)
        self.assertFalse(self.api._handoffs);self.assertFalse(self.api._fallback_aliases)

    def test_close_during_origin_changed_does_not_send_or_call_back(self):
        self.unreachable_primary();result=[];changed=[]
        self.api.origin_changed.connect(lambda _: (changed.append(True),self.api.close()))
        self.api.request('GET','/v1/sync/changes',lambda value,error:result.append((value,error)),authenticated=False)
        self.until(lambda:changed);APP.processEvents()
        self.assertFalse(result);self.assertFalse(self.calls[1]);self.assertFalse(self.api._handoffs)

if __name__=='__main__':unittest.main()
