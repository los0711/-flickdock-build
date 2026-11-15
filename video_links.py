"""Local video URL recognition; no network access."""
import ipaddress
import re
from urllib.parse import urlsplit,urlunsplit

MAX_URL_CHARS=8192
MEDIA_PATH=re.compile(r'\.(?:mp4|webm|mov|m4v|mp3|m4a|ogg|opus|m3u8)(?:$|\?)',re.I)


def video_url(value):
    if not isinstance(value,str) or not 1<=len(value)<=MAX_URL_CHARS:raise ValueError('请输入有效的视频网页链接。')
    value=value.strip()
    if any(ord(c)<33 for c in value):raise ValueError('链接中不能包含空白或控制字符。')
    parts=urlsplit(value)
    if parts.scheme not in ('http','https') or not parts.hostname or parts.username is not None or parts.password is not None:
        raise ValueError('仅支持 HTTP 或 HTTPS 视频网页链接。')
    host=parts.hostname.encode('idna').decode('ascii').lower()
    if host in ('localhost','localhost.localdomain') or host.endswith(('.local','.localhost')):raise ValueError('不支持本机或局域网地址。')
    try:address=ipaddress.ip_address(host)
    except ValueError:address=None
    if address is not None and not address.is_global:raise ValueError('不支持本机或局域网地址。')
    port=parts.port
    if port is not None and not 1<=port<=65535:raise ValueError('链接端口无效。')
    return urlunsplit((parts.scheme,parts.netloc,parts.path or '/',parts.query,''))


def candidate_link(text):
    if not isinstance(text,str) or len(text)>65536:return None
    for match in re.finditer(r'https?://[^\s<>"\u3000]+',text):
        try:return video_url(match.group().rstrip('.,;!?)，。；！）'))
        except (ValueError,UnicodeError):continue
    return None


def recognized_video(url,cancel):
    from yt_dlp.extractor import gen_extractor_classes
    if MEDIA_PATH.search(url):return True
    for extractor in gen_extractor_classes():
        if cancel.is_set():return False
        if extractor.IE_NAME!='generic' and not extractor.IE_NAME.startswith(('test','unsupported')) and extractor.suitable(url):return True
    return False

