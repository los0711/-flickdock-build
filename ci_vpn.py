"""Bounded system-proxy QA on disposable hosted runners, never a user's PC."""
from __future__ import annotations
import argparse
import ctypes
import gzip
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

VERSION='v1.19.33'
ASSETS={
    'win32':('mihomo-windows-amd64-v1-v1.19.33.zip',22543530,'fb0b960eff568459502d4ebe53e9cca203053d86ec9024fe935f19615d74d9aa'),
    'darwin':('mihomo-darwin-arm64-v1.19.33.gz',20784157,'060b64b0f2c9a310831db0fe4adb2878de67bfa606c311813c1f13966aa504b6'),
}


def guard():
    if (os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('GITHUB_REPOSITORY')!='los0711/-flickdock-build'
            or sys.platform not in ASSETS):raise RuntimeError('VPN QA requires an authorized disposable runner')


def prepare(root):
    guard();name,size,digest=ASSETS[sys.platform];archive=root/name
    with urllib.request.urlopen('https://github.com/MetaCubeX/mihomo/releases/download/'+VERSION+'/'+name,timeout=90) as response:
        body=response.read(size+1)
    if len(body)!=size or hashlib.sha256(body).hexdigest()!=digest:raise ValueError('Proxy core archive verification failed')
    archive.write_bytes(body);binary=root/('mihomo.exe' if sys.platform=='win32' else 'mihomo')
    if name.endswith('.zip'):
        with zipfile.ZipFile(archive) as package:
            members=[row for row in package.infolist() if not row.is_dir() and row.filename.endswith('.exe')]
            if len(members)!=1 or members[0].file_size>100*1024*1024:raise ValueError('Invalid proxy core archive')
            binary.write_bytes(package.read(members[0]))
    else:
        with gzip.open(archive,'rb') as stream:binary.write_bytes(stream.read(100*1024*1024))
    binary.chmod(0o700);archive.unlink();return binary


def free_port():
    with socket.socket() as stream:stream.bind(('127.0.0.1',0));return stream.getsockname()[1]


class SystemProxy:
    def __init__(self,port):self.port=port;self.saved=[]

    @staticmethod
    def network(*arguments):
        result=subprocess.run(['sudo','-n','networksetup',*arguments],capture_output=True,text=True,timeout=15)
        if result.returncode:raise RuntimeError('Disposable system proxy configuration failed')
        return result.stdout

    @staticmethod
    def notify():
        internet=ctypes.WinDLL('wininet')
        for flag in (39,37):
            if not internet.InternetSetOptionW(None,flag,None,0):raise ctypes.WinError()

    def enable(self):
        if sys.platform=='win32':
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r'Software\Microsoft\Windows\CurrentVersion\Internet Settings',0,winreg.KEY_QUERY_VALUE|winreg.KEY_SET_VALUE) as key:
                for name,value,kind in (('ProxyEnable',1,winreg.REG_DWORD),('ProxyServer',f'127.0.0.1:{self.port}',winreg.REG_SZ),('ProxyOverride','<local>;127.0.0.1;localhost',winreg.REG_SZ)):
                    try:old=winreg.QueryValueEx(key,name)
                    except FileNotFoundError:old=None
                    self.saved.append((name,old));winreg.SetValueEx(key,name,0,kind,value)
            self.notify()
        else:
            services=self.network('-listallnetworkservices').splitlines()[1:]
            for service in services:
                if not service or service.startswith('*'):continue
                for kind in ('webproxy','securewebproxy'):
                    status=dict(line.split(': ',1) for line in self.network('-get'+kind,service).splitlines() if ': ' in line)
                    if status.get('Authenticated Proxy Enabled')=='1':raise RuntimeError('Hosted runner has an unexpected authenticated proxy')
                    self.saved.append((service,kind,status))
                    self.network('-set'+kind,service,'127.0.0.1',str(self.port))
                    self.network('-set'+kind+'state',service,'on')
            if not self.saved:raise RuntimeError('Hosted runner has no system proxy service')

    def restore(self):
        if sys.platform=='win32':
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r'Software\Microsoft\Windows\CurrentVersion\Internet Settings',0,winreg.KEY_SET_VALUE) as key:
                for name,old in reversed(self.saved):
                    if old is None:
                        try:winreg.DeleteValue(key,name)
                        except FileNotFoundError:pass
                    else:winreg.SetValueEx(key,name,0,old[1],old[0])
            self.notify()
        else:
            for service,kind,status in reversed(self.saved):
                host=status.get('Server','');port=status.get('Port','0')
                if host and port!='0':self.network('-set'+kind,service,host,port)
                self.network('-set'+kind+'state',service,'on' if status.get('Enabled')=='Yes' else 'off')


def verify_qt_proxy():
    """Fail before a test can silently bypass the selected proxy."""
    port=os.environ.get('FD_QA_PROXY_PORT')
    if port is None:return dict(mode='off')
    from PySide6.QtCore import QUrl
    from PySide6.QtNetwork import QNetworkProxyFactory,QNetworkProxyQuery,QNetworkProxy
    QNetworkProxyFactory.setUseSystemConfiguration(True)
    proxies=QNetworkProxyFactory.proxyForQuery(QNetworkProxyQuery(QUrl('https://flickdock.cc.cd/v1/health')))
    if not any(p.type()==QNetworkProxy.ProxyType.HttpProxy and p.hostName()=='127.0.0.1' and p.port()==int(port) for p in proxies):
        raise RuntimeError('Native Qt did not select the isolated system proxy')
    return dict(mode='system_http_proxy',qt_proxy_verified=True,tun=False)


def main():
    guard();parser=argparse.ArgumentParser();parser.add_argument('command',choices=('prepare','run'));parser.add_argument('child',nargs=argparse.REMAINDER);args=parser.parse_args()
    root=Path(os.environ['RUNNER_TEMP'])/'flickdock-vpn';root.mkdir(exist_ok=True)
    if args.command=='prepare':prepare(root);print(json.dumps(dict(ok=True,version=VERSION)));return 0
    child=args.child[1:] if args.child[:1]==['--'] else args.child
    if not child:raise ValueError('Missing QA command')
    nodes=json.loads(os.environ.pop('FLICKDOCK_QA_VPN_NODE'))
    if not isinstance(nodes,list) or not 1<=len(nodes)<=4:raise ValueError('Invalid private QA nodes')
    for index,node in enumerate(nodes):
        if not isinstance(node,dict) or node.get('type') not in ('vless','trojan'):raise ValueError('Invalid private QA node')
        node['name']='private-qa-'+str(index+1)
        for key in ('dialer-proxy','interface-name','routing-mark'):node.pop(key,None)
    mixed=free_port();controller=free_port();token=secrets.token_hex(32)
    config={'mixed-port':mixed,'allow-lan':False,'bind-address':'127.0.0.1','mode':'rule','log-level':'silent','ipv6':False,
            'external-controller':f'127.0.0.1:{controller}','secret':token,'tun':{'enable':False},'dns':{'enable':False},
            'geo-auto-update':False,'find-process-mode':'off','global-client-fingerprint':'chrome','proxies':nodes,
            'proxy-groups':[{'name':'QA','type':'select','proxies':[node['name'] for node in nodes]}],'rules':['MATCH,QA']}
    local=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def api(path,data=None):
        request=urllib.request.Request(f'http://127.0.0.1:{controller}'+path,method='PUT' if data is not None else 'GET',
            data=json.dumps(data).encode() if data is not None else None,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        with local.open(request,timeout=5) as response:
            value=response.read(1024*1024);return json.loads(value) if value else None
    process=None;system=SystemProxy(mixed);report=dict(ok=False,mode='system_http_proxy',tun=False,platform=sys.platform)
    temporary=tempfile.TemporaryDirectory(prefix='profile-',dir=root)
    try:
        directory=temporary.name;path=Path(directory)/'config.json';path.write_text(json.dumps(config),encoding='utf8');path.chmod(0o600)
        process=subprocess.Popen([str(root/('mihomo.exe' if sys.platform=='win32' else 'mihomo')),'-d',directory,'-f',str(path)],
                                 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        deadline=time.monotonic()+15
        while True:
            if process.poll() is not None:raise RuntimeError('Isolated proxy core failed to start')
            try:api('/version');break
            except OSError:
                if time.monotonic()>deadline:raise TimeoutError('Isolated proxy core did not become ready')
                time.sleep(.05)
        report['node_health']=[]
        for node in nodes:
            api('/proxies/QA',{'name':node['name']})
            connection=http.client.HTTPSConnection('127.0.0.1',mixed,timeout=10,context=ssl.create_default_context())
            started=time.monotonic();result=dict(alias=node['name'],ok=False)
            try:
                connection.set_tunnel('flickdock.cc.cd',443)
                connection.request('GET','/v1/health',headers={'User-Agent':'FlickDock/1.8.25 (CloudSync)'})
                response=connection.getresponse();response.read(4096)
                result.update(status=response.status,ok=response.status==200)
            except (OSError,http.client.HTTPException) as error:result['error_type']=type(error).__name__
            finally:connection.close()
            result['seconds']=round(time.monotonic()-started,3);report['node_health'].append(result)
            if result['ok']:break
        else:raise RuntimeError('Private nodes failed the service health check')
        system.enable();before=api('/connections');env=dict(os.environ,FD_QA_PROXY_PORT=str(mixed))
        code=subprocess.run(child,env=env,timeout=1500).returncode
        after=api('/connections');report.update(child_exit=code,upload_bytes=max(0,after['uploadTotal']-before['uploadTotal']),
            download_bytes=max(0,after['downloadTotal']-before['downloadTotal']))
        report['ok']=code==0 and report['upload_bytes']+report['download_bytes']>0
        if code==0 and not report['ok']:raise RuntimeError('No traffic passed through the selected node')
        return code if code else (0 if report['ok'] else 1)
    except Exception as error:report['error_type']=type(error).__name__;return 1
    finally:
        try:system.restore();report['system_proxy_restored']=True
        finally:
            if process is not None:
                process.terminate()
                try:process.wait(5)
                except subprocess.TimeoutExpired:process.kill();process.wait(5)
            temporary.cleanup()
            (root/'proxy-report.json').write_text(json.dumps(report,indent=2),encoding='utf8')
            print(json.dumps(report),flush=True)


if __name__=='__main__':raise SystemExit(main())
