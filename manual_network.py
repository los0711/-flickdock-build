"""Fresh per-connection system routes; never changes OS proxy or VPN settings."""
from PySide6.QtNetwork import QNetworkProxy
from network_environment import proxy_routes


def relay_proxy(proxy=None):
    if proxy is None or proxy.type()==QNetworkProxy.DefaultProxy:
        proxy=proxy_routes('https://turn.cloudflare.com:443')[0]
    if proxy.type()==QNetworkProxy.NoProxy:return None
    return dict(kind='http' if proxy.type()==QNetworkProxy.HttpProxy else 'socks5',
                host=proxy.hostName(),port=proxy.port(),username=proxy.user(),password=proxy.password())
