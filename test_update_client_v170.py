"""Real Qt HTTPS loopback updates; temporary trust/profile, no installer run."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'

from datetime import datetime,timedelta,timezone
import copy
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID,NameOID
from PySide6.QtCore import QEventLoop,QTimer
from PySide6.QtNetwork import QNetworkAccessManager,QNetworkRequest,QSslCertificate
from PySide6.QtWidgets import QApplication
import update_client as subject


app=QApplication.instance() or QApplication([])
def wait_until(predicate,timeout=4000):
    loop=QEventLoop();timer=QTimer();timer.setInterval(3);deadline=QTimer();deadline.setSingleShot(True)
    timer.timeout.connect(lambda:loop.quit() if predicate() else None);deadline.timeout.connect(loop.quit)
    timer.start();deadline.start(timeout);loop.exec();timer.stop();deadline.stop()
    assert predicate(),'HTTPS update fixture timed out'


class UpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary=tempfile.TemporaryDirectory(prefix='flickdock-update-v170-')
        cls.root=Path(cls.temporary.name)
        cls.routes={};cls.requests=[]
        ca_key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        ca_name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'FlickDock temporary update test CA')])
        now=datetime.now(timezone.utc)
        authority=(x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=1))
            .not_valid_after(now+timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True)
            .add_extension(x509.KeyUsage(digital_signature=False,content_commitment=False,key_encipherment=False,
                data_encipherment=False,key_agreement=False,key_cert_sign=True,crl_sign=True,
                encipher_only=False,decipher_only=False),critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),critical=False)
            .sign(ca_key,hashes.SHA256()))
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'FlickDock local update test server')])
        certificate=(x509.CertificateBuilder().subject_name(name).issuer_name(ca_name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=1))
            .not_valid_after(now+timedelta(days=1)).add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True,content_commitment=False,key_encipherment=True,
                data_encipherment=False,key_agreement=False,key_cert_sign=False,crl_sign=False,
                encipher_only=False,decipher_only=False),critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),critical=False)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),critical=False)
            .sign(ca_key,hashes.SHA256()))
        cert=cls.root/'fixture.crt';private=cls.root/'fixture.key'
        # Trust only this disposable root in the injected network manager. The
        # server presents a separate server-auth leaf, as real HTTPS does.
        cls.cert_bytes=authority.public_bytes(serialization.Encoding.PEM)
        cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        private.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                cls.requests.append((self.path,dict(self.headers)))
                route=cls.routes.get(self.path,dict(status=404,body=b'not found'))
                if route.get('delay'):time.sleep(route['delay'])
                body=route.get('body',b'');total=len(body);start=0;status=route.get('status',200)
                if route.get('ranges') and self.headers.get('Range') and status==200:
                    start=int(self.headers['Range'].split('=')[1].split('-')[0]);body=body[start:];status=206
                self.send_response(status)
                self.send_header('Content-Type',route.get('type','application/octet-stream'))
                if route.get('length',True):self.send_header('Content-Length',str(route.get('declared',len(body))))
                if status==206:self.send_header('Content-Range',f'bytes {start+(1 if route.get("bad_range") else 0)}-{total-1}/{total}')
                if route.get('location'):self.send_header('Location',route['location'])
                self.end_headers()
                cut=route.get('drop_once') if not route.get('dropped') else None
                try:
                    for offset in range(0,len(body),16384):
                        self.wfile.write(body[offset:offset+16384]);self.wfile.flush()
                        if cut and offset+16384>=cut:
                            route['dropped']=True;self.close_connection=True;return
                        if route.get('chunk_delay'):time.sleep(route['chunk_delay'])
                except (OSError,ssl.SSLError):pass
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);cls.server.daemon_threads=True
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,private)
        cls.server.socket=context.wrap_socket(cls.server.socket,server_side=True)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.origin=f'https://127.0.0.1:{cls.server.server_port}'
        class TrustedNetwork(QNetworkAccessManager):
            def createRequest(self,operation,request,outgoing=None):
                request=QNetworkRequest(request);configuration=request.sslConfiguration()
                configuration.setCaCertificates(configuration.caCertificates()+[QSslCertificate(cls.cert_bytes)])
                request.setSslConfiguration(configuration)
                return super().createRequest(operation,request,outgoing)
        cls.Network=TrustedNetwork

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(2);cls.temporary.cleanup()

    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(prefix='case-',dir=self.root)
        self.addCleanup(self.folder.cleanup)
        self.payload=b'MZ'+bytes(range(256))*4096
        self.manifest=dict(schema_version=1,version='1.10.0',published_at='2026-10-23T04:00:00Z',notes='修复与改进',
            installer=dict(url=self.origin+'/setup.exe',size=len(self.payload),sha256=hashlib.sha256(self.payload).hexdigest(),filename='FlickDock-1.10.0-setup.exe'))
        self.routes['/manifest']=dict(type='application/json',body=json.dumps(self.manifest).encode('utf8'))
        self.routes['/setup.exe']=dict(body=self.payload,chunk_delay=.001)
        self.clients=[]

    def tearDown(self):
        for client in self.clients:client.close()
        app.processEvents()

    def client(self,endpoint='/manifest',current='1.9.9',timeout=1000):
        network=self.Network()
        client=subject.UpdateClient(current,Path(self.folder.name)/'updates',endpoint=self.origin+endpoint,
                                    allowed_hosts={'127.0.0.1'},network=network,timeout_ms=timeout)
        client._test_network=network
        self.clients.append(client)
        state={name:[] for name in ('checking','update_available','up_to_date','failed','progress','verified_file','cancelled')}
        for name in state:getattr(client,name).connect(lambda *args,name=name:state[name].append(args))
        return client,state

    def test_real_https_check_stream_verify_and_numeric_version(self):
        self.assertGreater(subject.compare_versions('1.10.0','1.9.99'),0)
        client,state=self.client();beats=[];timer=QTimer();timer.setInterval(5);timer.timeout.connect(lambda:beats.append(True));timer.start()
        self.assertTrue(client.check(manual=True));wait_until(lambda:state['update_available'] or state['failed'])
        self.assertFalse(state['failed'],state['failed'])
        self.assertEqual(state['update_available'][0][1],True)
        self.assertEqual(client.available_manifest['version'],'1.10.0')
        self.assertTrue(client.download())
        wait_until(lambda:state['verified_file'] or state['failed']);timer.stop()
        self.assertFalse(state['failed'],state['failed'])
        target,manifest,manual=state['verified_file'][0]
        self.assertEqual(Path(target),Path(self.folder.name)/'updates'/'1.10.0'/'setup.exe')
        self.assertEqual(Path(target).read_bytes(),self.payload)
        self.assertEqual(manual,True);self.assertEqual(manifest['installer']['sha256'],hashlib.sha256(self.payload).hexdigest())
        self.assertGreater(len(state['progress']),2);self.assertGreater(len(beats),2)
        self.assertFalse(list(Path(self.folder.name).rglob('*.part')))
        self.assertFalse(any('Authorization' in headers for _,headers in self.requests))

    def test_official_check_retries_one_mirror_and_a_new_check_starts_again(self):
        self.routes['/primary/latest.json']=dict(status=503,body=b'unavailable')
        self.routes['/mirror/latest.json']=self.routes['/manifest']
        client,state=self.client('/primary/latest.json')
        client.allowed_hosts=client.allowed_hosts|{'flickdock-site.roger000416.workers.dev'}
        with patch.object(subject,'MAC_MANIFEST_BASE',self.origin+'/primary/'),patch.object(subject,'WORKER_MANIFEST_BASE',self.origin+'/mirror/'):
            client.check();wait_until(lambda:state['update_available'] or state['failed'])
            self.assertFalse(state['failed'],state['failed']);self.assertTrue(state['update_available'])
            self.routes['/mirror/latest.json']=dict(status=503,body=b'unavailable')
            self.requests.clear();client.check();wait_until(lambda:state['failed'])
            self.assertEqual([path for path,_ in self.requests],['/primary/latest.json','/mirror/latest.json'])
            self.assertFalse(client.busy)

    def test_invalid_manifest_and_failed_download_do_not_use_mirror(self):
        self.routes['/primary/latest.json']=dict(type='application/json',body=b'{}')
        client,state=self.client('/primary/latest.json')
        client.allowed_hosts=client.allowed_hosts|{'flickdock-site.roger000416.workers.dev'}
        with patch.object(subject,'MAC_MANIFEST_BASE',self.origin+'/primary/'),patch.object(subject,'WORKER_MANIFEST_BASE',self.origin+'/mirror/'):
            self.requests.clear();client.check();wait_until(lambda:state['failed'])
            self.assertEqual([path for path,_ in self.requests],['/primary/latest.json'])
            self.routes['/setup.exe']=dict(status=503,body=b'unavailable')
            self.requests.clear();state['failed'].clear();client.download(self.manifest)
            client._download_task.max_retries=0
            wait_until(lambda:state['failed'])
            self.assertEqual([path for path,_ in self.requests],['/setup.exe'])

    def test_real_https_mac_dmg_download_checks_trailer_and_hash(self):
        payload=b'FlickDock image'+bytes(65536)+b'koly'+bytes(508)
        candidate=dict(schema_version=1,version='1.8.5',published_at='2026-10-26T00:00:00Z',
            notes='Mac DMG',platform='darwin',architecture='arm64',
            installer=dict(url=self.origin+'/update.dmg',size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),filename='FlickDock-1.8.5-macOS-arm64.dmg'))
        self.routes['/update.dmg']=dict(body=payload)
        network=self.Network()
        client=subject.UpdateClient('1.8.4',Path(self.folder.name)/'mac-updates',
            allowed_hosts={'127.0.0.1'},network=network,platform='darwin',machine='arm64')
        client._test_network=network;self.clients.append(client)
        verified=[];failed=[]
        client.verified_file.connect(lambda *args:verified.append(args))
        client.failed.connect(lambda *args:failed.append(args))
        self.assertTrue(client.download(candidate))
        wait_until(lambda:verified or failed)
        self.assertFalse(failed,failed)
        self.assertEqual(Path(verified[0][0]),Path(self.folder.name)/'mac-updates/1.8.5/update.dmg')
        self.assertEqual(Path(verified[0][0]).read_bytes(),payload)

    def test_no_update_and_failure_preserve_manual_intent(self):
        client,state=self.client(current='1.10.0');client.check(manual=False)
        wait_until(lambda:state['up_to_date'] or state['failed']);self.assertEqual(state['up_to_date'],[(False,)])
        self.assertIsNone(client.available_manifest)
        self.routes['/login']=dict(type='text/html',body=b'<html>please log in</html>')
        client,state=self.client('/login');client.check(manual=False)
        wait_until(lambda:state['failed']);self.assertEqual(state['failed'][0][1],False)
        self.assertFalse(state['update_available'])
        self.routes['/403']=dict(status=403,type='text/html',body=b'login')
        client,state=self.client('/403');client.check(manual=True)
        wait_until(lambda:state['failed']);self.assertIn('403',state['failed'][0][0]);self.assertTrue(state['failed'][0][1])

    def test_redirects_are_checked_on_every_hop(self):
        self.routes['/redirect']=dict(status=302,location='/manifest',type='text/html',body=b'<html>redirect</html>')
        client,state=self.client('/redirect');client.check();wait_until(lambda:state['update_available'] or state['failed'])
        self.assertTrue(state['update_available'],state['failed'])
        for path,target in (('/http','http://127.0.0.1/manifest'),('/other','https://untrusted.invalid/manifest'),('/loop','/loop')):
            with self.subTest(path=path):
                self.routes[path]=dict(status=302,location=target,body=b'')
                client,state=self.client(path);client.check();wait_until(lambda:state['failed'])
                self.assertFalse(state['update_available'])
        self.assertFalse(any(path.startswith('http:') for path,_ in self.requests))

    def test_bounded_manifest_timeout_and_invalid_fields(self):
        self.routes['/large']=dict(body=b'x'*(subject.MAX_MANIFEST_BYTES+1),length=False)
        client,state=self.client('/large');client.check();wait_until(lambda:state['failed']);self.assertFalse(state['update_available'])
        self.routes['/slow']=dict(delay=.3,body=b'{}')
        client,state=self.client('/slow',timeout=50);client.check();wait_until(lambda:state['failed'],1000)
        self.assertIn('超时',state['failed'][0][0])
        for path,value in (('schema_version',True),('version','01.10.0'),('published_at','2026-10-23'),('notes',None)):
            bad=copy.deepcopy(self.manifest);bad[path]=value
            with self.subTest(field=path),self.assertRaises(ValueError):subject.validate_manifest(bad,{'127.0.0.1'})
        for path,value in (('url','http://gitee.com/setup.exe'),('size',True),('size',0),('sha256','X'*64),('filename','../setup.exe')):
            bad=copy.deepcopy(self.manifest);bad['installer'][path]=value
            with self.subTest(field=path),self.assertRaises(ValueError):subject.validate_manifest(bad,{'127.0.0.1'})

    def test_size_hash_html_and_non_executable_never_publish(self):
        cases=(('hash',self.payload[:-1]+b'x',len(self.payload),hashlib.sha256(self.payload).hexdigest()),
               ('size',self.payload,len(self.payload)+1,hashlib.sha256(self.payload).hexdigest()),
               ('html',b'<html>login</html>',18,hashlib.sha256(b'<html>login</html>').hexdigest()),
               ('nonpe',b'ZZnot executable',16,hashlib.sha256(b'ZZnot executable').hexdigest()))
        for label,body,size,digest in cases:
            with self.subTest(label=label):
                self.routes['/'+label]=dict(body=body)
                candidate=copy.deepcopy(self.manifest);candidate['installer'].update(url=self.origin+'/'+label,size=size,sha256=digest)
                client,state=self.client();client.download(candidate)
                wait_until(lambda:state['failed'] or state['verified_file'])
                self.assertTrue(state['failed']);self.assertFalse(state['verified_file'])
                self.assertFalse(list(Path(self.folder.name).rglob('*.part')))
                self.assertFalse(list(Path(self.folder.name).rglob('setup.exe')))

    def test_cancel_close_and_downgrade_reject_late_results(self):
        client,state=self.client()
        client.progress.connect(lambda done,total:client.cancel() if done>0 else None)
        client.download(self.manifest);wait_until(lambda:state['cancelled'] or state['failed'])
        self.assertEqual(state['cancelled'],[(True,)])
        self.assertFalse(state['verified_file']);self.assertFalse(list(Path(self.folder.name).rglob('*.part')))
        for version in ('1.9.9','1.0.0'):
            bad=copy.deepcopy(self.manifest);bad['version']=version
            client,state=self.client();before=len(self.requests);client.download(bad)
            self.assertTrue(state['failed']);self.assertEqual(len(self.requests),before)
        self.routes['/closing']=dict(delay=.2,type='application/json',body=json.dumps(self.manifest).encode())
        client,state=self.client('/closing');client.check();client.close()
        loop=QEventLoop();QTimer.singleShot(300,loop.quit);loop.exec()
        self.assertFalse(state['update_available'] or state['failed'] or state['cancelled'])
        self.assertFalse(client.busy)

    def test_download_redirect_and_manual_check_coalescing(self):
        client,state=self.client();client.check(manual=False);client.check(manual=True)
        wait_until(lambda:state['update_available'] or state['failed'])
        self.assertFalse(state['failed']);self.assertEqual(state['update_available'][0][1],True)
        self.routes['/download-redirect']=dict(status=302,location='/setup.exe',type='text/html',body=b'<html>redirect</html>')
        candidate=copy.deepcopy(self.manifest);candidate['installer']['url']=self.origin+'/download-redirect'
        client.download(candidate);wait_until(lambda:state['verified_file'] or state['failed'])
        self.assertFalse(state['failed']);self.assertEqual(Path(state['verified_file'][0][0]).read_bytes(),self.payload)
        Path(state['verified_file'][0][0]).unlink()
        self.routes['/download-blocked']=dict(status=302,location='http://127.0.0.1/setup.exe',body=b'')
        candidate['installer']['url']=self.origin+'/download-blocked'
        client,state=self.client();client.download(candidate);wait_until(lambda:state['failed'])
        self.assertFalse(state['verified_file']);self.assertFalse(list(Path(self.folder.name).rglob('*.part')))
        self.assertFalse(list(Path(self.folder.name).rglob('setup.exe')))


if __name__=='__main__':unittest.main(verbosity=2)
