"""Keep Bilibili's supplied mirror URLs and measure them before native download."""
from concurrent.futures import ThreadPoolExecutor
import time
from urllib.parse import urlsplit
from yt_dlp.extractor.bilibili import BiliBiliIE
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import RequestError

CDN_PROBE_BYTES=128*1024
CDN_PROBE_TIMEOUT_SECONDS=3
CDN_MAX_CANDIDATES=3
BILIBILI_HTTP_CHUNK_BYTES=8*1024*1024

class BilibiliMirrorsIE(BiliBiliIE):
    @classmethod
    def ie_key(cls):return BiliBiliIE.ie_key()

    def extract_formats(self,play_info):
        formats=super().extract_formats(play_info);dash=play_info.get('dash') or {}
        tracks=[*(dash.get('video') or []),*(dash.get('audio') or []),*((dash.get('dolby') or {}).get('audio') or [])]
        if (dash.get('flac') or {}).get('audio'):tracks.append(dash['flac']['audio'])
        mirrors={}
        for track in tracks:
            base=track.get('baseUrl') or track.get('base_url') or track.get('url')
            urls=track.get('backupUrl') or track.get('backup_url') or []
            if isinstance(base,str) and isinstance(urls,list):
                mirrors[base]=[url for url in urls if isinstance(url,str) and url.startswith('https://')][:CDN_MAX_CANDIDATES-1]
        for item in formats:
            if mirrors.get(item.get('url')):item['_flickdock_cdn_urls']=[item['url'],*mirrors[item['url']]]
        return formats

def select_cdn(ydl,info):
    urls=info.get('_flickdock_cdn_urls')
    if not urls:return
    def probe(url):
        started=time.monotonic();received=0;error=''
        try:
            headers=dict(info.get('http_headers') or {},Range=f'bytes=0-{CDN_PROBE_BYTES-1}')
            with ydl.urlopen(Request(url,headers=headers,extensions={'timeout':CDN_PROBE_TIMEOUT_SECONDS})) as response:
                while received<CDN_PROBE_BYTES and time.monotonic()-started<CDN_PROBE_TIMEOUT_SECONDS:
                    block=response.read(min(32768,CDN_PROBE_BYTES-received))
                    if not block:break
                    received+=len(block)
        except (OSError,RequestError,ValueError) as exception:
            # A failed probe is an actual measurement failure; the native
            # downloader retains its own diagnostics, retry and resume rules.
            error=type(exception).__name__
        elapsed=max(.001,time.monotonic()-started)
        return dict(url=url,score=received/elapsed,bytes=received,seconds=elapsed,error=error)
    with ThreadPoolExecutor(max_workers=CDN_MAX_CANDIDATES) as pool:measurements=list(pool.map(probe,urls))
    successful=[row for row in measurements if row['bytes'] and not row['error']]
    if successful:
        winner=max(successful,key=lambda row:row['score']);info['url']=winner['url']
        ydl.to_screen('Selected supplied Bilibili CDN: '+str(urlsplit(winner['url']).hostname))
    else:ydl.report_warning('B站线路检测未取得有效样本：'+', '.join(row['error'] or 'empty response' for row in measurements))
    info.setdefault('downloader_options',{})['http_chunk_size']=BILIBILI_HTTP_CHUNK_BYTES
