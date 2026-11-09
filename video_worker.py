"""Isolated yt-dlp extraction/download process. IPC carries bounded JSON lines."""
from pathlib import Path
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import sys
import threading
import time
import urllib.request
from urllib.parse import urlsplit,urljoin

from video_links import video_url
from video_formats import media_options

MAX_RESULTS=20
MAX_DOWNLOAD_BYTES=16*1024**3
MAX_IPC_BYTES=1024*1024
SOCKET_TIMEOUT_SECONDS=15
ROOT=Path(getattr(sys,'_MEIPASS',Path(__file__).parent))
DOWNLOAD_PROTOCOLS={'http','https','m3u8','m3u8_native','http_dash_segments'}
OUTPUT_EXTENSIONS={'.mp4','.mkv','.mov','.m4v','.webm','.flv','.ts','.mp3','.m4a','.aac','.opus','.ogg','.oga','.ogv','.flac','.wav','.wma','.wmv','.avi','.mpeg','.mpg','.3gp'}


def filter_formats(info):
    if info.get('is_live'):return False
    formats=info.get('formats')
    if isinstance(formats,list):
        info['formats']=[row for row in formats if isinstance(row,dict) and row.get('protocol') in DOWNLOAD_PROTOCOLS and not row.get('has_drm')
            and urlsplit(str(row.get('url',''))).scheme in ('http','https')]
        return bool(info['formats'])
    return info.get('protocol') in DOWNLOAD_PROTOCOLS and urlsplit(str(info.get('url',''))).scheme in ('http','https')


def emit(kind,**value):
    sys.stdout.write(json.dumps(dict(type=kind,**value),ensure_ascii=False,separators=(',',':'))+'\n');sys.stdout.flush()


def worker_stdio():
    # A windowed PyInstaller executable deliberately clears Python's streams,
    # while QProcess still supplies real inherited pipe handles.
    if sys.platform=='win32' and any(stream is None for stream in (sys.stdin,sys.stdout,sys.stderr)):
        import ctypes,io,msvcrt
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.GetStdHandle.argtypes=[wintypes.DWORD];kernel.GetStdHandle.restype=wintypes.HANDLE
        for name,identifier,mode,flag in (('stdin',-10,'rb',os.O_RDONLY),('stdout',-11,'wb',os.O_WRONLY),('stderr',-12,'wb',os.O_WRONLY)):
            if getattr(sys,name) is not None:continue
            handle=kernel.GetStdHandle(identifier & 0xffffffff)
            if not handle or handle==wintypes.HANDLE(-1).value:raise OSError('Missing worker pipe: '+name)
            descriptor=msvcrt.open_osfhandle(handle,flag|os.O_BINARY)
            setattr(sys,name,io.TextIOWrapper(os.fdopen(descriptor,mode,buffering=0),encoding='utf-8',write_through=True))
    # Frozen Python ignores PYTHONUTF8 and otherwise uses the Windows ANSI
    # code page even when Qt redirected valid pipes. IPC is always UTF-8.
    for stream in (sys.stdout,sys.stderr):stream.reconfigure(encoding='utf-8',errors='strict',write_through=True)


def public_request(url,allow_local=False,*,proxies=None):
    parts=urlsplit(url)
    if parts.scheme=='data' and len(url)<=MAX_IPC_BYTES:return
    if allow_local and parts.scheme=='http' and parts.hostname=='127.0.0.1':return
    video_url(url)
    from yt_dlp.utils.networking import select_proxy
    if proxies is None:
        proxies=urllib.request.getproxies()
        if 'http' in proxies and 'https' not in proxies:proxies['https']=proxies['http']
    proxy=select_proxy(url,proxies)
    # HTTP CONNECT and remote-DNS SOCKS resolve the destination at the proxy.
    # A local DNS query may instead return the proxy's synthetic IP, or fail
    # entirely. Literal private URLs remain rejected by video_url above.
    if proxy and urlsplit(proxy).scheme.lower() in ('http','https','socks4a','socks5h'):return
    addresses=socket.getaddrinfo(parts.hostname,parts.port or (443 if parts.scheme=='https' else 80),type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError('视频链接指向本机或局域网，已停止请求。')


def media_entries(info):
    pending=[info];count=0
    while pending and count<MAX_RESULTS:
        current=pending.pop(0)
        if not isinstance(current,dict):continue
        if current.get('_type') in ('playlist','multi_video'):
            for entry in current.get('entries') or []:
                if len(pending)+count>=MAX_RESULTS:break
                if entry:pending.append(entry)
        else:
            count+=1;yield current


def downloader(request,progress):
    from yt_dlp import YoutubeDL
    from yt_dlp.networking._urllib import UrllibRH
    allow_local=request.get('fixture_http') is True and os.environ.get('QT_QPA_PLATFORM')=='offscreen'
    class Guard(urllib.request.BaseHandler):
        handler_order=50
        def __init__(self,proxies):self.proxies=proxies
        def http_request(self,req):public_request(req.full_url,allow_local,proxies=self.proxies);return req
        https_request=http_request
    class PublicUrllibRH(UrllibRH):
        def _create_instance(self,proxies,cookiejar,legacy_ssl_support=None):
            opener=super()._create_instance(proxies,cookiejar,legacy_ssl_support)
            opener.add_handler(Guard(proxies));return opener
    class PublicDownloader(YoutubeDL):
        def __init__(self,params):
            super().__init__(params)
            from video_cdn import BilibiliMirrorsIE
            self.add_info_extractor(BilibiliMirrorsIE(self))

        def dl(self,name,info,subtitle=False,test=False):
            from video_cdn import select_cdn
            if info.get('_flickdock_cdn_urls'):
                emit('progress',phase='selecting',bytes=0,total=None,speed=None);select_cdn(self,info)
            return super().dl(name,info,subtitle=subtitle,test=test)

        def build_request_director(self,handlers,preferences=None):
            return super().build_request_director([PublicUrllibRH],preferences)
    class Logger:
        def debug(self,message):pass
        def warning(self,message):emit('warning',message=re.sub(r'\x1b\[[0-9;]*m','',str(message))[:600])
        def error(self,message):pass
    tools=None
    if request['action']=='download':
        from video_runtime import ensure_video_runtime
        tools=ensure_video_runtime(lambda **value:emit('progress',**value))
    node=ROOT/'p2p/runtime'/('node.exe' if sys.platform=='win32' else 'node')
    params=dict(logger=Logger(),quiet=True,no_warnings=False,noprogress=True,noplaylist=True,
        playlistend=MAX_RESULTS,skip_download=False,ignoreerrors=False,socket_timeout=SOCKET_TIMEOUT_SECONDS,
        retries=2,fragment_retries=2,extractor_retries=1,concurrent_fragment_downloads=4,
        cachedir=False,windowsfilenames=True,trim_file_name=160,overwrites=False,
        format='bv*+ba/b',ffmpeg_location=str(tools) if tools else None,js_runtimes={'node':{'path':str(node)}},remote_components=[],
        hls_prefer_native=True,external_downloader={'default':'native'},progress_hooks=[progress],max_filesize=MAX_DOWNLOAD_BYTES,
        postprocessor_hooks=[lambda state:emit('progress',phase='processing',bytes=0,total=None,speed=None)],
        compat_opts={'no-certifi'},enable_file_urls=False)
    return PublicDownloader(params)


def execute(request):
    action=request.get('action')
    if action not in ('extract','download','thumbnail'):raise ValueError('视频任务类型无效。')
    url=request.get('url');allow_local=request.get('fixture_http') is True and os.environ.get('QT_QPA_PLATFORM')=='offscreen'
    public_request(url,allow_local)
    last=[0.]
    def progress(state):
        from transfer_priority import yield_bulk_payload
        if state.get('downloaded_bytes',0)>MAX_DOWNLOAD_BYTES:raise ValueError('下载内容超过 16 GB 限制。')
        if state['status']=='downloading':yield_bulk_payload(threading.Event())
        now=time.monotonic()
        if state['status']=='downloading' and now-last[0]>=.1:
            last[0]=now
            total=state.get('total_bytes') or state.get('total_bytes_estimate')
            emit('progress',phase='downloading',bytes=state.get('downloaded_bytes',0),total=total,speed=state.get('speed'))
        elif state['status']=='finished':emit('progress',phase='processing',bytes=state.get('downloaded_bytes',0),total=None,speed=None)
    with downloader(request,progress) as ydl:
        if action=='thumbnail':
            from video_covers import MAX_COVER_BYTES,normalize_cover
            from yt_dlp.networking import Request
            page=request.get('page_url')
            if page:public_request(page,allow_local)
            with ydl.urlopen(Request(url,headers={'Referer':page} if page else {})) as response:
                body=response.read(MAX_COVER_BYTES+1)
            emit('result',**normalize_cover(body));return
        if action=='extract':
            info=ydl.extract_info(url,download=False);rows=[]
            for entry in media_entries(info):
                if entry.get('has_drm') or not filter_formats(entry):continue
                options=media_options(entry,ydl)
                if not options:continue
                page=entry.get('webpage_url') or entry.get('original_url') or url
                public_request(page,allow_local)
                duration=entry.get('duration')
                rows.append(dict(id=str(entry.get('id',''))[:160],url=page,title=str(entry.get('title') or '未命名视频')[:300],
                    uploader=str(entry.get('uploader') or entry.get('extractor') or '')[:100],
                    duration=duration if type(duration) in (float,int) and math.isfinite(duration) and duration>=0 else None,options=options,
                    thumbnail_url=urljoin(page,str(entry['thumbnail']))[:8192] if entry.get('thumbnail') else ''))
            if not rows:raise ValueError('没有找到可下载的视频或音频。直播、受保护内容或需要登录的链接可能无法解析。')
            emit('result',entries=rows,limited=len(rows)==MAX_RESULTS)
            return
        folder=Path(request['folder'])
        if not folder.is_absolute() or not folder.is_dir() or folder.is_symlink():raise ValueError('下载保存位置无效。')
        token=request.get('token','')
        if not re.fullmatch('[a-f0-9]{32}',token):raise ValueError('下载任务标识无效。')
        stage=folder/('.flickdock-download-'+token);stage.mkdir()
        try:
            info=ydl.extract_info(url,download=False)
            entry=next((entry for entry in media_entries(info) if str(entry.get('id',''))==request['id']),None)
            if entry is None:raise ValueError('视频内容已变化，请重新解析该链接。')
            if not filter_formats(entry):raise ValueError('该视频的下载协议暂不支持，或内容正在直播。')
            option=next((row for row in media_options(entry,ydl) if row['id']==request.get('option')),None)
            if option is None:raise ValueError('该画质已不可用，请重新解析该链接。')
            ydl.params.update(format=option['format'],paths={'home':str(stage)},outtmpl={'default':'%(title).90B [%(id).40B].%(ext)s'})
            if option['extract_audio']:
                from yt_dlp.postprocessor import FFmpegExtractAudioPP
                ydl.add_post_processor(FFmpegExtractAudioPP(ydl,preferredcodec='best'))
            complete=ydl.process_ie_result(entry,download=True)
            candidates=[path for path in stage.iterdir() if path.is_file() and not path.is_symlink() and path.suffix not in ('.part','.ytdl','.json','.temp')]
            if len(candidates)!=1 or candidates[0].stat().st_size==0:raise ValueError('下载未生成完整文件，请重试。')
            if candidates[0].suffix.lower() not in OUTPUT_EXTENSIONS:raise ValueError('下载结果不是支持的音视频文件。')
            from selection_archive import _publish_part
            path=_publish_part(candidates[0],folder/candidates[0].name,None)
            emit('result',path=str(path),bytes=path.stat().st_size)
        finally:shutil.rmtree(stage)


def run():
    worker_stdio()
    try:
        stream=getattr(sys.stdin.buffer,'raw',sys.stdin.buffer)
        raw=stream.readline(MAX_IPC_BYTES+1)
        if len(raw)>MAX_IPC_BYTES:raise ValueError('视频任务超过限制。')
        request=json.loads(raw)
        if not isinstance(request,dict):raise ValueError('视频任务格式无效。')
        def controls():
            from transfer_priority import set_clipboard_priority
            while line:=stream.readline(257):
                if len(line)>256:return
                try:value=json.loads(line)
                except (ValueError,UnicodeError):return
                if isinstance(value,dict) and type(value.get('clipboard_priority')) is bool:set_clipboard_priority(value['clipboard_priority'])
        threading.Thread(target=controls,daemon=True,name='FlickDock-video-controls').start()
        execute(request);return 0
    except Exception as error:
        message=re.sub(r'\x1b\[[0-9;]*m','',str(error)).strip()
        emit('error',message=(message or type(error).__name__)[:1000]);return 1


if __name__=='__main__':raise SystemExit(run())
