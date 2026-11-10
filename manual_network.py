"""Fresh per-connection system routes; never changes OS proxy or VPN settings."""
from PySide6.QtCore import QUrl
from PySide6.QtNetwork import QNetworkProxy,QNetworkProxyFactory,QNetworkProxyQuery


def signaling_proxies(url):
    result=[]
    for proxy in QNetworkProxyFactory.systemProxyForQuery(QNetworkProxyQuery(QUrl(url))):
        if proxy.type() not in (QNetworkProxy.HttpProxy,QNetworkProxy.Socks5Proxy,QNetworkProxy.NoProxy):continue
        if proxy not in result:result.append(proxy)
    direct=QNetworkProxy(QNetworkProxy.NoProxy)
    if direct not in result:result.append(direct)
    return result


def relay_proxy(proxy=None):
    if proxy is None or proxy.type()==QNetworkProxy.DefaultProxy:
        proxy=signaling_proxies('https://turn.cloudflare.com:443')[0]
    if proxy.type()==QNetworkProxy.NoProxy:return None
    return dict(kind='http' if proxy.type()==QNetworkProxy.HttpProxy else 'socks5',
                host=proxy.hostName(),port=proxy.port(),username=proxy.user(),password=proxy.password())
