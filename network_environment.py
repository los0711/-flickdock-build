"""Read OS VPN services and system proxy status without changing settings.

Virtual interfaces and utun names never count as proof of a VPN. Unregistered
third-party tunnels are unknown. No service names or proxy credentials persist.
"""
import json,re,subprocess,sys
from PySide6.QtCore import QObject,QRunnable,QThreadPool,QTimer,Signal,Qt,QUrl
from PySide6.QtNetwork import QNetworkProxy,QNetworkProxyFactory,QNetworkProxyQuery
from cloud_config import DEFAULT_CLOUD_URL


def parse_mac_vpn(output):
    states=re.findall(r'^\s*\*?\s*\((Connected|Disconnected|Connecting|Disconnecting)\)\s',output,re.M)
    if 'Connected' in states:return 'connected'
    if states and all(value=='Disconnected' for value in states):return 'disconnected'
    return 'unknown'


def vpn_status(platform=None,runner=subprocess.run):
    platform=platform or sys.platform
    try:
        if platform=='darwin':
            result=runner(['/usr/sbin/scutil','--nc','list'],capture_output=True,text=True,timeout=4)
            return parse_mac_vpn(result.stdout) if result.returncode==0 else 'unknown'
        if platform=='win32':
            script='''$states=@();$failed=$false
foreach($all in @($false,$true)) {
 try { if($all){$items=Get-VpnConnection -AllUserConnection -ErrorAction Stop}else{$items=Get-VpnConnection -ErrorAction Stop}; $states+=@($items|ForEach-Object {$_.ConnectionStatus.ToString()}) } catch {$failed=$true}
}
@{states=@($states);failed=$failed}|ConvertTo-Json -Compress'''
            result=runner(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],
                capture_output=True,text=True,timeout=5,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if result.returncode:return 'unknown'
            data=json.loads(result.stdout);states=data.get('states',[])
            if 'Connected' in states:return 'connected'
            if states and not data.get('failed') and all(value=='Disconnected' for value in states):return 'disconnected'
    except (OSError,ValueError,TypeError,subprocess.TimeoutExpired):pass
    return 'unknown'


def proxy_status():
    try:
        kinds=[proxy.type() for proxy in QNetworkProxyFactory.systemProxyForQuery(QNetworkProxyQuery(QUrl(DEFAULT_CLOUD_URL)))]
        if any(kind in (QNetworkProxy.ProxyType.HttpProxy,QNetworkProxy.ProxyType.Socks5Proxy,
                        QNetworkProxy.ProxyType.HttpCachingProxy) for kind in kinds):return 'enabled'
        if kinds and all(kind==QNetworkProxy.ProxyType.NoProxy for kind in kinds):return 'disabled'
    except (RuntimeError,TypeError):pass
    return 'unknown'


class _Signals(QObject):done=Signal(dict)


class _Probe(QRunnable):
    def __init__(self,detect):super().__init__();self.detect=detect;self.signals=_Signals()
    def run(self):
        try:result=self.detect()
        except Exception:result=dict(vpn='unknown',proxy='unknown')
        self.signals.done.emit(result)


class NetworkEnvironment(QObject):
    changed=Signal(dict)
    def __init__(self,parent=None,*,enabled=True,detect=None):
        super().__init__(parent);self.state=dict(vpn='unknown',proxy='unknown');self.closed=False;self.job=None
        self.detect=detect or (lambda:dict(vpn=vpn_status(),proxy=proxy_status()))
        self.pool=QThreadPool(self);self.pool.setMaxThreadCount(1)
        self.timer=QTimer(self);self.timer.setInterval(5000);self.timer.timeout.connect(self.check)
        if enabled:self.timer.start();QTimer.singleShot(0,self,self.check)

    def check(self):
        if self.closed or self.job is not None:return
        job=_Probe(self.detect);self.job=job
        job.signals.done.connect(self._done,Qt.ConnectionType.QueuedConnection);self.pool.start(job)

    def _done(self,value):
        self.job=None
        if self.closed:return
        if value!=self.state:self.state=dict(value);self.changed.emit(dict(value))

    def close(self):self.closed=True;self.timer.stop()
