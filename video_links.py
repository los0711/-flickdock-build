"""Local URL recognition and bounded prompt deduplication; no network access."""
from collections import OrderedDict
import ipaddress
import re
import time
from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode

MAX_URL_CHARS=8192
PROMPT_REPEAT_SECONDS=120
PROMPT_CACHE_ENTRIES=128
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


class LinkPromptHistory:
    def __init__(self,clock=time.monotonic):self.clock=clock;self.seen=OrderedDict()
    def claim(self,url):
        parts=urlsplit(url)
        query=urlencode([(key,value) for key,value in parse_qsl(parts.query,keep_blank_values=True) if not key.startswith('utm_') and key not in ('fbclid','si')])
        host=parts.netloc.lower().removeprefix('www.').replace('twitter.com','x.com')
        key=urlunsplit((parts.scheme,host,parts.path.rstrip('/'),query,''));now=self.clock()
        previous=self.seen.pop(key,None);self.seen[key]=now
        while len(self.seen)>PROMPT_CACHE_ENTRIES:self.seen.popitem(last=False)
        return previous is None or now-previous>=PROMPT_REPEAT_SECONDS
