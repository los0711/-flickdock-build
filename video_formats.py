"""Expose each downloadable media source with its own stable selection and size."""
import math
import re
import sys

VIDEO_EXTENSIONS={'mp4','mkv','mov','m4v','webm','flv','ts','avi','ogv','3gp'}
AUDIO_EXTENSIONS={'mp3','m4a','aac','opus','ogg','flac','wav','wma','weba'}
FORMAT_ID=re.compile(r'[A-Za-z0-9_.:-]{1,160}\Z')

def _is_mac():return sys.platform=='darwin'

def positive(value):
    return value if type(value) in (int,float) and math.isfinite(value) and value>0 else None

def media_options(info,ydl):
    formats=[row for row in info.get('formats',[]) if isinstance(row,dict) and not row.get('has_drm')]
    if not formats:formats=[info]
    options=[];seen=set()
    audio_only=any(row.get('vcodec')=='none' and row.get('acodec') not in ('none',None) for row in formats)
    aac_available=any(row.get('vcodec')=='none' and str(row.get('acodec') or '').lower().split('.')[0] in ('mp4a','aac') for row in formats)
    for source in reversed(formats):
        identifier=str(source.get('format_id',''))
        if not FORMAT_ID.fullmatch(identifier):
            raise ValueError('视频来源的格式标识无效，无法安全选择下载源。')
        extension=str(source.get('ext','')).lower()
        video=source.get('vcodec')!='none' and (source.get('vcodec') is not None or extension in VIDEO_EXTENSIONS)
        audio=source.get('acodec')!='none' and (source.get('acodec') is not None or extension in VIDEO_EXTENSIONS|AUDIO_EXTENSIONS)
        kinds=['video'] if video else ['audio'] if audio else []
        if video and audio and not audio_only:kinds.append('audio')
        for kind in kinds:
            key=(kind,identifier)
            if key in seen:continue
            seen.add(key)
            selector=identifier+('+ba' if kind=='video' and not audio and audio_only else '')
            video_codec=str(source.get('vcodec') or '').lower().split('.')[0]
            if _is_mac() and kind=='video' and video_codec in ('avc1','h264','hvc1','hev1','hevc') and not audio and aac_available:
                selector=identifier+'+(ba[acodec^=mp4a]/ba[acodec=aac]/ba)'
            height=positive(source.get('height'));fps=positive(source.get('fps'))
            rate=positive(source.get('abr'))
            extract_audio=kind=='audio' and video
            codec=str(source.get('vcodec') if kind=='video' else source.get('acodec') or '').split('.')[0]
            codec={'avc1':'H.264','h264':'H.264','hev1':'H.265','hvc1':'H.265','hevc':'H.265','av01':'AV1','vp09':'VP9','mp4a':'AAC'}.get(codec,codec.upper())
            if codec in ('NONE',''):codec='编码未知'
            label=f'{round(height)}p' if kind=='video' and height else '视频' if kind=='video' else '原始音频'
            details=[extension.upper() if not extract_audio else '音频提取',codec]
            if selector!=identifier+('+ba' if kind=='video' and not audio and audio_only else ''):details.append('AAC 音轨')
            if sys.platform=='darwin' and kind=='video' and codec in ('AV1','VP9','VP8'):
                details.append('下载后转为 H.264')
            if kind=='video' and not audio and not audio_only:details.append('无音轨')
            if kind=='video' and fps:details.append(f'{fps:g} fps')
            if kind=='audio' and rate:details.append(f'{rate:g} kbps')
            tracks=[track for item in ydl._select_formats(formats,ydl.build_format_selector(selector)) for track in item.get('requested_formats',[item])]
            audio_codecs=[str(track.get('acodec') or '').lower().split('.')[0] for track in tracks if track.get('acodec') not in ('none',None)]
            pixel_format=str(source.get('pix_fmt') or '').lower()
            fast_video=(video_codec in ('avc1','h264') and pixel_format in ('','yuv420p','yuvj420p')) or (_is_mac() and video_codec=='hvc1' and pixel_format in ('','yuv420p','yuvj420p','yuv420p10le'))
            recommended=kind=='video' and extension in ('mp4','m4v','mov') and fast_video and bool(audio_codecs) and all(code in ('aac','mp4a','mp3','alac') for code in audio_codecs)
            sizes=[];approximate=extract_audio
            for track in tracks:
                exact=positive(track.get('filesize'));size=exact or positive(track.get('filesize_approx'))
                if not size:sizes=[];break
                sizes.append(round(size));approximate|=not bool(exact)
            size=sum(sizes) if sizes else None
            if extract_audio:
                duration=positive(info.get('duration'))
                size=round(duration*rate*1000/8) if duration and rate else None
            options.append(dict(id=('v:' if kind=='video' else 'a:')+identifier,label=label,kind=kind,
                details=' · '.join(details),format=selector,bytes=size,approximate=approximate,
                extract_audio=extract_audio,height=round(height) if height else None,recommended=recommended))
    return sorted(options,key=lambda row:(row['kind']!='video',not row['recommended']))
