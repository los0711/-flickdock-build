"""Private, unmodified Syncthing sidecar controlled through its official API.

Only manual transfer workers use this module. It never receives a shelf or
clipboard reference, adds unknown devices, or enables a public relay.
"""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError,URLError
from urllib.parse import quote,urlsplit
from urllib.request import HTTPRedirectHandler,ProxyHandler,Request,build_opener
import xml.etree.ElementTree as ET

from video_jobs import ProcessTree,PROCESS_TREE_STOP_SECONDS

KERNEL_VERSION='v2.1.7'
START_TIMEOUT_SECONDS=20
START_POLL_SECONDS=.05
API_TIMEOUT_SECONDS=8
API_MAX_BYTES=4*1024*1024
DEVICE_ID=re.compile(r'[A-Z2-7]{7}(?:-[A-Z2-7]{7}){7}')
TASK_ID=re.compile(r'fd-manual-[a-f0-9]{32}')
PRIVATE_NETWORKS=('0.0.0.0/8','10.0.0.0/8','100.64.0.0/10','127.0.0.0/8',
                  '169.254.0.0/16','172.16.0.0/12','192.168.0.0/16',
                  '::/128','::1/128','fc00::/7','fe80::/10')
REMOTE_NETWORKS=['!'+value for value in PRIVATE_NETWORKS]+['0.0.0.0/0','::/0']


class EngineError(RuntimeError):
    def __init__(self,message,status=0):
        super().__init__(message);self.status=status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,request,*args):
        raise EngineError('传输内核返回了无效的控制地址。')


def _free_port(host):
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as stream:
        stream.bind((host,0));return stream.getsockname()[1]


def check_device(value):
    if not isinstance(value,str) or not DEVICE_ID.fullmatch(value):raise ValueError('传输设备身份无效。')
    return value


def check_task(value):
    if not isinstance(value,str) or not TASK_ID.fullmatch(value):raise ValueError('传输任务标识无效。')
    return value


def check_addresses(values,mode):
    if not isinstance(values,list) or not 1<=len(values)<=12:raise ValueError('传输设备地址无效。')
    result=[]
    for address in values:
        if address=='dynamic' and mode=='remote':result.append(address);continue
        if not isinstance(address,str) or len(address)>256:raise ValueError('传输设备地址无效。')
        url=urlsplit(address)
        try:host=ipaddress.ip_address(url.hostname or '');port=url.port
        except ValueError as error:raise ValueError('传输设备地址无效。') from error
        if (url.scheme not in ('tcp','tcp4','tcp6','quic','quic4','quic6') or not port
                or url.username or url.password or url.path or url.query or url.fragment
                or host.is_unspecified or host.is_multicast):raise ValueError('传输设备地址无效。')
        if mode=='lan' and not (host.is_private or host.is_link_local or host.is_loopback):raise ValueError('局域网互传只接受本地网络地址。')
        if mode=='remote' and not host.is_global:raise ValueError('远程互传不能切换到局域网地址。')
        result.append(address)
    return list(dict.fromkeys(result))


class SyncthingEngine:
    def __init__(self,home,*,mode,binary=None,loopback=False):
        if mode not in ('lan','remote'):raise ValueError('Unknown manual transfer mode')
        self.home=Path(home).absolute();self.mode=mode;self.loopback=loopback
        self.binary=Path(binary) if binary else Path(__file__).parent/'syncthing/runtime'/('syncthing.exe' if sys.platform=='win32' else 'syncthing')
        self.process=None;self.tree=None;self.log=None;self.device_id='';self.port=0
        self._key='';self._url='';self._closed=False
        self._lock=threading.RLock();self._configuration=threading.RLock();self._folders={}
        self._opener=build_opener(ProxyHandler({}),_NoRedirect())

    def _child_environment(self):
        env=dict(os.environ)
        # Local REST must never enter a proxy. The native engine also does not
        # inherit HTTP proxy variables intended for a shell or another app.
        for key in list(env):
            if (key.upper().endswith('_PROXY') or key.startswith('ST')
                    or key in ('FLICKDOCK_QA_SYNC_JSON','GITHUB_TOKEN','GH_TOKEN','ACTIONS_RUNTIME_TOKEN')):env.pop(key)
        temporary=self.home/'tmp';temporary.mkdir(exist_ok=True)
        env.update(TEMP=str(temporary),TMP=str(temporary),STNOUPGRADE='1',STNORESTART='1')
        return env

    def _configure(self):
        path=self.home/'config.xml';tree=ET.parse(path);cfg=tree.getroot()
        # These profiles belong exclusively to finite manual jobs. Dropping an
        # abandoned share at startup cannot publish a deletion to a user folder.
        for child in cfg.findall('folder'):cfg.remove(child)
        own_cert=(self.home/'cert.pem').read_bytes()
        if not own_cert:raise EngineError('传输内核设备证书缺失。')
        for child in cfg.findall('device'):cfg.remove(child)
        gui=cfg.find('gui')
        if gui is None:raise EngineError('传输内核配置缺少控制接口。')
        gui.set('enabled','true');gui.set('tls','false')
        self._key=secrets.token_hex(32);self._url='http://127.0.0.1:'+str(_free_port('127.0.0.1'))
        def value(parent,name,text):
            node=parent.find(name)
            if node is None:node=ET.SubElement(parent,name)
            node.text=text
        value(gui,'address',self._url.removeprefix('http://'));value(gui,'apikey',self._key)
        options=cfg.find('options')
        if options is None:raise EngineError('传输内核配置缺少网络选项。')
        for child in options.findall('listenAddress'):options.remove(child)
        self.port=_free_port('127.0.0.1' if self.loopback else '0.0.0.0')
        bind='127.0.0.1' if self.loopback else '0.0.0.0'
        ET.SubElement(options,'listenAddress').text=f'tcp://{bind}:{self.port}'
        if not self.loopback:ET.SubElement(options,'listenAddress').text=f'quic://{bind}:{self.port}'
        remote=self.mode=='remote' and not self.loopback
        for name,text in dict(localAnnounceEnabled='false',globalAnnounceEnabled=str(remote).lower(),
                natEnabled=str(remote).lower(),relaysEnabled='false',announceLANAddresses='false',
                startBrowser='false',autoUpgradeIntervalH='0',urAccepted='-1',crashReportingEnabled='false',
                maxSendKbps='0',maxRecvKbps='0',progressUpdateIntervalS='1',setLowPriority='false').items():value(options,name,text)
        # A generated config contains no user paths; write it atomically before
        # starting the upstream process, never edit its live XML configuration.
        temporary=path.with_suffix('.new');tree.write(temporary,encoding='utf-8',xml_declaration=True)
        temporary.chmod(0o600);os.replace(temporary,path)

    def start(self):
        with self._lock:
            if self._closed:raise EngineError('传输内核已经关闭。')
            if self.process is not None:
                if self.process.poll() is not None:raise EngineError('传输内核意外退出，请重新打开文件互传。')
                return self.device_id
            if not self.binary.is_file():raise EngineError('安装包缺少 Syncthing 传输内核，请重新安装 FlickDock。')
            if self.home.is_symlink():raise EngineError('传输内核目录无效。')
            self.home.mkdir(parents=True,exist_ok=True,mode=0o700)
            env=self._child_environment();flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
            if not (self.home/'config.xml').exists():
                try:subprocess.run([str(self.binary),'generate','--home',str(self.home),'--no-port-probing'],
                    env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=START_TIMEOUT_SECONDS,check=True,creationflags=flags)
                except (OSError,subprocess.SubprocessError) as error:raise EngineError('无法初始化 Syncthing 传输内核。') from error
            self._configure()
            self.log=(self.home/'engine.log').open('wb')
            try:
                self.process=subprocess.Popen([str(self.binary),'serve','--home',str(self.home),
                    '--no-browser','--no-restart','--no-upgrade','--log-file=-'],env=env,
                    stdout=self.log,stderr=subprocess.STDOUT,creationflags=flags,start_new_session=sys.platform!='win32')
                self.tree=ProcessTree(self.process.pid)
                deadline=time.monotonic()+START_TIMEOUT_SECONDS
                while time.monotonic()<deadline:
                    if self.process.poll() is not None:raise EngineError('Syncthing 传输内核启动失败；诊断信息已保存在应用日志中。')
                    try:
                        status=self.request('GET','/rest/system/status')
                        self.device_id=check_device(status['myID'])
                        listener=f'tcp://{"127.0.0.1" if self.loopback else "0.0.0.0"}:{self.port}'
                        ready=status['connectionServiceStatus'].get(listener)
                        if ready and ready.get('error'):raise EngineError('传输内核无法监听连接，请检查端口和系统网络权限。')
                        # REST can answer before the independent sync listener
                        # binds. Do not announce a port that is not ready yet.
                        if ready and (ready.get('lanAddresses') or ready.get('wanAddresses')):return self.device_id
                    except (URLError,ConnectionError):pass
                    time.sleep(START_POLL_SECONDS)
                raise EngineError('Syncthing 传输内核启动超时。')
            except BaseException:
                self._stop();raise

    def request(self,method,path,data=None):
        if not path.startswith('/rest/') or '://' in path or '#' in path:raise ValueError('Invalid local kernel endpoint')
        raw=json.dumps(data).encode('utf8') if data is not None else None
        request=Request(self._url+path,method=method,data=raw,headers={'X-API-Key':self._key,'Content-Type':'application/json'})
        try:
            with self._opener.open(request,timeout=API_TIMEOUT_SECONDS) as reply:
                body=reply.read(API_MAX_BYTES+1)
                if len(body)>API_MAX_BYTES:raise EngineError('传输内核响应超过限制。')
                if not body:return None
                try:return json.loads(body)
                except ValueError as error:raise EngineError('传输内核响应格式无效。') from error
        except HTTPError as error:
            # The upstream body can contain local filenames; keep it in its own
            # diagnostic log, never put it into a toast or a public report.
            raise EngineError(f'传输内核请求未完成（HTTP {error.code}）。',error.code) from error

    def local_address(self,host):
        self.start();address=ipaddress.ip_address(host)
        return f'tcp://[{address}]:{self.port}' if address.version==6 else f'tcp://{address}:{self.port}'

    def pair(self,peer,addresses):
        check_device(peer);addresses=check_addresses(addresses,'lan' if self.loopback else self.mode)
        self.start()
        with self._configuration:
            device=self.request('GET','/rest/config/defaults/device')
            device.update(deviceID=peer,name='FlickDock manual peer',addresses=addresses,
                          autoAcceptFolders=False,introducer=False,compression='metadata',
                          allowedNetworks=REMOTE_NETWORKS if self.mode=='remote' and not self.loopback else [])
            self.request('POST','/rest/config/devices',device)

    def share(self,task,folder,peer,addresses,*,send):
        check_task(task)
        path=Path(folder).resolve(strict=True)
        if not path.is_dir() or path.is_symlink():raise ValueError('传输任务文件夹无效。')
        with self._configuration:
            self.pair(peer,addresses)
            config=self.request('GET','/rest/config/defaults/folder')
            config.update(id=task,label='FlickDock manual transfer',path=str(path),
                          type='sendonly' if send else 'receiveonly',rescanIntervalS=0,
                          fsWatcherEnabled=False,devices=[{'deviceID':peer}],ignorePerms=True,
                          syncOwnership=False,sendOwnership=False,syncXattrs=False,sendXattrs=False)
            self.request('POST','/rest/config/folders',config);self._folders[task]=peer
            self.request('POST','/rest/db/scan?folder='+quote(task,safe=''))

    def completion(self,task,peer=None):
        check_task(task)
        query='folder='+quote(task,safe='')
        if peer:query+='&device='+quote(check_device(peer),safe='')
        return self.request('GET','/rest/db/completion?'+query)

    def status(self,task):
        return self.request('GET','/rest/db/status?folder='+quote(check_task(task),safe=''))

    def events(self,since):
        if type(since) is not int or since<0:raise ValueError('Invalid local event cursor')
        result=self.request('GET',f'/rest/events?since={since}&timeout=1&limit=32&events=DownloadProgress,FolderErrors')
        if not isinstance(result,list):raise EngineError('传输内核事件格式无效。')
        return result

    def connections(self):
        return self.request('GET','/rest/system/connections')['connections']

    def addresses(self):
        """Exchange native WAN candidates without waiting for discovery caches."""
        if self.loopback:return [self.local_address('127.0.0.1')]
        rows=self.request('GET','/rest/system/status')['connectionServiceStatus']
        addresses=[]
        for row in rows.values():
            for value in row.get('wanAddresses',[]):
                try:valid=check_addresses([value],'remote')
                except ValueError:continue
                addresses.extend(valid)
        return list(dict.fromkeys(addresses))[:11]+['dynamic']

    def unshare(self,task):
        check_task(task)
        with self._configuration:
            peer=self._folders.get(task)
            if peer is None:return
            self.request('DELETE','/rest/config/folders/'+quote(task,safe=''));self._folders.pop(task)
            # Syncthing asynchronously pauses a removed device. Removing and
            # re-adding it between finite jobs can close the next job's live
            # connection. Retain this manually authorized peer for this process;
            # no folder remains shared, and startup clears all old peer records.

    def _stop(self):
        process=self.process
        if process is not None:
            if process.poll() is None:
                try:self.request('POST','/rest/system/shutdown')
                except (OSError,EngineError):pass
                try:process.wait(3)
                except subprocess.TimeoutExpired:
                    if self.tree is not None:self.tree.stop()
                    else:process.kill()
                    process.wait(3)
            self.process=None
        if self.tree is not None:
            tree=self.tree
            if not tree.stopped():tree.stop()
            deadline=time.monotonic()+PROCESS_TREE_STOP_SECONDS
            while not tree.stopped():
                if time.monotonic()>=deadline:raise EngineError('传输内核的子进程尚未退出；保留诊断信息。')
                time.sleep(START_POLL_SECONDS)
            tree.close();self.tree=None
        if self.log is not None:self.log.close();self.log=None
        self.device_id='';self._folders.clear()

    def close(self):
        with self._lock:self._closed=True;self._stop()
