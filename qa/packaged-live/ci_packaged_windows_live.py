"""Hosted Windows only: NORMAL frozen entry, external input and actual screen pixels.

The EXE is never imported/patched and receives no --verify-* arguments. Local
workerd is a real service fixture behind a normally verified TLS gateway, NOT
the public Cloudflare deployment or a physical second device. All credentials,
keys, fixtures and system changes belong to the disposable runner.
"""
from __future__ import annotations
import argparse,datetime,hashlib,http.client,json,os,queue,re,socket,ssl,subprocess,sys,tempfile,threading,time,traceback
from pathlib import Path
from urllib.request import Request,urlopen

DOMAINS=('flickdock-cloud.roger000416.workers.dev','flickdock-site.roger000416.workers.dev')

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

class DeferredProfile:
    """Children and credential store close before Windows removes their files."""
    def __init__(self):self.directory=tempfile.TemporaryDirectory(prefix='flickdock-frozen-normal-ci-')
    def __enter__(self):return self.directory.name
    def __exit__(self,*_):pass
    def cleanup(self):self.directory.cleanup()

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
        subprocess.run(['certutil','-user','-addstore','Root',str(cert)],check=True,capture_output=True)
        self.hosts=Path(os.environ['SystemRoot'])/'System32/drivers/etc/hosts'
        self.hosts_before=self.hosts.read_bytes()
        self.hosts_after=self.hosts_before+b'\r\n# FlickDock ephemeral local QA\r\n'+''.join('127.0.0.1 '+domain+'\r\n' for domain in DOMAINS).encode()
        self.hosts.write_bytes(self.hosts_after)
        subprocess.run(['ipconfig','/flushdns'],check=True,capture_output=True)
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
        from account_store import SessionStore
        name='live-'+os.urandom(8).hex();session=self.sign(name);self.peer=self.sign(name,True)
        self.store=SessionStore(base,'https://'+DOMAINS[0]);self.store.save(session)
        self.session=session
    def stats(self):return json_request(self.control,'GET','/stats')
    def command(self,path):
        with urlopen(self.control+path,timeout=5) as response:response.read()
    def publish_shelf(self):
        import uuid
        identity=str(uuid.uuid4());body=b'Synthetic remote shelf object\n';checksum=hashlib.sha256(body).hexdigest()
        result=json_request(self.origin,'POST','/v1/uploads',dict(item_id=identity,base_version=0,kind='text',name='Actual local peer item',
            mime_type='text/plain',size=len(body),sha256=checksum,fast_upload=True),self.peer['token'])
        binary_request(self.origin,'PUT',f'/v1/uploads/{result["upload_id"]}/parts/0',body,self.peer['token'],{'X-Content-SHA256':checksum})
        json_request(self.origin,'POST',f'/v1/uploads/{result["upload_id"]}/commit',{},self.peer['token'])
        return identity
    def close(self):
        for process in reversed(self.processes):
            if process.poll() is None:
                # qa-server owns Wrangler/workerd descendants. Kill only this
                # explicitly-created PID tree; never taskkill /IM node.exe.
                subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,timeout=15)
                try:process.wait(8)
                except subprocess.TimeoutExpired:process.kill();process.wait(5)
        if self.store is not None:self.store.clear()
        if self.hosts is not None:
            if self.hosts.read_bytes()!=self.hosts_after:raise RuntimeError('Runner hosts changed outside QA; refusing to overwrite unexpected changes')
            self.hosts.write_bytes(self.hosts_before)
            subprocess.run(['ipconfig','/flushdns'],capture_output=True)
        if self.thumbprint:subprocess.run(['certutil','-user','-delstore','Root',self.thumbprint],check=True,capture_output=True)
        if hasattr(self,'log'):self.log.close()

class Recorder:
    """Capture actual desktop; no manual QVariantAnimation/setCurrentTime use."""
    def __init__(self,out):self.out=out;self.out.mkdir(parents=True,exist_ok=True);self.segments=[]
    def run(self,label,action,duration=.9):
        require_hosted()
        import mss
        from PIL import Image,ImageDraw
        folder=self.out/label;folder.mkdir();frames=[];failure=[];ready=threading.Event();stop=threading.Event()
        # Sampling is QA-only. No app FPS override/cap or fake animation clock.
        def capture():
            try:
                with mss.mss() as grabber:
                    monitor=grabber.monitors[1];ready.set();last=None;start=time.perf_counter()
                    while not stop.is_set():
                        timestamp=time.perf_counter()-start;raw=grabber.grab(monitor)
                        body=raw.rgb;checksum=hashlib.sha256(body).hexdigest()
                        filename=None
                        if checksum!=last:
                            filename=f'{len(frames):05}.png'
                            Image.frombytes('RGB',raw.size,body).save(folder/filename,compress_level=1)
                        frames.append(dict(t=timestamp,sha256=checksum,file=filename));last=checksum
            except Exception as error:failure.append(type(error).__name__);ready.set()
        thread=threading.Thread(target=capture,daemon=True);thread.start();ready.wait(5)
        if failure:raise RuntimeError('Actual desktop capture unavailable: '+str(failure))
        started=time.perf_counter();result=None;action_error=None
        try:result=action();time.sleep(max(0,duration-(time.perf_counter()-started)))
        except Exception as error:action_error=error
        finally:stop.set();thread.join(15)
        if thread.is_alive() or failure:raise RuntimeError('Actual frame capture did not complete')
        saved=[frame for frame in frames if frame['file']]
        if not saved:raise AssertionError('No actual native desktop pixels captured')
        selected=[saved[round(index*(len(saved)-1)/7)] for index in range(8)]
        contact=Image.new('RGB',(960,8*210),'#dddddd');draw=ImageDraw.Draw(contact)
        for index,frame in enumerate(selected):
            picture=Image.open(folder/frame['file']);picture.thumbnail((960,188));contact.paste(picture,(0,index*210+22))
            draw.text((4,index*210+3),f'{label} t={frame["t"]:.4f}s',fill='black')
        contact.save(folder/'contact-sheet.png')
        receipt=dict(label=label,elapsed=time.perf_counter()-started,frames=frames,distinct_pixels=len(saved),
            sampling='Uncapped real capture loop; capture/encoding time limits samples, NOT an app FPS measurement',
            manual_visual_review='required',actual_normal_exe=True)
        (folder/'timeline.json').write_text(json.dumps(receipt,indent=2));self.segments.append(receipt)
        if action_error is not None:raise action_error
        return result

class Driver:
    def __init__(self,pid,producer_pid,base):
        from pywinauto import Desktop
        self.desktop=Desktop(backend='uia');self.pid=pid;self.producer_pid=producer_pid;self.base=base;self.sequence=0
    def windows(self,pid=None):return [window for window in self.desktop.windows(process=pid or self.pid) if window.is_visible()]
    def tree(self):
        rows=[]
        for window in self.windows():
            for control in [window,*window.descendants()]:
                try:
                    rect=control.rectangle();rows.append(dict(name=control.element_info.name,role=control.element_info.control_type,
                        rectangle=[rect.left,rect.top,rect.right,rect.bottom],visible=control.is_visible(),enabled=control.is_enabled()))
                except Exception:pass
        return rows
    def find(self,name,*,pid=None,prefix=False):
        matches=[]
        for window in self.windows(pid):
            for control in [window,*window.descendants()]:
                actual=control.element_info.name or ''
                if (actual.startswith(name) if prefix else actual==name) and control.is_visible() and control.is_enabled():matches.append(control)
        return matches[0] if matches else None
    def click(self,name,*,pid=None,prefix=False):
        control=wait(lambda:self.find(name,pid=pid,prefix=prefix),label='native accessible control '+name)
        # Physical input through the OS. No invoke() callback shortcut.
        control.click_input();return control
    def hotkey(self,keys):
        from pywinauto.keyboard import send_keys
        send_keys(keys,pause=.02)
    def command(self,action,**values):
        self.sequence+=1;path=self.base/'editor-command.json';temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(dict(id=self.sequence,action=action,**values)));temporary.replace(path)
        def acknowledged():
            try:return json.loads((self.base/'editor-ack.json').read_text())['id']==self.sequence
            except (OSError,ValueError):return False
        wait(acknowledged,label='external producer command')
    def text_copy(self,value):
        self.command('text',text=value);self.click('Synthetic external editor',pid=self.producer_pid)
        self.hotkey('^a');self.hotkey('^c')
    def inspect(self):
        self.command('inspect');return json.loads((self.base/'native-result.json').read_text())
    def paste_text(self):
        self.command('text',text='');self.click('Synthetic external editor',pid=self.producer_pid)
        self.hotkey('^v');self.command('read_editor')
        return json.loads((self.base/'paste-result.json').read_text())['text']
    def paste_file_in_explorer(self,source):
        from pywinauto import Desktop
        import ctypes
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
            explorer.set_focus();start=time.perf_counter();self.hotkey('^v')
            result=destination/Path(source).name
            wait(lambda:result.exists() and result.stat().st_size==Path(source).stat().st_size,label='native Explorer file Paste')
            if digest(result)!=digest(source):raise AssertionError('Actual Explorer paste changed original bytes')
            return (time.perf_counter()-start)*1000
        finally:ctypes.windll.user32.PostMessageW(explorer.handle,0x0010,0,0)
    def setting(self,name,value):
        settings=self.base/'FlickDock/settings.json'
        def current():return json.loads(settings.read_text())[name]
        if current()!=value:self.click({'clipboard_sync_enabled':'云剪贴板同步','sync_upload_enabled':'上传本机搁架到云端','sync_receive_enabled':'接收云端与其他设备内容'}[name])
        wait(lambda:current()==value,label='persisted native settings '+name)

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
    (profile/'shelves.json').write_text(json.dumps(dict(version=6,shelf=dict(permanent=refs,texts={},metadata={ref:{'added_at':now-index} for index,ref in enumerate(refs)},pinned=True))))
    for mode in ('lan','wan'):
        records=[dict(name=f'Completed synthetic {index}.bin',peer='Local QA peer',state='完成',direction='send',when='11-01 12:30',paths=[str(small)]) for index in range(12)]
        (profile/(mode+'_history.json')).write_text(json.dumps(records))
    (base/'producer.json').write_text(json.dumps(dict(file=str(data),image=str(image),files=refs)))
    return profile,refs

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--exe',type=Path,required=True)
    parser.add_argument('--sha256',required=True);parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--actions',type=Path)
    args=parser.parse_args();require_hosted()
    sys.path.insert(0,str(args.root.resolve()))
    if not re.fullmatch(r'[0-9a-f]{64}',args.sha256) or digest(args.exe)!=args.sha256:raise AssertionError('Wrong immutable frozen executable')
    args.output.mkdir(parents=True,exist_ok=True);qa_root=Path(__file__).resolve().parent
    report=dict(ok=False,completion_eligible=False,exe_sha256=args.sha256,normal_entry=True,verify_switches=False,cocoa_smoke=False,
        production_entry_imported=False,frozen_executable_patched=False,public_cloud=False,physical_cross_device=False,installer_executed=False,
        all_15_groups_verified=False,manual_visual_review='required',checks=[],unverified=['Real public cloud latency/P95','Physical Windows↔Mac clipboard','GPU/AMD/VMware','Mac permissions/statusbar/update installation','Native installer update chain'])
    children=[];cloud=None;streams=[];temporary_profile=DeferredProfile()
    try:
        with temporary_profile as temporary:
            base=Path(temporary);profile,refs=synthetic_profile(base)
            cloud=LocalCloud(args.root,base,qa_root);cloud.start();cloud.seed_session(profile)
            environment=dict(os.environ,APPDATA=str(base),LOCALAPPDATA=str(base/'local'),QT_QPA_PLATFORM='windows',QT_ACCESSIBILITY='1')
            # Avoid inheriting a CI proxy that would bypass local DNS/fixture.
            for key in tuple(environment):
                if key.upper() in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY'):environment.pop(key)
            environment['NO_PROXY']='127.0.0.1,localhost,'+','.join(DOMAINS)
            stdout=(base/'application.stdout').open('wb');stderr=(base/'application.stderr').open('wb')
            streams.extend((stdout,stderr))
            application=subprocess.Popen([str(args.exe.resolve())],cwd=base,env=environment,stdout=stdout,stderr=stderr);children.append(application)
            producer=subprocess.Popen([sys.executable,str(qa_root/'native_editor.py'),str(base)],env=environment,stdout=stdout,stderr=stderr);children.append(producer)
            driver=Driver(application.pid,producer.pid,base);recorder=Recorder(args.output/'frames')
            wait(lambda:driver.windows(producer.pid),label='separate native producer')
            # A real global shortcut goes through the shipped native hotkey path.
            recorder.run('01-normal-overview-open',lambda:driver.hotkey('^%+{SPACE}'))
            wait(lambda:driver.find('搜索搁架内容'),label='actual normal frozen overview')
            report['checks'].append('Normal frozen entry created real manager/tray/overview; no early verification return')
            (args.output/'initial-accessibility.json').write_text(json.dumps(driver.tree(),ensure_ascii=False,indent=2))
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
            recorder.run('07-close-settings',lambda:driver.click('关闭设置'))
            recorder.run('08-reopen-overview',lambda:driver.hotkey('^%+{SPACE}'))
            actions=json.loads(args.actions.read_text(encoding='utf8')) if args.actions else []
            for index,step in enumerate(actions):
                if application.poll() is not None:raise AssertionError('Frozen app exited during native UI flow')
                def execute(step=step):
                    if step['kind']=='click':return driver.click(step['name'],prefix=step.get('prefix',False))
                    if step['kind']=='hotkey':return driver.hotkey(step['keys'])
                    if step['kind']=='hover':
                        control=wait(lambda:driver.find(step['name'],prefix=step.get('prefix',False)),label='hover control')
                        rect=control.rectangle();from pywinauto import mouse;mouse.move(coords=(rect.mid_point().x,rect.mid_point().y));return
                    if step['kind']=='file_copy':return driver.click('Copy original file',pid=producer.pid)
                    if step['kind']=='image_copy':return driver.click('Copy original image',pid=producer.pid)
                    raise ValueError('Unsupported native action')
                recorder.run(f'flow-{index:02}-{step["id"]}',execute,duration=step.get('duration',.9))
                (args.output/f'flow-{index:02}-accessibility.json').write_text(json.dumps(driver.tree(),ensure_ascii=False,indent=2))
            report.update(ok=True,native_input=True,real_system_clipboard=True,local_workerd=True,tls_verification=True,
                actual_service_requests=cloud.stats()['rows'],screen_segments=[dict(label=row['label'],distinct_pixels=row['distinct_pixels']) for row in recorder.segments])
            # Retain only bounded payload-free synthetic provenance, never the
            # session/key/cert/profile or arbitrary application log directory.
            audit=profile/'diagnostics/clipboard-writes.jsonl'
            if audit.exists() and audit.stat().st_size<=2*1024*1024:(args.output/'synthetic-clipboard-provenance.jsonl').write_bytes(audit.read_bytes())
    except Exception as error:
        report['error']=type(error).__name__+': '+str(error);report['traceback']=traceback.format_exc()
        if 'driver' in locals():
            try:(args.output/'failure-accessibility.json').write_text(json.dumps(driver.tree(),ensure_ascii=False,indent=2))
            except Exception:pass
        raise
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:child.wait(8)
                except subprocess.TimeoutExpired:child.kill();child.wait(5)
        if cloud is not None:
            try:cloud.close()
            except Exception as error:report['cleanup_error']=type(error).__name__+': '+str(error);report['ok']=False
        for stream in streams:stream.close()
        try:temporary_profile.cleanup()
        except OSError as error:report['profile_cleanup_error']=type(error).__name__;report['ok']=False
        (args.output/'normal-frozen-native-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        coverage={str(index):dict(status='not_fully_verified',completion_eligible=False,reason='Raw actual normal-EXE steps require visual review and remaining platform/flow checks') for index in range(1,16)}
        if 'screen_segments' in report:
            coverage['1']['actual_steps']=['list/grid segments','time/size/default sort']
            coverage['3']['actual_steps']=['LAN/WAN mode and Back flows if all ui-actions completed']
            coverage['5']['actual_steps']=['12 synthetic persisted history rows: delete/clear actual UI']
            coverage['7']['actual_steps']=['actual clear-confirm and settings category frames']
            coverage['10']['actual_steps']=['actual queue/history removal frames']
            coverage['11']['actual_steps']=['normal EXE clipboard popup and result producer frames']
        (args.output/'coverage-15-groups.json').write_text(json.dumps(coverage,ensure_ascii=False,indent=2))
        (args.output/'completion-gate.json').write_text(json.dumps(dict(implementation_complete=False,
            all_requested_changes_complete=False,publication_authorized=False,exe_sha256=args.sha256,
            reason='Native core flow is not complete 15-group visual, platform and installer acceptance; actual frames require review'),indent=2))
    return 0 if report['ok'] else 1

if __name__=='__main__':raise SystemExit(main())
