"""Optional TLS bulk path bound to an authenticated, nearby WebRTC peer.

Only an already connected DTLS channel can advertise the one-shot TLS pin and
nonce. Global addresses and arbitrary destinations are never dialed. Older or
unreachable peers keep their existing reliable DataChannel byte stream.
"""
from collections import deque
import hashlib,ipaddress,json,os,secrets,socket,ssl,struct,tempfile,threading,time
from pathlib import Path

_NETWORKS=tuple(ipaddress.ip_network(value) for value in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','127.0.0.0/8','169.254.0.0/16'))

def _nearby(address):
    try:
        parsed=ipaddress.ip_address(address)
        return parsed.version==4 and any(parsed in network for network in _NETWORKS)
    except ValueError:return False

def _send(stream,value):
    body=json.dumps(value,separators=(',',':')).encode('ascii')
    if len(body)>1024:raise ValueError('Invalid nearby handshake')
    stream.sendall(struct.pack('!I',len(body))+body)

def _remaining(raw,deadline,cancel):
    if raw.closed or cancel.is_set():raise InterruptedError('已取消传输。')
    remaining=deadline-time.monotonic()
    if remaining<=0:raise TimeoutError('Nearby handshake expired')
    return remaining

def _read(stream,raw,deadline,cancel):
    def exact(count):
        output=bytearray()
        while len(output)<count:
            stream.settimeout(min(.1,_remaining(raw,deadline,cancel)))
            try:part=stream.recv(count-len(output))
            except socket.timeout:continue
            if not part:raise EOFError()
            output.extend(part)
        return output
    size=struct.unpack('!I',exact(4))[0]
    if not 1<=size<=1024:raise ValueError('Invalid nearby handshake')
    return json.loads(exact(size))

def _message(raw,deadline,cancel):
    with raw._condition:
        while not raw._nearby_messages:
            if raw.closed or cancel.is_set():raise InterruptedError('已取消传输。')
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('Nearby handshake expired')
            raw._condition.wait(min(.1,remaining))
        _remaining(raw,deadline,cancel)
        return raw._nearby_messages.popleft()

class NearbyStream:
    """Socket interface with peer-read progress, retaining WebRTC cancellation."""
    def __init__(self,raw,stream):
        self.raw,self.stream=raw,stream;self._sent=self._received=0
        self._spans=deque();self._payload=0;self._offset=self._origin=0
        self._acked=0;self._last_ack=0.;self._lock=threading.RLock()
        raw.own_socket(stream)
    @property
    def route(self):return 'direct'
    def settimeout(self,value):self.stream.settimeout(value)
    def _credit(self,force=False):
        now=time.monotonic()
        if self._received>self._acked and (force or self._received-self._acked>=1024*1024 or now-self._last_ack>=.05):
            self.raw.control(dict(type='nearby',data=dict(state='credit',received=str(self._received))))
            self._acked=self._received;self._last_ack=now
    def sendall(self,data):
        self._credit(force=True)
        with self._lock:self._sent+=len(data)
        self.stream.sendall(data)
    def recv(self,size):
        data=self.stream.recv(size);self._received+=len(data);self._credit();return data
    def recv_into(self,target):
        count=self.stream.recv_into(target);self._received+=count;self._credit();return count
    def set_payload_offset(self,value):
        with self._lock:self._offset=value;self._origin=self._payload
    def send_payload(self,data):
        with self._lock:
            start=self._sent;end=start+len(data)
            if self._spans and self._spans[-1][1]==start:self._spans[-1]=(self._spans[-1][0],end)
            elif end>start:self._spans.append((start,end))
        self.sendall(data)
    def payload_progress(self):
        with self._lock:
            delivered=min(self._sent,self.raw._nearby_delivered)
            while self._spans:
                start,end=self._spans[0];count=max(0,min(end,delivered)-start);self._payload+=count
                if start+count==end:self._spans.popleft()
                else:self._spans[0]=(start+count,end);break
            return self._offset+self._payload-self._origin
    def traffic(self,*,refresh=False):
        value=self.raw.traffic(refresh=refresh);value['sent']+=self._sent;value['received']+=self._received;return value
    def shutdown(self,*_):self.raw.shutdown()
    def close(self):self.raw.close()

def try_nearby(raw,role,cancel):
    context=getattr(raw,'_nearby_context',{})
    if (not context.get('capable') or raw.route!='direct' or
            not all(_nearby(context.get(key,'')) for key in ('local','remote'))):return raw
    from lan_transfer import _ephemeral_certificate,_tune_stream_socket
    stream=listener=None;committed=False;nonce='';deadline=time.monotonic()+2.5
    try:
        if role=='receive':
            cert,key,pin=_ephemeral_certificate()
            tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.minimum_version=ssl.TLSVersion.TLSv1_2
            with tempfile.TemporaryDirectory(prefix='flickdock-nearby-tls-') as directory:
                certificate=Path(directory)/'session.pem';private=Path(directory)/'key.pem'
                certificate.write_bytes(cert)
                descriptor=os.open(str(private),os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                with os.fdopen(descriptor,'wb') as output:output.write(key)
                tls.load_cert_chain(str(certificate),str(private))
            listener=socket.socket(socket.AF_INET,socket.SOCK_STREAM);raw.own_socket(listener)
            listener.bind((context['local'],0));listener.listen(2);listener.settimeout(.1)
            nonce=secrets.token_hex(32)
            raw.control(dict(type='nearby',data=dict(state='offer',port=listener.getsockname()[1],pin=pin,nonce=nonce)))
            while True:
                if cancel.is_set() or raw.closed:raise InterruptedError('已取消传输。')
                if time.monotonic()>=deadline:raise TimeoutError()
                try:stream,address=listener.accept()
                except socket.timeout:continue
                if address[0]==context['remote']:break
                stream.close();stream=None
            raw.own_socket(stream);_tune_stream_socket(stream);stream.settimeout(max(.1,deadline-time.monotonic()))
            stream=tls.wrap_socket(stream,server_side=True);raw.own_socket(stream)
            if not secrets.compare_digest(str(_read(stream,raw,deadline,cancel).get('nonce','')),nonce):raise ValueError('Invalid nearby identity')
            stream.settimeout(_remaining(raw,deadline,cancel))
            _send(stream,dict(state='ready'))
            message=_message(raw,deadline,cancel)
            if message!=dict(state='commit',nonce=nonce):raise ValueError('Nearby connection declined')
            committed=True
            raw.control(dict(type='nearby',data=dict(state='committed',nonce=nonce)))
        else:
            offer=_message(raw,deadline,cancel)
            if (offer.get('state')!='offer' or type(offer.get('port')) is not int or not 1<=offer['port']<=65535 or
                    any(not isinstance(offer.get(key),str) or len(offer[key])!=64 or any(c not in '0123456789abcdef' for c in offer[key]) for key in ('pin','nonce'))):
                raise ValueError('Invalid nearby offer')
            nonce=offer['nonce']
            stream=socket.socket(socket.AF_INET,socket.SOCK_STREAM);raw.own_socket(stream)
            _tune_stream_socket(stream);stream.settimeout(max(.1,deadline-time.monotonic()))
            stream.connect((context['remote'],offer['port']))
            tls=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);tls.check_hostname=False;tls.verify_mode=ssl.CERT_NONE;tls.minimum_version=ssl.TLSVersion.TLSv1_2
            stream=tls.wrap_socket(stream,server_hostname='flickdock-nearby');raw.own_socket(stream)
            if not secrets.compare_digest(hashlib.sha256(stream.getpeercert(binary_form=True)).hexdigest(),offer['pin']):raise ValueError('Invalid nearby certificate')
            stream.settimeout(_remaining(raw,deadline,cancel));_send(stream,dict(nonce=nonce))
            if _read(stream,raw,deadline,cancel)!=dict(state='ready'):raise ValueError('Nearby peer not ready')
            _remaining(raw,deadline,cancel)
            committed=True
            raw.control(dict(type='nearby',data=dict(state='commit',nonce=nonce)))
            if _message(raw,deadline,cancel)!=dict(state='committed',nonce=nonce):raise ConnectionError('Nearby peer did not commit')
        stream.settimeout(1)
        return NearbyStream(raw,stream)
    except InterruptedError:raise
    except (OSError,EOFError,ValueError,RuntimeError):
        if stream:stream.close()
        if committed:raise ConnectionError('直连连接中断，正在尝试恢复。')
        if not raw.closed:
            try:raw.control(dict(type='nearby',data=dict(state='fallback',nonce=nonce)))
            except OSError:pass
        return raw
    finally:
        if listener:listener.close()
