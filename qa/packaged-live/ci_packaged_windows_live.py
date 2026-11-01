"""Hosted Windows only: NORMAL frozen entry, external input and actual screen pixels.

The EXE is never imported/patched and receives no --verify-* arguments. Local
workerd is a real service fixture behind a normally verified TLS gateway, NOT
the public Cloudflare deployment or a physical second device. All credentials,
keys, fixtures and system changes belong to the disposable runner.
"""
from __future__ import annotations
import argparse,base64,datetime,hashlib,http.client,json,os,queue,re,socket,ssl,struct,subprocess,sys,tempfile,threading,time,traceback
from pathlib import Path
from urllib.request import Request,urlopen

DOMAINS=('flickdock-cloud.roger000416.workers.dev','flickdock-site.roger000416.workers.dev')
_PROGRESS=None


class Progress:
    """Payload-free stages plus independent desktop diagnostics, never UIA."""
    def __init__(self,out):
        self.out=out;self.lock=threading.Lock();self.phase='startup';self.started=time.monotonic();self.stopped=threading.Event()
        self.path=out/'qa-phases.jsonl';self.shots=out/'phase-screen';self.shots.mkdir(exist_ok=True)
        self.thread=threading.Thread(target=self._heartbeat,daemon=True);self.thread.start()
    def record(self,event,phase=None,**details):
        with self.lock:
            if phase is not None:self.phase=phase;self.started=time.monotonic()
            row=dict(event=event,phase=self.phase,elapsed_s=round(time.monotonic()-self.started,3),mono=time.monotonic(),**details)
            with self.path.open('a',encoding='utf8') as stream:stream.write(json.dumps(row)+'\n')
        print('QA_STAGE '+json.dumps(row),flush=True)
    def _heartbeat(self):
        while not self.stopped.wait(10):self.record('heartbeat')
    def screenshot(self,label):
        require_hosted()
        errors=[]
        def capture():
            try:
                import mss
                from PIL import Image
                with mss.mss() as screen:
                    shot=screen.grab(screen.monitors[1]);Image.frombytes('RGB',shot.size,shot.rgb).save(self.shots/(label+'.png'),compress_level=1)
            except Exception as error:errors.append(type(error).__name__)
        task=threading.Thread(target=capture,daemon=True);task.start();task.join(3)
        if task.is_alive() or errors:self.record('screenshot_unavailable')
    def close(self):self.stopped.set();self.thread.join(1)


def phase(label,action,*,screens=False):
    if _PROGRESS is not None:
        _PROGRESS.record('before',label)
        if screens:_PROGRESS.screenshot(label+'-before')
    try:return action()
    finally:
        if _PROGRESS is not None:
            if screens:_PROGRESS.screenshot(label+'-after')
            _PROGRESS.record('after')

def require_hosted():
    if sys.platform!='win32' or os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('RUNNER_OS')!='Windows':
        raise RuntimeError('This script refuses to operate outside an ephemeral GitHub Windows runner')

def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def wait(action,*,seconds=30,label='condition'):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        value=action()
        if value:return value
        time.sleep(.015)
    raise AssertionError('Timed out: '+label)

def json_request(origin,method,path,data=None,token=None):
    headers={'Content-Type':'application/json'}
    if token:headers['Authorization']='Bearer '+token
    raw=None if data is None else json.dumps(data).encode()
    with urlopen(Request(origin+path,data=raw,headers=headers,method=method),timeout=20) as response:
        body=response.read();return json.loads(body) if body else None

def binary_request(origin,method,path,body,token,headers=None):
    request=Request(origin+path,data=body,method=method,headers={'Authorization':'Bearer '+token,**(headers or {})})
    with urlopen(request,timeout=20) as response:return json.loads(response.read())


def inline_clipboard(text):
    """Wire fixture only, independently encode the documented FDCLIP2 format."""
    raw=text.encode('utf8');body=b'FDCLIP2\0'+struct.pack('!I',len(raw))+raw
    if len(body)>32768:raise ValueError('Synthetic inline fixture too large')
    return dict(kind='text',body=base64.b64encode(body).decode('ascii'),size=len(body),sha256=hashlib.sha256(body).hexdigest())


def producer_corner(width,height,target,screen_width,screen_height):
    """Place only the disposable producer away from the real drop point."""
    candidates=((8,8),(screen_width-width-8,8),(8,screen_height-height-48),
                (screen_width-width-8,screen_height-height-48))
    safe=[(x,y) for x,y in candidates if x>=0 and y>=0 and
          not (x<=target[0]<x+width and y<=target[1]<y+height)]
    if not safe:raise RuntimeError('No unobscured drag destination on disposable display')
    return max(safe,key=lambda xy:(xy[0]+width/2-target[0])**2+(xy[1]+height/2-target[1])**2)


def retain_diagnostics(output,profile,cloud):
    """Failure-safe allowlist export; never copy a private profile or raw log."""
    status={};errors=[]
    def number(value):return type(value) in (int,float) and value==value and 0<=value<=2**53
    def code(value):return isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{0,90}',value)
    def read(path,limit):
        if path.is_symlink() or not path.is_file() or path.stat().st_size>limit:return None
        return path.read_text(encoding='utf8')
    if cloud is not None:
        try:
            raw=cloud.stats();safe=[]
            for row in raw.get('rows',[])[-4096:]:
                if not isinstance(row,dict):continue
                item={key:value for key,value in row.items() if key in ('index','started','status','first_byte','finished') and number(value)}
                if row.get('method') in ('GET','POST','PUT','DELETE','PATCH','HEAD','OPTIONS'):item['method']=row['method']
                path=str(row.get('path','')).split('?')[0]
                path=re.sub(r'[0-9a-f]{8}-[0-9a-f-]{27,}',':id',path,flags=re.I)
                item['path']=path if re.fullmatch(r'/(?:v1|api|updates|health)(?:/[A-Za-z0-9_:.-]{1,80})*',path) else 'UNRECOGNIZED_ROUTE'
                for key in ('held','cancelled'):
                    if type(row.get(key)) is bool:item[key]=row[key]
                if code(row.get('error')):item['error']=row['error']
                safe.append(item)
            saved=dict(rows=safe,total_rows=len(raw.get('rows',[])),held=raw.get('held') if type(raw.get('held')) is int else None)
            (output/'synthetic-gateway-stats.json').write_text(json.dumps(saved,indent=2));status['gateway_rows']=len(safe)
        except Exception as error:errors.append('gateway:'+type(error).__name__)
    if profile is not None:
        try:
            raw=read(profile.parent/'native-input-ownership.jsonl',1024*1024);safe=[]
            for line in (raw or '').splitlines()[-2048:]:
                row=json.loads(line)
                item={key:row[key] for key in ('expected_pid','foreground_pid') if number(row.get(key))}
                for key in ('producer','pointer_owner_verified','no_activate','style_unavailable'):
                    if type(row.get(key)) is bool:item[key]=row[key]
                if row.get('operation')=='mouse_target':item['operation']='mouse_target'
                safe.append(item)
            (output/'synthetic-input-ownership.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in safe));status['input_rows']=len(safe)
        except Exception as error:errors.append('input:'+type(error).__name__)
        try:
            raw=read(profile/'diagnostics/clipboard-writes.jsonl',2*1024*1024);safe=[]
            for line in (raw or '').splitlines()[-2048:]:
                try:row=json.loads(line)
                except ValueError:continue
                if not isinstance(row,dict):continue
                item={key:value for key,value in row.items() if key in ('module','function','trigger','origin','content_type','operation','result') and code(value)}
                if isinstance(row.get('time'),str) and re.fullmatch(r'[0-9T:+.Z-]{1,40}',row['time']):item['time']=row['time']
                if isinstance(row.get('fingerprint'),str) and re.fullmatch(r'[a-f0-9]{64}',row['fingerprint']):item['fingerprint']=row['fingerprint']
                if isinstance(row.get('task_id'),str) and re.fullmatch(r'[a-f0-9-]{36}',row['task_id']):item['task_id']=row['task_id']
                for key in ('pid','dropped'):
                    if number(row.get(key)):item[key]=row[key]
                if type(row.get('duplicate')) is bool:item['duplicate']=row['duplicate']
                safe.append(item)
            (output/'synthetic-clipboard-provenance.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in safe));status['audit_rows']=len(safe)
        except Exception as error:errors.append('audit:'+type(error).__name__)
        try:
            raw=read(profile/'network-diagnostics.json',65536);rows=json.loads(raw) if raw else [];safe=[]
            for row in rows[-64:] if isinstance(rows,list) else []:
                if not isinstance(row,dict):continue
                item={key:value for key,value in row.items() if key in ('component','phase','code','platform','version') and code(value)}
                for key in ('at','status'):
                    if number(row.get(key)):item[key]=row[key]
                detail=row.get('details',{});values={}
                if isinstance(detail,dict):
                    for key in ('received_size','expected_size','network_error','errno'):
                        if number(detail.get(key)):values[key]=detail[key]
                    for key in ('hash_matches','reply_finished','header_digest_valid'):
                        if type(detail.get(key)) is bool:values[key]=detail[key]
                    for key in ('phase','cause','location','response_length'):
                        if code(detail.get(key)):values[key]=detail[key]
                if values:item['details']=values
                safe.append(item)
            (output/'synthetic-network-diagnostics.json').write_text(json.dumps(safe,indent=2));status['network_rows']=len(safe)
        except Exception as error:errors.append('network:'+type(error).__name__)
    if errors:status['errors']=errors
    return status

class DeferredProfile:
    """Children and credential store close before Windows removes their files."""
    def __init__(self):self.directory=tempfile.TemporaryDirectory(prefix='flickdock-frozen-normal-ci-')
    def __enter__(self):return self.directory.name
    def __exit__(self,*_):pass
    def cleanup(self):self.directory.cleanup()


class ClientAreaAnimation:
    """Read/temporarily enable/restore a real OS preference on hosted CI only."""
    def __init__(self):self.initial=None;self.qa=None;self.restored=None
    def _spi(self):
        import ctypes
        from ctypes import wintypes
        function=ctypes.WinDLL('user32',use_last_error=True).SystemParametersInfoW
        function.argtypes=[wintypes.UINT,wintypes.UINT,ctypes.c_void_p,wintypes.UINT];function.restype=wintypes.BOOL
        return function
    def read(self):
        import ctypes
        value=ctypes.c_int()
        if not self._spi()(0x1042,0,ctypes.byref(value),0):raise OSError('Cannot read runner ClientAreaAnimation')
        return bool(value.value)
    def write(self,value):
        import ctypes
        # Microsoft SPI_SETCLIENTAREAANIMATION takes the BOOL as pvParam,
        # unlike GET which takes a pointer. Flags=0 avoids profile persistence.
        # https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-systemparametersinfow
        if not self._spi()(0x1043,0,ctypes.c_void_p(int(value)),0):raise OSError('Cannot set runner ClientAreaAnimation')
    def enable(self):
        require_hosted();self.initial=self.read()
        if not self.initial:self.write(True)
        self.qa=self.read()
        if not self.qa:raise AssertionError('Real runner ClientAreaAnimation remained disabled')
    def restore(self):
        if self.initial is None:return
        require_hosted();self.write(self.initial);self.restored=self.read()
        if self.restored!=self.initial:raise AssertionError('Runner ClientAreaAnimation did not restore')
    def snapshot(self):return dict(initial=self.initial,qa=self.qa,restored=self.restored,scope='actual OS preference; ephemeral Windows runner only')

def certificate(folder):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'FlickDock ephemeral LOCAL QA TLS')])
    now=datetime.datetime.now(datetime.timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=5))
        .not_valid_after(now+datetime.timedelta(hours=2))
        .add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name) for name in DOMAINS]),critical=False)
        .sign(key,hashes.SHA256()))
    key_path=folder/'local-key.pem';cert_path=folder/'local-cert.pem'
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_path,cert_path,cert.fingerprint(hashes.SHA1()).hex()

class LocalCloud:
    def __init__(self,root,folder,qa_root):
        self.root=root;self.folder=folder;self.qa_root=qa_root;self.processes=[];self.hosts=None;self.thumbprint=None;self.store=None
    def start(self):
        require_hosted()
        self.log=(self.folder/'local-service.log').open('wb')
        self.server=subprocess.Popen(['node',str(self.root/'cloud/test/qa-server.mjs')],cwd=self.root,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,creationflags=subprocess.CREATE_NO_WINDOW)
        self.processes.append(self.server)
        # qa-server prints exactly its ready origin only after migrations+health.
        lines=queue.Queue()
        def read_ready():
            for line in self.server.stdout:lines.put(line.decode(errors='replace').strip())
        threading.Thread(target=read_ready,daemon=True).start()
        def ready():
            if self.server.poll() is not None:raise RuntimeError('Local workerd exited; private log retained in temporary profile')
            try:line=lines.get_nowait()
            except queue.Empty:return None
            return line if re.fullmatch(r'http://127\.0\.0\.1:\d+',line) else None
        self.origin=wait(ready,seconds=90,label='actual local workerd startup')
        key,cert,self.thumbprint=certificate(self.folder)
        # CurrentUser trust import can open a confirmation UI and stall. This
        # administrator-owned disposable runner uses its machine Root store;
        # only this generated certificate is removed during finally cleanup.
        require_hosted()
        subprocess.run(['certutil','-f','-addstore','Root',str(cert)],check=True,capture_output=True,timeout=15)
        self.hosts=Path(os.environ['SystemRoot'])/'System32/drivers/etc/hosts'
        self.hosts_before=self.hosts.read_bytes()
        self.hosts_after=self.hosts_before+b'\r\n# FlickDock ephemeral local QA\r\n'+''.join('127.0.0.1 '+domain+'\r\n' for domain in DOMAINS).encode()
        self.hosts.write_bytes(self.hosts_after)
        subprocess.run(['ipconfig','/flushdns'],check=True,capture_output=True,timeout=10)
        config=dict(origin=self.origin,key=str(key),cert=str(cert),ready=str(self.folder/'gateway-ready.json'))
        config_file=self.folder/'gateway-config.json';config_file.write_text(json.dumps(config))
        proxy=subprocess.Popen(['node',str(self.qa_root/'gateway.mjs'),str(config_file)],stdout=self.log,stderr=self.log,
            creationflags=subprocess.CREATE_NO_WINDOW);self.processes.append(proxy)
        def gateway_ready():
            if proxy.poll() is not None:raise RuntimeError('Local TLS gateway exited')
            try:return json.loads(Path(config['ready']).read_text())
            except (OSError,ValueError):return None
        self.control=wait(gateway_ready,label='local TLS gateway')['control']
        for domain in DOMAINS:
            if socket.gethostbyname(domain)!='127.0.0.1':raise AssertionError('Cloud hostname did not resolve to local-only service')
            # This independent TLS verification must succeed. No ignoreSslErrors
            # or CERT_NONE fallback is allowed in this harness or application.
            with ssl.create_default_context().wrap_socket(socket.create_connection((domain,443),timeout=10),server_hostname=domain):pass
    def sign(self,name,login=False):
        value=json_request(self.origin,'POST','/v1/auth/challenge',dict(username=name,purpose='login' if login else 'register'))
        key=hashlib.scrypt(b'local-synthetic-password',salt=bytes.fromhex(value['salt']),n=16384,r=8,p=5,dklen=32,maxmem=32*1024*1024).hex()
        return json_request(self.origin,'POST','/v1/auth/'+('login' if login else 'register'),
            dict(username=name,challenge_id=value['challenge_id'],auth_key=key,device_name='Native QA peer' if login else 'Frozen Windows QA',platform='windows'))
    def seed_session(self,base):
        name='live-'+os.urandom(8).hex();session=self.sign(name);self.peer=self.sign(name,True)
        self.store=base
        private=self.folder/'synthetic-session-private.json';private.write_text(json.dumps(session))
        subprocess.run([sys.executable,str(Path(__file__).resolve()),'--credential-worker','save',str(self.root),str(base),str(private)],
            check=True,capture_output=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
        private.unlink()
        self.session=session
    def stats(self):return json_request(self.control,'GET','/stats')
    def command(self,path):
        with urlopen(self.control+path,timeout=5) as response:response.read()
    def publish_shelf(self):
        import uuid
        identity=str(uuid.uuid4());body=('Synthetic remote shelf object '+identity+'\n').encode();checksum=hashlib.sha256(body).hexdigest()
        result=json_request(self.origin,'POST','/v1/uploads',dict(item_id=identity,base_version=0,kind='text',name='Actual local peer item',
            mime_type='text/plain',size=len(body),sha256=checksum,fast_upload=True),self.peer['token'])
        binary_request(self.origin,'PUT',f'/v1/uploads/{result["upload_id"]}/parts/0',body,self.peer['token'],{'X-Content-SHA256':checksum})
        json_request(self.origin,'POST',f'/v1/uploads/{result["upload_id"]}/commit',{},self.peer['token'])
        return identity
    def publish_clipboard(self,text):
        import uuid
        previous=json_request(self.origin,'GET','/v1/clipboard',token=self.peer['token']).get('clipboard') or {}
        self.peer_sequence=getattr(self,'peer_sequence',0)+1
        intent=dict(id=str(uuid.uuid4()),protocol=2,sequence=self.peer_sequence,
                    copied_at=max(int(time.time()*1000),previous.get('copied_at',0)+1),inline=inline_clipboard(text))
        head=json_request(self.origin,'POST','/v1/clipboard',intent,self.peer['token'])['clipboard']
        if not head.get('ready') or head.get('id')!=intent['id']:raise AssertionError('Real peer inline clipboard did not commit')
        return head
    def close(self):
        errors=[]
        for process in reversed(self.processes):
            if process.poll() is None:
                # qa-server owns Wrangler/workerd descendants. Kill only this
                # explicitly-created PID tree; never taskkill /IM node.exe.
                try:
                    subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,timeout=15)
                    try:process.wait(8)
                    except subprocess.TimeoutExpired:process.kill();process.wait(5)
                except Exception as error:errors.append('owned_process:'+type(error).__name__)
        if self.store is not None:
            try:
                subprocess.run([sys.executable,str(Path(__file__).resolve()),'--credential-worker','clear',str(self.root),str(self.store)],
                    check=True,capture_output=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
            except Exception as error:errors.append('synthetic_credential:'+type(error).__name__)
        if self.hosts is not None:
            try:
                if self.hosts.read_bytes()!=self.hosts_after:raise RuntimeError('Runner hosts changed outside QA; refusing to overwrite unexpected changes')
                self.hosts.write_bytes(self.hosts_before)
                subprocess.run(['ipconfig','/flushdns'],capture_output=True,timeout=10)
            except Exception as error:errors.append('hosts_restore:'+type(error).__name__)
        if self.thumbprint:
            try:
                require_hosted()
                subprocess.run(['certutil','-delstore','Root',self.thumbprint],check=True,capture_output=True,timeout=15)
            except Exception as error:errors.append('test_ca_restore:'+type(error).__name__)
        if hasattr(self,'log'):self.log.close()
        if errors:raise RuntimeError('Runner QA cleanup incomplete: '+','.join(errors))

class Recorder:
    """Capture actual desktop; no manual QVariantAnimation/setCurrentTime use."""
    def __init__(self,out):self.out=out;self.out.mkdir(parents=True,exist_ok=True);self.segments=[]
    def run(self,label,action,duration=.9):
        require_hosted()
        if _PROGRESS is not None:
            _PROGRESS.record('before',label);_PROGRESS.screenshot(label+'-before')
        import mss
        from PIL import Image,ImageDraw
        folder=self.out/label;folder.mkdir();frames=[];failure=[];ready=threading.Event();stop=threading.Event()
        epoch=[None]
        # Sampling is QA-only. No app FPS override/cap or fake animation clock.
        def capture():
            try:
                with mss.mss() as grabber:
                    monitor=grabber.monitors[1];last=None;start=time.perf_counter();epoch[0]=start
                    while not stop.is_set():
                        raw=grabber.grab(monitor);timestamp=time.perf_counter()-start
                        body=raw.rgb;checksum=hashlib.sha256(body).hexdigest()
                        filename=None
                        if checksum!=last:
                            filename=f'{len(frames):05}.png'
                            Image.frombytes('RGB',raw.size,body).save(folder/filename,compress_level=1)
                        frames.append(dict(t=timestamp,sha256=checksum,file=filename));last=checksum
                        ready.set()
            except Exception as error:failure.append(type(error).__name__);ready.set()
        thread=threading.Thread(target=capture,daemon=True);thread.start()
        if not ready.wait(5):
            stop.set();thread.join(3)
            raise RuntimeError('Actual desktop first frame was not available before action')
        if failure:raise RuntimeError('Actual desktop capture unavailable: '+str(failure))
        action_start=time.perf_counter()-epoch[0]
        started=time.perf_counter();result=None;action_error=None
        try:result=action();time.sleep(max(0,duration-(time.perf_counter()-started)))
        except Exception as error:action_error=error
        finally:stop.set();thread.join(15)
        if thread.is_alive() or failure:raise RuntimeError('Actual frame capture did not complete')
        saved=[frame for frame in frames if frame['file']]
        if not saved:raise AssertionError('No actual native desktop pixels captured')
        selected=[saved[round(index*(len(saved)-1)/7)] for index in range(8)]
        contact=Image.new('RGB',(1000,4*400),'#dddddd');draw=ImageDraw.Draw(contact)
        for index,frame in enumerate(selected):
            left=(index%2)*500;top=(index//2)*400
            picture=Image.open(folder/frame['file']);picture.thumbnail((500,375));contact.paste(picture,(left,top+22))
            draw.text((left+4,top+3),f'{label} t={frame["t"]:.4f}s',fill='black')
        contact.save(folder/'contact-sheet.png')
        receipt=dict(label=label,elapsed=time.perf_counter()-started,frames=frames,distinct_pixels=len(saved),
            action_after_first_frame=True,action_after_capture_t=action_start,
            sampling='Uncapped real capture loop; capture/encoding time limits samples, NOT an app FPS measurement',
            manual_visual_review='required',actual_normal_exe=True)
        (folder/'timeline.json').write_text(json.dumps(receipt,indent=2));self.segments.append(receipt)
        if _PROGRESS is not None:
            _PROGRESS.screenshot(label+'-after');_PROGRESS.record('after')
        if action_error is not None:raise action_error
        return result

class _UIABackend:
    def __init__(self,pid,producer_pid,base):
        from pywinauto import Desktop
        self.desktop=Desktop(backend='uia');self.pid=pid;self.producer_pid=producer_pid;self.base=base;self.sequence=0
    def windows(self,pid=None):return [window for window in self.desktop.windows(process=pid or self.pid) if window.is_visible()]
    def tree(self):
        rows=[]
        for window in self.windows():
            for control in [window,*window.descendants()]:
                try:
                    rect=control.rectangle();rows.append(dict(name=control.element_info.name,role=control.element_info.control_type,class_name=control.element_info.class_name,
                        rectangle=[rect.left,rect.top,rect.right,rect.bottom],visible=control.is_visible(),enabled=control.is_enabled()))
                except Exception:pass
        return rows
    def find(self,name,*,pid=None,prefix=False,roles=None):
        matches=[]
        for window in self.windows(pid):
            for control in [window,*window.descendants()]:
                actual=control.element_info.name or ''
                if ((actual.startswith(name) if prefix else actual==name) and control.is_visible() and control.is_enabled()
                        and (roles is None or control.element_info.control_type in roles)):matches.append(control)
        return matches[0] if matches else None
    def _input_api(self):
        import ctypes
        from ctypes import wintypes
        api=ctypes.windll.user32
        declarations={'GetForegroundWindow':([],wintypes.HWND),
            'WindowFromPoint':([wintypes.POINT],wintypes.HWND),
            'GetWindowThreadProcessId':([wintypes.HWND,ctypes.POINTER(wintypes.DWORD)],wintypes.DWORD),
            'GetCursorPos':([ctypes.POINTER(wintypes.POINT)],wintypes.BOOL),
            'SetWindowPos':([wintypes.HWND,wintypes.HWND,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.UINT],wintypes.BOOL)}
        for name,(arguments,result) in declarations.items():
            function=getattr(api,name)
            if isinstance(function,ctypes._CFuncPtr):function.argtypes=arguments;function.restype=result
        return api
    def _window_pid(self,handle):
        import ctypes
        value=ctypes.c_ulong();self._input_api().GetWindowThreadProcessId(handle,ctypes.byref(value));return value.value
    def _producer_topmost(self,enabled):
        require_hosted();api=self._input_api()
        for window in self.windows(self.producer_pid):
            if self._window_pid(window.handle)!=self.producer_pid:raise RuntimeError('HARNESS_INPUT_OWNERSHIP: producer HWND changed owner')
            if not api.SetWindowPos(window.handle,-1 if enabled else -2,0,0,0,0,0x0013):
                raise OSError('HARNESS_INPUT_OWNERSHIP: cannot adjust own producer z-order')
    def _assert_pointer_owner(self,point,pid):
        from ctypes import wintypes
        actual=self._window_pid(self._input_api().WindowFromPoint(wintypes.POINT(*point)))
        if actual!=pid:raise RuntimeError(f'HARNESS_INPUT_OWNERSHIP: pointer target PID {actual}, expected {pid}; no input sent')
    def _assert_foreground(self,pid):
        # Windows may briefly report a null foreground during real activation.
        # Wait for the actual owner; never substitute an intended target.
        deadline=time.monotonic()+1.5
        while True:
            actual=self._window_pid(self._input_api().GetForegroundWindow())
            if actual==pid or time.monotonic()>=deadline:break
            time.sleep(.02)
        if actual!=pid:raise RuntimeError(f'HARNESS_INPUT_OWNERSHIP: foreground PID {actual}, expected {pid}; no input sent')
    def _prepare_control(self,control,pid):
        if pid not in (self.pid,self.producer_pid):raise ValueError('Input may target only the two owned QA processes')
        self._producer_topmost(pid==self.producer_pid)
        # The shelf deliberately permits mouse interaction without activating
        # its Tool window. Foreground ownership is essential for keyboard input,
        # not for a pointer whose actual hit target has already been checked.
        rect=control.rectangle();point=((rect.left+rect.right)//2,(rect.top+rect.bottom)//2)
        self._assert_pointer_owner(point,pid)
        api=self._input_api()
        row=dict(operation='mouse_target',expected_pid=pid,
                 foreground_pid=self._window_pid(api.GetForegroundWindow()),
                 producer=pid==self.producer_pid,pointer_owner_verified=True)
        try:
            import ctypes
            from ctypes import wintypes
            query=api.GetWindowLongW;query.argtypes=[wintypes.HWND,ctypes.c_int];query.restype=wintypes.LONG
            row['no_activate']=bool(query(control.top_level_parent().handle,-20)&0x08000000)
        except (AttributeError,TypeError):row['style_unavailable']=True
        with (self.base/'native-input-ownership.jsonl').open('a',encoding='utf8') as stream:stream.write(json.dumps(row)+'\n')
        return point
    def click(self,name,*,pid=None,prefix=False,roles=None):
        control=wait(lambda:self.find(name,pid=pid,prefix=prefix,roles=roles),seconds=6,label='native accessible control '+name)
        # Physical input through the OS. No invoke() callback shortcut.
        self._prepare_control(control,pid or self.pid)
        control.click_input()
        # The real click itself activates a normal external editor. Verify its
        # resulting foreground before any following Ctrl+C/V, not before click.
        if pid==self.producer_pid:self._assert_foreground(pid)
        return control
    def hotkey(self,keys,*,pid=None):
        from pywinauto.keyboard import send_keys
        if keys in ('^%+{SPACE}','^%{SPACE}'):
            self._producer_topmost(False)
        else:
            expected=pid or self.pid;self._assert_foreground(expected)
        send_keys(keys,pause=.02)
    def hover(self,name,*,prefix=False):
        from pywinauto import mouse
        control=wait(lambda:self.find(name,prefix=prefix),seconds=6,label='native hover target')
        point=self._prepare_control(control,self.pid);mouse.move(coords=point);self._assert_pointer_owner(point,self.pid)
    def focus_window(self,title):
        self._producer_topmost(False)
        target=wait(lambda:next((w for w in self.windows() if w.element_info.name==title),None),
                    seconds=6,label='actual native window '+title)
        target.set_focus()
        self._assert_foreground(self.pid)
        return True
    def drag_files(self,target_name,*,target_prefix=False):
        require_hosted()
        import ctypes
        from ctypes import wintypes
        from pywinauto import mouse
        target=wait(lambda:self.find(target_name,prefix=target_prefix),seconds=6,label='real native drop target')
        source=wait(lambda:self.find('Drag original files',pid=self.producer_pid),seconds=6,label='external native drag source')
        rectangle=target.rectangle();destination=(int((rectangle.left+rectangle.right)/2),int((rectangle.top+rectangle.bottom)/2))
        window=source.top_level_parent();bounds=window.rectangle();api=ctypes.windll.user32
        owner=ctypes.c_ulong();api.GetWindowThreadProcessId(window.handle,ctypes.byref(owner))
        if owner.value!=self.producer_pid:raise AssertionError('Refusing to move a window outside the disposable producer')
        width,height=api.GetSystemMetrics(0),api.GetSystemMetrics(1)
        if rectangle.right<=rectangle.left or rectangle.bottom<=rectangle.top or not (0<=destination[0]<width and 0<=destination[1]<height):
            raise RuntimeError('Native drop target is outside the disposable primary display')
        position=producer_corner(bounds.right-bounds.left,bounds.bottom-bounds.top,destination,width,height)
        api.SetWindowPos.argtypes=[wintypes.HWND,wintypes.HWND,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_uint]
        api.SetWindowPos.restype=wintypes.BOOL
        if not api.SetWindowPos(window.handle,None,*position,0,0,0x0015):raise OSError('Cannot place disposable producer for native drag')
        actual=window.rectangle()
        if actual.left<=destination[0]<actual.right and actual.top<=destination[1]<actual.bottom:
            raise RuntimeError('Disposable producer would cover the native drop destination')
        window.set_focus()
        source=wait(lambda:self.find('Drag original files',pid=self.producer_pid),seconds=6,label='placed external native drag source')
        rectangle=source.rectangle();start=(int((rectangle.left+rectangle.right)/2),int((rectangle.top+rectangle.bottom)/2))
        self._prepare_control(source,self.producer_pid);mouse.move(coords=start)
        try:
            mouse.press(button='left',coords=start)
            # OS motion enters a real QDrag loop in the independent producer.
            # Do not call application drag/drop handlers or UIA invoke().
            for index in range(1,19):
                mouse.move(coords=tuple(round(a+(b-a)*index/18) for a,b in zip(start,destination)));time.sleep(.015)
        finally:
            mouse.release(button='left',coords=destination);self._producer_topmost(False)
        return dict(source=list(start),destination=list(destination),os_drag=True)
    def command(self,action,**values):
        self.sequence+=1;path=self.base/'editor-command.json';temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(dict(id=self.sequence,action=action,**values)))
        # The independent producer briefly reads this mailbox on Windows.
        # Publishing the same command ID after a transient sharing lock must
        # not create another copy/input event or expose partial JSON.
        deadline=time.monotonic()+2
        while True:
            try:temporary.replace(path);break
            except PermissionError:
                if time.monotonic()>=deadline:raise RuntimeError('HARNESS_CONTROL_FILE_BUSY: external editor mailbox could not be published')
                time.sleep(.005)
        def acknowledged():
            try:return json.loads((self.base/'editor-ack.json').read_text())['id']==self.sequence
            except (OSError,ValueError):return False
        wait(acknowledged,label='external producer command')
    def text_copy(self,value):
        self.command('text',text=value);self.click('Synthetic external editor',pid=self.producer_pid)
        self.hotkey('^a',pid=self.producer_pid);self.hotkey('^c',pid=self.producer_pid)
        if self.inspect()['text']!=value:raise RuntimeError('HARNESS_INPUT_OWNERSHIP: external native Copy did not produce the requested synthetic text')
    def inspect(self):
        self.command('inspect');return json.loads((self.base/'native-result.json').read_text())
    def paste_text(self):
        self.command('text',text='');self.click('Synthetic external editor',pid=self.producer_pid)
        self.hotkey('^v',pid=self.producer_pid);self.command('read_editor')
        return json.loads((self.base/'paste-result.json').read_text())['text']
    def paste_file_in_explorer(self,source):
        from pywinauto import Desktop
        import ctypes
        self._producer_topmost(False)
        destination=self.base/('native-paste-'+os.urandom(5).hex());destination.mkdir()
        # Explorer can reuse its desktop process; own only this unique folder
        # window, never terminate the global shell/other tests' Explorer process.
        subprocess.Popen(['explorer.exe',str(destination)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        def target():
            for window in Desktop(backend='uia').windows(class_name='CabinetWClass'):
                if window.is_visible() and window.element_info.name==destination.name:return window
            return None
        explorer=wait(target,label='unique native Explorer paste destination')
        try:
            explorer.set_focus();self._assert_foreground(self._window_pid(explorer.handle))
            from pywinauto.keyboard import send_keys
            start=time.perf_counter();send_keys('^v',pause=.02)
            result=destination/Path(source).name
            wait(lambda:result.exists() and result.stat().st_size==Path(source).stat().st_size,label='native Explorer file Paste')
            if digest(result)!=digest(source):raise AssertionError('Actual Explorer paste changed original bytes')
            return (time.perf_counter()-start)*1000
        finally:ctypes.windll.user32.PostMessageW(explorer.handle,0x0010,0,0)
    def setting(self,name,value):
        settings=self.base/'FlickDock/settings.json'
        def current():return json.loads(settings.read_text())[name]
        if current()!=value:self.click({'clipboard_sync_enabled':'云剪贴板同步','sync_upload_enabled':'上传本机搁架到云端','sync_receive_enabled':'接收云端与其他设备内容'}[name],roles=['CheckBox'])
        wait(lambda:current()==value,label='persisted native settings '+name)


class _RemoteControl:
    def __init__(self,rectangle):self.bounds=rectangle
    def rectangle(self):
        from types import SimpleNamespace
        left,top,right,bottom=self.bounds
        return SimpleNamespace(left=left,top=top,right=right,bottom=bottom,
            mid_point=lambda:SimpleNamespace(x=(left+right)//2,y=(top+bottom)//2))


class Driver(_UIABackend):
    """UIA COM never runs in the controller/report/cleanup process.

    A provider may ignore Python's polling deadline while FindAll/rectangle is
    inside COM. One scoped worker process makes that wait actually bounded and
    allows the original harness error/frames to survive a provider hang.
    """
    def __init__(self,pid,producer_pid,base):
        require_hosted();self.pid=pid;self.producer_pid=producer_pid;self.base=base;self.sequence=0
        self._responses=queue.Queue();self._rpc_id=0;self._provider_failed=False
        self._worker_stderr=(base/'uia-worker-private.stderr').open('wb')
        self._worker=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--uia-worker',str(pid),str(producer_pid),str(base)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self._worker_stderr,text=True,encoding='utf8',creationflags=subprocess.CREATE_NO_WINDOW)
        def receive():
            for line in self._worker.stdout:
                try:self._responses.put(json.loads(line))
                except ValueError:continue
        threading.Thread(target=receive,daemon=True).start()
    def _rpc(self,operation,*,seconds=12,**values):
        if self._provider_failed or self._worker.poll() is not None:raise RuntimeError('UIA worker already unavailable; refusing repeated diagnostic COM calls')
        if _PROGRESS is not None:_PROGRESS.record('automation_before',operation=operation)
        self._rpc_id+=1;identity=self._rpc_id
        self._worker.stdin.write(json.dumps(dict(id=identity,operation=operation,values=values))+'\n');self._worker.stdin.flush()
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            try:result=self._responses.get(timeout=min(.2,max(.001,deadline-time.monotonic())))
            except queue.Empty:
                if self._worker.poll() is not None:raise RuntimeError('UIA worker exited during '+operation)
                continue
            if result.get('id')!=identity:continue
            if result.get('error'):raise RuntimeError('UIA '+operation+': '+result['error'])
            if _PROGRESS is not None:_PROGRESS.record('automation_after',operation=operation)
            return result.get('result')
        self._provider_failed=True;self._worker.terminate()
        try:self._worker.wait(3)
        except subprocess.TimeoutExpired:self._worker.kill();self._worker.wait(3)
        raise TimeoutError('HARNESS_AUTOMATION_REQUEST_TIMEOUT: '+operation+' exceeded '+str(seconds)+' seconds; application responsiveness not established')
    def windows(self,pid=None):return self._rpc('windows',pid=pid)
    def tree(self):return self._rpc('tree',seconds=8)
    def find(self,name,*,pid=None,prefix=False,roles=None):
        result=self._rpc('find',name=name,pid=pid,prefix=prefix,roles=roles)
        return _RemoteControl(result) if result else None
    def click(self,name,*,pid=None,prefix=False,roles=None):
        result=self._rpc('click',name=name,pid=pid,prefix=prefix,roles=roles)
        return _RemoteControl(result) if result else None
    def hotkey(self,keys,*,pid=None):return self._rpc('hotkey',keys=keys,pid=pid)
    def hover(self,name,*,prefix=False):return self._rpc('hover',name=name,prefix=prefix)
    def focus_window(self,title):return self._rpc('focus_window',title=title)
    def drag_files(self,target_name,*,target_prefix=False):return self._rpc('drag_files',seconds=24,target_name=target_name,target_prefix=target_prefix)
    def paste_file_in_explorer(self,source):return self._rpc('paste_file_in_explorer',seconds=45,source=str(source))
    def close(self):
        if self._worker.poll() is None:
            self._worker.terminate()
            try:self._worker.wait(3)
            except subprocess.TimeoutExpired:self._worker.kill();self._worker.wait(3)
        self._worker.stdin.close();self._worker.stdout.close();self._worker_stderr.close()


def uia_worker():
    require_hosted();backend=_UIABackend(int(sys.argv[2]),int(sys.argv[3]),Path(sys.argv[4]))
    allowed={'windows','find','click','tree','hotkey','hover','focus_window','paste_file_in_explorer','drag_files'}
    def bounds(control):
        rect=control.rectangle();return [rect.left,rect.top,rect.right,rect.bottom]
    for line in sys.stdin:
        command=json.loads(line);response={'id':command['id']}
        try:
            operation=command['operation']
            if operation not in allowed:raise ValueError('Unknown UIA operation')
            result=getattr(backend,operation)(**command.get('values',{}))
            if operation in ('find','click'):result=bounds(result) if result else None
            elif operation=='windows':result=[bounds(control) for control in result]
            response['result']=result
        except Exception as error:response['error']=type(error).__name__+': '+str(error)
        print(json.dumps(response),flush=True)
    return 0


def credential_worker():
    require_hosted();action,root,base=sys.argv[2:5];sys.path.insert(0,root)
    from account_store import SessionStore
    store=SessionStore(base,'https://'+DOMAINS[0])
    if action=='save':store.save(json.loads(Path(sys.argv[5]).read_text()))
    elif action=='clear':store.clear()
    else:raise ValueError('Unknown credential worker operation')
    print('Synthetic runner credential operation completed',flush=True);return 0

def synthetic_profile(base):
    from PIL import Image
    profile=base/'FlickDock';profile.mkdir();files=base/'files';files.mkdir()
    image=files/'original-image.png';Image.new('RGBA',(640,360),(25,90,220,170)).save(image)
    data=files/'original-8MiB.bin';data.write_bytes(bytes(range(256))*32768)
    text=files/'original-text.txt';text.write_text('Synthetic local file\n',encoding='utf8')
    small=files/'tiny.bin';small.write_bytes(b'original small bytes')
    settings=dict(version=1,theme='dark',autostart=False,startup_initialized=True,automatic_updates=False,
        clipboard_notice_seen=True,clipboard_sync_enabled=True,clipboard_success_notice=True,
        sync_upload_enabled=False,sync_receive_enabled=False,view_mode='icons',capture_mode='reference',
        dock_edge='right',dock_position=20,dock_length=160,dock_thickness=8,trigger_visible=True,keep_empty_handle=True)
    (profile/'settings.json').write_text(json.dumps(settings))
    refs=[str(path) for path in (image,text,small,data)];now=time.time()
    metadata={ref:{'shelf_added_at':datetime.datetime.fromtimestamp(now-index,datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} for index,ref in enumerate(refs)}
    (profile/'shelves.json').write_text(json.dumps(dict(version=6,shelf=dict(permanent=refs,texts={},metadata=metadata,pinned=True))))
    for mode in ('lan','wan'):
        records=[dict(name=f'Completed synthetic {index}.bin',peer='Local QA peer',state='完成',direction='send',when='11-01 12:30',paths=[str(small)]) for index in range(12)]
        (profile/(mode+'_history.json')).write_text(json.dumps(records))
    (base/'producer.json').write_text(json.dumps(dict(file=str(data),image=str(image),files=refs)))
    return profile,refs

def main():
    global _PROGRESS
    parser=argparse.ArgumentParser();parser.add_argument('--exe',type=Path,required=True)
    parser.add_argument('--sha256',required=True);parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--actions',type=Path)
    args=parser.parse_args();require_hosted()
    sys.path.insert(0,str(args.root.resolve()))
    if not re.fullmatch(r'[0-9a-f]{64}',args.sha256) or digest(args.exe)!=args.sha256:raise AssertionError('Wrong immutable frozen executable')
    args.output.mkdir(parents=True,exist_ok=True);qa_root=Path(__file__).resolve().parent
    _PROGRESS=Progress(args.output)
    import faulthandler
    stacks=(args.output/'qa-controller-stacks.log').open('w')
    faulthandler.dump_traceback_later(60,repeat=True,file=stacks)
    report=dict(ok=False,completion_eligible=False,exe_sha256=args.sha256,normal_entry=True,verify_switches=False,cocoa_smoke=False,
        production_entry_imported=False,frozen_executable_patched=False,public_cloud=False,physical_cross_device=False,installer_executed=False,
        all_15_groups_verified=False,manual_visual_review='required',checks=[],unverified=['Real public cloud latency/P95','Physical Windows↔Mac clipboard','GPU/AMD/VMware','Mac permissions/statusbar/update installation','Native installer update chain'])
    children=[];cloud=None;streams=[];temporary_profile=DeferredProfile();animation_preference=ClientAreaAnimation()
    try:
        with temporary_profile as temporary:
            base=Path(temporary);profile,refs=synthetic_profile(base)
            cloud=LocalCloud(args.root,base,qa_root)
            phase('local-cloud-start',cloud.start,screens=True)
            phase('local-synthetic-session',lambda:cloud.seed_session(profile),screens=True)
            phase('runner-client-area-animation',animation_preference.enable)
            environment=dict(os.environ,APPDATA=str(base),LOCALAPPDATA=str(base/'local'),QT_QPA_PLATFORM='windows',QT_ACCESSIBILITY='1')
            if any(environment.get(key,'').lower() in ('1','true','yes','on') for key in ('FLICKDOCK_REDUCE_MOTION','FLICKDOCK_LOW_PERFORMANCE')):
                raise AssertionError('Runner inherited an application reduced-motion override; native animation QA cannot proceed')
            # Avoid inheriting a CI proxy that would bypass local DNS/fixture.
            for key in tuple(environment):
                if key.upper() in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY'):environment.pop(key)
            environment['NO_PROXY']='127.0.0.1,localhost,'+','.join(DOMAINS)
            stdout=(base/'application.stdout').open('wb');stderr=(base/'application.stderr').open('wb')
            streams.extend((stdout,stderr))
            application=subprocess.Popen([str(args.exe.resolve())],cwd=base,env=environment,stdout=stdout,stderr=stderr);children.append(application)
            producer=subprocess.Popen([sys.executable,str(qa_root/'native_editor.py'),str(base)],env=environment,stdout=stdout,stderr=stderr);children.append(producer)
            driver=phase('automation-worker-start',lambda:Driver(application.pid,producer.pid,base));recorder=Recorder(args.output/'frames')
            phase('external-producer-window',lambda:wait(lambda:driver.windows(producer.pid),label='separate native producer'),screens=True)
            # A real global shortcut goes through the shipped native hotkey path.
            recorder.run('01-normal-overview-open',lambda:driver.hotkey('^%+{SPACE}'))
            phase('normal-overview-accessibility',lambda:wait(lambda:driver.find('搜索搁架内容'),label='actual normal frozen overview'),screens=True)
            report['checks'].append('Normal frozen entry created real manager/tray/overview; no early verification return')
            assert not any(row['method'] in ('POST','PUT') and row['path'].startswith('/v1/uploads')
                           for row in cloud.stats()['rows']),'Startup with shelf upload disabled wrote shelf data'
            phase('initial-accessibility-tree',lambda:(args.output/'initial-accessibility.json').write_text(json.dumps(driver.tree(),ensure_ascii=False,indent=2)),screens=True)
            def copy_and_committed(text):
                before=len(cloud.stats()['rows']);driver.text_copy(text)
                def ready():
                    head=json_request(cloud.origin,'GET','/v1/clipboard',token=cloud.session['token']).get('clipboard')
                    return head if head and head.get('ready') else None
                head=wait(ready,label='real local service clipboard commit')
                return before,head
            unique='synthetic native copy '+os.urandom(6).hex()
            before,head=recorder.run('02-native-text-copy',lambda:copy_and_committed(unique))
            assert driver.inspect()['text']==unique
            for _ in range(5):assert driver.paste_text()==unique,'Actual native Ctrl+V did not preserve original text'
            report['checks'].append('Actual Ctrl+V into the separate editor repeats original text without consuming it')
            # Reassert unchanged native content from a separate process, including
            # loss of private formats; this must not be 8 new network tasks.
            settled=cloud.stats()['rows'];before_posts=sum(row['method']=='POST' and row['path']=='/v1/clipboard' for row in settled)
            for _ in range(8):driver.text_copy(unique);time.sleep(.12)
            time.sleep(.5);after=cloud.stats()['rows'];after_posts=sum(row['method']=='POST' and row['path']=='/v1/clipboard' for row in after)
            assert after_posts==before_posts,'Same native content produced another upload task'
            assert json_request(cloud.origin,'GET','/v1/clipboard',token=cloud.session['token'])['clipboard']['id']==head['id']
            report['checks'].append('Actual external native text copy/reassertion: one real local-service head, no repeated POST')
            driver.command('text',text='typing without copy')
            driver.click('Synthetic external editor',pid=producer.pid);driver.hotkey('{END}ordinary-input',pid=producer.pid)
            idle_before=sum(row['method']=='POST' and row['path']=='/v1/clipboard' for row in cloud.stats()['rows'])
            time.sleep(2)
            assert driver.inspect()['text']==unique,'Typing changed the native clipboard without Copy'
            assert sum(row['method']=='POST' and row['path']=='/v1/clipboard' for row in cloud.stats()['rows'])==idle_before
            report['checks'].append('External ordinary typing and idle interval preserve clipboard/head without new upload')
            def receive_peer_clipboard():
                text='synthetic peer receive '+os.urandom(6).hex()
                posts=sum(row['method']=='POST' and row['path']=='/v1/clipboard' for row in cloud.stats()['rows'])
                remote_head=cloud.publish_clipboard(text)
                wait(lambda:driver.inspect()['text']==text,label='real peer clipboard written into native frozen client clipboard')
                for _ in range(3):assert driver.paste_text()==text,'Real received clipboard could not be pasted repeatedly'
                # Include the 5s retry boundary; no new copy is performed.
                time.sleep(5.2)
                assert sum(row['method']=='POST' and row['path']=='/v1/clipboard' for row in cloud.stats()['rows'])==posts,'Remote clipboard echoed into a new upload task'
                assert json_request(cloud.origin,'GET','/v1/clipboard',token=cloud.session['token'])['clipboard']['id']==remote_head['id'],'Received clipboard replaced itself while idle'
                assert driver.inspect()['text']==text
            recorder.run('02c-native-peer-receive-and-no-echo',receive_peer_clipboard)
            report['checks'].append('Real local-workerd peer inline receive writes native clipboard; repeated OS Paste and idle retry boundary produce no echo POST')
            recorder.run('03-settings-open',lambda:driver.click('设置'))
            driver.click('搁架与剪贴板设置')
            # Receive policy is tested against a real committed remote object,
            # not by merely reading a toggle's checked state.
            driver.setting('sync_receive_enabled',False);remote=cloud.publish_shelf();time.sleep(.7)
            journal=profile/'cloud-shelves'/cloud.session['account']['id']/'state.json'
            def state():
                try:return json.loads(journal.read_text())
                except (OSError,ValueError):return {}
            assert remote not in state().get('entries',{}),'Receive-off still added remote shelf item'
            recorder.run('04a-enable-shelf-receive',lambda:driver.setting('sync_receive_enabled',True))
            wait(lambda:remote in state().get('entries',{}),label='normal frozen receive resumes actual workerd item')
            recorder.run('04b-disable-shelf-receive',lambda:driver.setting('sync_receive_enabled',False))
            remote_after_off=cloud.publish_shelf();time.sleep(.7)
            assert remote_after_off not in state().get('entries',{}),'Receive-off after active session still accepted a new remote item'
            driver.setting('sync_receive_enabled',True)
            wait(lambda:remote_after_off in state().get('entries',{}),label='receive re-enable catches new item after active disable')
            driver.setting('sync_receive_enabled',False)
            report['checks'].append('Actual receive switch blocks remote item, then enabled state resumes real local-workerd item')
            recorder.run('04-disable-cloud-clipboard',lambda:driver.setting('clipboard_sync_enabled',False))
            before=cloud.stats()['rows'];driver.text_copy('local only after disabling '+os.urandom(4).hex());time.sleep(.6)
            assert not any(row['path'].startswith('/v1/clipboard') for row in cloud.stats()['rows'][len(before):]),'Disabled clipboard still requested cloud data'
            assert 'local only after disabling' in driver.inspect()['text']
            report['checks'].append('Actual persisted off switch blocks clipboard network work while native copy remains usable')
            # Resume by actual UI control, then exercise original-file local
            # pasteability while the service intentionally stalls the body PUT.
            driver.setting('clipboard_sync_enabled',True);cloud.command('/hold')
            recorder.run('05-native-file-copy-stalled-upload',lambda:driver.click('Copy original file',pid=producer.pid),duration=.7)
            wait(lambda:cloud.stats()['held']>0,label='actual frozen file PUT held by local service')
            native=driver.inspect();assert native['files'] and Path(native['files'][0]).samefile(refs[-1])
            assert 'application/x-flickdock-qa-native-source' in native['formats']
            assert digest(native['files'][0])==digest(refs[-1])
            report['local_file_paste_while_upload_held_ms']=driver.paste_file_in_explorer(refs[-1])
            assert cloud.stats()['held']>0,'Cloud upload unexpectedly completed before native file paste test'
            report['checks'].append('Original local file and native formats immediately readable while actual frozen body upload is held')
            report['checks'].append('Actual Windows Explorer Ctrl+V completed original 8MiB file while frozen cloud PUT remained held')
            # Turn off again with this PUT still outstanding, then deliver its
            # completion. There must be no late success/upload-result notice.
            driver.setting('clipboard_sync_enabled',False);cloud.command('/release');time.sleep(.6)
            assert driver.inspect()['files'] and Path(driver.inspect()['files'][0]).samefile(refs[-1])
            report['checks'].append('Disable during real in-flight PUT preserves native original payload')
            driver.setting('clipboard_sync_enabled',True)
            image_expected=hashlib.sha256(Path(refs[0]).read_bytes()).hexdigest()
            recorder.run('06-native-original-image-copy',lambda:driver.click('Copy original image',pid=producer.pid))
            def image_ready():
                current=json_request(cloud.origin,'GET','/v1/clipboard',token=cloud.session['token']).get('clipboard') or {}
                return current.get('kind')=='image' and current.get('ready') and current.get('sha256')==image_expected
            wait(image_ready,label='actual native original PNG committed by frozen client')
            assert driver.inspect()['image']
            report['checks'].append('Separate-process original PNG present in actual native clipboard')
            # The shelf upload switch must control actual requests, independently
            # of the clipboard switch and the separately-tested receive policy.
            driver.setting('clipboard_sync_enabled',False);cloud.command('/hold')
            upload_start=len(cloud.stats()['rows'])
            recorder.run('06a-enable-shelf-upload',lambda:driver.setting('sync_upload_enabled',True))
            wait(lambda:any(row['method']=='POST' and row['path']=='/v1/uploads' for row in cloud.stats()['rows'][upload_start:]),
                 label='normal frozen shelf upload request')
            wait(lambda:cloud.stats()['held']>0,label='real frozen shelf body PUT held before disabling upload')
            recorder.run('06b-disable-shelf-upload',lambda:driver.setting('sync_upload_enabled',False))
            cloud.command('/release');time.sleep(.7)
            stopped=len(cloud.stats()['rows']);time.sleep(.7)
            assert not any(row['method'] in ('POST','PUT') and row['path'].startswith('/v1/uploads')
                           for row in cloud.stats()['rows'][stopped:]),'Disabled shelf upload resumed network writes'
            recorder.run('06c-resume-shelf-upload',lambda:driver.setting('sync_upload_enabled',True))
            wait(lambda:any(row['method']=='POST' and row['path'].startswith('/v1/uploads/') and row['path'].endswith('/commit')
                            for row in cloud.stats()['rows'][stopped:]),label='real resumed shelf upload commit')
            driver.setting('sync_upload_enabled',False)
            report['checks'].append('Actual shelf upload off cancels/stops writes; enabling resumes a real local-workerd commit')
            recorder.run('07-close-settings',lambda:driver.click('关闭设置'))
            recorder.run('08-reopen-overview',lambda:driver.hotkey('^%+{SPACE}'))
            actions=json.loads(args.actions.read_text(encoding='utf8')) if args.actions else []
            for index,step in enumerate(actions):
                if application.poll() is not None:raise AssertionError('Frozen app exited during native UI flow')
                def execute(step=step):
                    if step['kind']=='click':return driver.click(step['name'],prefix=step.get('prefix',False))
                    if step['kind']=='hotkey':return driver.hotkey(step['keys'])
                    if step['kind']=='focus_window':return driver.focus_window(step['title'])
                    if step['kind']=='drag':
                        result=driver.drag_files(step['target'],target_prefix=step.get('target_prefix',False))
                        for name in step.get('expected_controls',[]):
                            wait(lambda name=name:driver.find(name),seconds=8,label='actual dropped file queued: '+name)
                        return result
                    if step['kind']=='hover':
                        return driver.hover(step['name'],prefix=step.get('prefix',False))
                    if step['kind']=='file_copy':return driver.click('Copy original file',pid=producer.pid)
                    if step['kind']=='image_copy':return driver.click('Copy original image',pid=producer.pid)
                    raise ValueError('Unsupported native action')
                recorder.run(f'flow-{index:02}-{step["id"]}',execute,duration=step.get('duration',.9))
                (args.output/f'flow-{index:02}-accessibility.json').write_text(json.dumps(driver.tree(),ensure_ascii=False,indent=2))
            report.update(ok=True,native_input=True,real_system_clipboard=True,local_workerd=True,tls_verification=True,
                actual_service_requests=cloud.stats()['rows'],screen_segments=[dict(label=row['label'],distinct_pixels=row['distinct_pixels']) for row in recorder.segments])
    except Exception as error:
        report['error']=type(error).__name__+': '+str(error);report['traceback']=traceback.format_exc()
        _PROGRESS.record('exception')
        _PROGRESS.screenshot('failure-no-uia')
        if 'driver' in locals() and not driver._provider_failed:
            try:phase('bounded-failure-accessibility',lambda:(args.output/'failure-accessibility.json').write_text(json.dumps(driver.tree(),ensure_ascii=False,indent=2)))
            except Exception:pass
        raise
    finally:
        _PROGRESS.record('before','cleanup')
        report['retained_diagnostics']=retain_diagnostics(args.output,profile if 'profile' in locals() else None,cloud)
        if 'driver' in locals():
            try:phase('cleanup-automation-worker',driver.close)
            except Exception as error:report['automation_cleanup_error']=type(error).__name__;report['ok']=False
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:child.wait(8)
                except subprocess.TimeoutExpired:child.kill();child.wait(5)
        if cloud is not None:
            try:phase('cleanup-local-cloud',cloud.close)
            except Exception as error:report['cleanup_error']=type(error).__name__+': '+str(error);report['ok']=False
        try:phase('cleanup-runner-client-area-animation',animation_preference.restore)
        except Exception as error:report['animation_preference_restore_error']=type(error).__name__+': '+str(error);report['ok']=False
        report['client_area_animation']=animation_preference.snapshot()
        for stream in streams:stream.close()
        try:temporary_profile.cleanup()
        except Exception as error:report['profile_cleanup_error']=type(error).__name__;report['ok']=False
        (args.output/'normal-frozen-native-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        coverage={str(index):dict(status='not_fully_verified',completion_eligible=False,reason='Raw actual normal-EXE steps require visual review and remaining platform/flow checks') for index in range(1,16)}
        if 'screen_segments' in report:
            coverage['1']['actual_steps']=['list/grid segments','time/size/default sort']
            coverage['3']['actual_steps']=['LAN/WAN mode and Back flows if all ui-actions completed']
            coverage['4']['actual_steps']=['OS drag from independent producer into both LAN/WAN targets and native Paste; four original-name queue controls appear; underlying path/integrity acceptance remains separate']
            coverage['5']['actual_steps']=['12 synthetic persisted history rows: delete/clear actual UI']
            coverage['7']['actual_steps']=['actual clear-confirm and settings category frames']
            coverage['8']['actual_steps']=['direct LAN/WAN OS file drop with actual feedback/row frames; preview transfer-zone drops remain unverified']
            coverage['10']['actual_steps']=['actual queue/history removal frames']
            coverage['11']['actual_steps']=['normal EXE clipboard popup and result producer frames']
        (args.output/'coverage-15-groups.json').write_text(json.dumps(coverage,ensure_ascii=False,indent=2))
        (args.output/'completion-gate.json').write_text(json.dumps(dict(implementation_complete=False,
            all_requested_changes_complete=False,publication_authorized=False,exe_sha256=args.sha256,
            reason='Native core flow is not complete 15-group visual, platform and installer acceptance; actual frames require review'),indent=2))
        _PROGRESS.record('done');_PROGRESS.close();faulthandler.cancel_dump_traceback_later();stacks.close()
    return 0 if report['ok'] else 1

if __name__=='__main__':
    worker=sys.argv[1] if len(sys.argv)>1 else ''
    raise SystemExit(uia_worker() if worker=='--uia-worker' else credential_worker() if worker=='--credential-worker' else main())
