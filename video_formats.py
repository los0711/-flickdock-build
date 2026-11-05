"""Expose each downloadable media source with its own stable selection and size."""
import math
import re

VIDEO_EXTENSIONS={'mp4','mkv','mov','m4v','webm','flv','ts','avi','ogv','3gp'}
AUDIO_EXTENSIONS={'mp3','m4a','aac','opus','ogg','flac','wav','wma','weba'}
FORMAT_ID=re.compile(r'[A-Za-z0-9_.:-]{1,160}\Z')

def positive(value):
    return value if type(value) in (int,float) and math.isfinite(value) and value>0 else None

def media_options(info,ydl):
    formats=[row for row in info.get('formats',[]) if isinstance(row,dict) and not row.get('has_drm')]
    if not formats:formats=[info]
    options=[];seen=set()
    audio_only=any(row.get('vcodec')=='none' and row.get('acodec') not in ('none',None) for row in formats)
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
            height=positive(source.get('height'));fps=positive(source.get('fps'))
            rate=positive(source.get('abr'))
            extract_audio=kind=='audio' and video
            codec=str(source.get('vcodec') if kind=='video' else source.get('acodec') or '').split('.')[0]
            codec={'avc1':'H.264','h264':'H.264','hev1':'H.265','hvc1':'H.265','hevc':'H.265','av01':'AV1','vp09':'VP9','mp4a':'AAC'}.get(codec,codec.upper())
            if codec in ('NONE',''):codec='编码未知'
            label=f'{round(height)}p' if kind=='video' and height else '视频' if kind=='video' else '原始音频'
            details=[extension.upper() if not extract_audio else '音频提取',codec]
            if kind=='video' and not audio and not audio_only:details.append('无音轨')
            if kind=='video' and fps:details.append(f'{fps:g} fps')
            if kind=='audio' and rate:details.append(f'{rate:g} kbps')
            tracks=[track for item in ydl._select_formats(formats,ydl.build_format_selector(selector)) for track in item.get('requested_formats',[item])]
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
                extract_audio=extract_audio,height=round(height) if height else None))
    return options
