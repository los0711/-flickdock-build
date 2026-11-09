"""Bounded byte stream over a native, reliable ordered WebRTC DataChannel.

Only IPC carries raw bytes here. Signaling uses the owner's authenticated
room; no files, credentials or SDP are written to logs or command arguments.
"""
from collections import deque
import json,os,socket,struct,subprocess,sys,threading,time
from pathlib import Path

class WebRTCStream:
    def __init__(self,on_signal,*,node=None,root=None):
        root=Path(root or getattr(sys,'_MEIPASS',Path(__file__).parent))/'p2p'
        executable=Path(node) if node else root/'runtime'/('node.exe' if sys.platform=='win32' else 'node')
        if not executable.is_file():raise RuntimeError('远程传输组件缺失，请重新安装 FlickDock。')
        self._process=subprocess.Popen([str(executable),str(root/'bridge.mjs')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,cwd=root,creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0,
            env={key:value for key,value in os.environ.items() if key not in ('NODE_OPTIONS','NODE_PATH','NODE_EXTRA_CA_CERTS')})
        self._condition=threading.Condition();self._data=deque();self._size=0
        self._controls=deque();self._writes=deque();self._write_bytes=0;self._control_bytes=0
        self._closed=False;self._timeout=1.;self.opened=threading.Event();self.route='';self.on_signal=on_signal
        self._traffic={'sent':0,'received':0};self._stats_generation=0;self.last_activity=time.monotonic()
        self._sent_position=0;self._payload_spans=deque();self._payload_delivered=0
        self._delivered_position=0;self._payload_offset=0;self._payload_origin=0
        threading.Thread(target=self._reader,daemon=True,name='FlickDock-WebRTC-pipe').start()
        threading.Thread(target=self._writer,daemon=True,name='FlickDock-WebRTC-writer').start()

    @property
    def closed(self):return self._closed
    def control(self,value):self._write(b'C' if value.get('type')=='close' else b'J',json.dumps(value,separators=(',',':')).encode())
    def _write(self,kind,data):
        with self._condition:
            deadline=time.monotonic()+max(30,self._timeout)
            if kind==b'J':
                if self._closed:raise ConnectionError('远程连接已中断。')
                if len(self._controls)>=128 or self._control_bytes+len(data)>1024*1024:raise ConnectionError('连接信令积压，请重试。')
                self._controls.append(data);self._control_bytes+=len(data)
            else:
                while self._write_bytes+len(data)>4*1024*1024 and not self._closed:
                    remaining=deadline-time.monotonic()
                    if remaining<=0:raise socket.timeout('对方设备长时间未接收数据。')
                    self._condition.wait(remaining)
                if self._closed:raise ConnectionError('远程连接已中断。')
                self._writes.append((kind,data));self._write_bytes+=len(data)
                if kind==b'B':self._sent_position+=len(data)
            self._condition.notify_all()
    def _writer(self):
        try:
            while True:
                with self._condition:
                    while not self._controls and not self._writes and not self._closed:self._condition.wait()
                    if self._closed:return
                    if self._controls:
                        kind=b'J';data=self._controls.popleft();self._control_bytes-=len(data)
                    else:
                        kind,data=self._writes.popleft();self._write_bytes-=len(data)
                        if kind==b'C':kind=b'J'
                    self._condition.notify_all()
                self._process.stdin.write(struct.pack('!I',len(data)+1)+kind+data);self._process.stdin.flush()
        except (OSError,ValueError):
            with self._condition:self._closed=True;self._condition.notify_all()
            self.on_signal({'type':'closed'})

    def _read_pipe(self,size):
        result=bytearray()
        while len(result)<size:
            chunk=self._process.stdout.read(size-len(result))
            if not chunk:raise EOFError()
            result.extend(chunk)
        return bytes(result)

    def _reader(self):
        try:
            while not self._closed:
                size=struct.unpack('!I',self._read_pipe(4))[0]
                if not 1<=size<=131073:raise ValueError('Invalid bridge frame')
                packet=self._read_pipe(size)
                if packet[:1]==b'B':
                    with self._condition:
                        while self._size>=8*1024*1024 and not self._closed:self._condition.wait()
                        if self._closed:return
                        self._data.append(packet[1:]);self._size+=len(packet)-1;self._condition.notify_all()
                elif packet[:1]==b'J':
                    value=json.loads(packet[1:])
                    if value.get('type')=='open':self.route=value['route'];self.opened.set()
                    if value.get('type')=='stats':
                        with self._condition:
                            for key in ('sent','received'):
                                count=value.get(key)
                                if type(count) is int and count>=0:
                                    if count>self._traffic[key]:self.last_activity=time.monotonic()
                                    self._traffic[key]=max(self._traffic[key],count)
                            self._stats_generation+=1;self._condition.notify_all()
                            delivered=value.get('delivered')
                            if type(delivered) is int and delivered>=self._delivered_position:
                                self._delivered_position=delivered
                                self._advance_payload()
                    self.on_signal(value)
                else:raise ValueError('Invalid bridge type')
        except (OSError,EOFError,ValueError):pass
        finally:
            with self._condition:self._closed=True;self._condition.notify_all()
            self.on_signal({'type':'closed'})

    def settimeout(self,value):self._timeout=value
    def traffic(self,*,refresh=False):
        with self._condition:
            if refresh and not self._closed:
                generation=self._stats_generation
                self.control({'type':'stats'})
                self._condition.wait_for(lambda:self._stats_generation!=generation or self._closed,timeout=.5)
            return dict(self._traffic)
    def sendall(self,data):
        view=memoryview(data)
        for start in range(0,len(view),65536):self._write(b'B',view[start:start+65536].tobytes())
    def set_payload_offset(self,offset):
        with self._condition:
            self._payload_offset=offset;self._payload_origin=self._payload_delivered
    def _advance_payload(self):
        while self._payload_spans:
            start,end=self._payload_spans[0]
            count=max(0,min(end,self._delivered_position)-start)
            self._payload_delivered+=count
            if start+count==end:self._payload_spans.popleft()
            else:
                self._payload_spans[0]=(start+count,end);break
    def send_payload(self,data):
        # Mark only file bytes, excluding manifests/checksums. A peer credit
        # acknowledges arrival across the network, unlike local queue writes.
        with self._condition:
            start=self._sent_position;end=start+len(data)
            if self._payload_spans and self._payload_spans[-1][1]==start:
                self._payload_spans[-1]=(self._payload_spans[-1][0],end)
            elif end>start:self._payload_spans.append((start,end))
            self.sendall(data);self._advance_payload()
    def payload_progress(self):
        with self._condition:return self._payload_offset+self._payload_delivered-self._payload_origin
    def recv(self,size):
        target=bytearray(size);count=self.recv_into(target);return bytes(target[:count])
    def recv_into(self,target):
        if not len(target):return 0
        with self._condition:
            deadline=time.monotonic()+self._timeout
            while not self._data and not self._closed:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise socket.timeout()
                self._condition.wait(remaining)
            if not self._data:return 0
            data=self._data.popleft();count=min(len(target),len(data));target[:count]=data[:count]
            if count<len(data):self._data.appendleft(data[count:])
            self._size-=count;self._condition.notify_all();return count
    def shutdown(self,*_):self.close(abort=True)
    def close(self,*,abort=False):
        if not abort and not self._closed:
            try:self.control({'type':'close'})
            except OSError:pass
        if abort and self._process.poll() is None:self._process.terminate()
        try:self._process.wait(timeout=4)
        except subprocess.TimeoutExpired:self._process.kill();self._process.wait(timeout=3)
        with self._condition:self._closed=True;self._condition.notify_all()
        for stream in (self._process.stdin,self._process.stdout):
            try:stream.close()
            except OSError:pass
