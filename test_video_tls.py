"""Use the production downloader against real verified TLS, without OS trust changes."""
from datetime import datetime,timedelta,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import ipaddress
import hashlib,io,time,urllib.error
from pathlib import Path
import ssl,tempfile,threading,unittest
from unittest.mock import patch
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import certifi
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import CertificateVerifyError
from video_worker import downloader


class VideoTlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='flickdock-video-tls-');root=Path(cls.temp.name)
        key=ec.generate_private_key(ec.SECP256R1());name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Disposable video CA')])
        now=datetime.now(timezone.utc)
        ca=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True,path_length=0),True).sign(key,hashes.SHA256()))
        leaf_key=ec.generate_private_key(ec.SECP256R1())
        leaf=(x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Video fixture')]))
            .issuer_name(name).public_key(leaf_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False,path_length=None),True)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),False).sign(key,hashes.SHA256()))
        cls.ca=root/'root.pem';cls.ca.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
        cert=root/'server.pem';cert.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
        private=root/'key.pem';private.write_bytes(leaf_key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                self.send_response(200);self.send_header('Content-Length','8');self.end_headers();self.wfile.write(b'verified')
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);cls.server.daemon_threads=True
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,private)
        cls.server.socket=context.wrap_socket(cls.server.socket,server_side=True)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.url=f'https://127.0.0.1:{cls.server.server_port}/'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(3);cls.temp.cleanup()

    def test_bundled_ca_is_loaded_even_without_python_system_roots(self):
        with patch('video_worker.public_request'),patch('urllib.request.getproxies',return_value={}),patch.object(certifi,'where',return_value=str(self.ca)):
            with downloader({'action':'extract'},lambda _:None) as ydl:
                with ydl.urlopen(Request(self.url)) as reply:self.assertEqual(reply.read(),b'verified')

    def test_untrusted_certificate_is_rejected(self):
        with patch('video_worker.public_request'),patch('urllib.request.getproxies',return_value={}):
            with downloader({'action':'extract'},lambda _:None) as ydl:
                with self.assertRaises(CertificateVerifyError):ydl.urlopen(Request(self.url))

    def test_component_uses_verified_ca_on_a_cold_download(self):
        from video_runtime import _download_archive
        data=b'verified';record=dict(url=self.url,archive_bytes=len(data),archive_sha256=hashlib.sha256(data).hexdigest())
        with patch.object(certifi,'where',return_value=str(self.ca)),patch('urllib.request.getproxies',return_value={}):
            output=io.BytesIO();progress=[]
            _download_archive(record,output,lambda **value:progress.append(value),time.monotonic()+10)
            self.assertEqual(output.getvalue(),data);self.assertEqual(progress[0]['phase'],'preparing')
        with patch('urllib.request.getproxies',return_value={}):
            with self.assertRaises(OSError):_download_archive(record,io.BytesIO(),lambda **_:None,time.monotonic()+10)

    def test_component_falls_back_and_discards_failed_partial_bytes(self):
        from video_runtime import _download_archive,_archive_urls
        payload=b'complete';record=dict(url='https://files.pythonhosted.org/packages/fixture',archive_bytes=len(payload),archive_sha256=hashlib.sha256(payload).hexdigest())
        urls=_archive_urls(record);calls=[]
        class Reply(io.BytesIO):
            def __init__(self,url,body):super().__init__(body);self.url=url
            def geturl(self):return self.url
        def request(req,**kwargs):
            calls.append(req.full_url)
            return Reply(req.full_url,b'bad' if len(calls)==1 else payload)
        with patch('urllib.request.urlopen',side_effect=request):
            output=io.BytesIO();_download_archive(record,output,lambda **_:None,time.monotonic()+10)
        self.assertEqual(calls,list(urls));self.assertEqual(output.getvalue(),payload)


if __name__=='__main__':unittest.main()
