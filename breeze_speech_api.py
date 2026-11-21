"""Bounded BreezeBlue requests via the app backend, on an independent lane."""
import base64
from collections import OrderedDict
import math
import re
import time
from uuid import uuid4

from PySide6.QtCore import QObject, QThreadPool, QTimer, Qt, Signal
from sync_files import DiskWork
from translation_languages import SPEECH_LANGUAGES


ERRORS={
    'SPEECH_NOT_CONFIGURED':'BreezeBlue 语音服务尚未配置，请先在服务端配置 API 密钥。',
    'SPEECH_VOICE':'所选音色不可用，请重新选择音色。',
    'SPEECH_LANGUAGE':'BreezeBlue 暂不支持这个朗读语言。',
    'SPEECH_PROVIDER_BUSY':'BreezeBlue 当前繁忙，请稍后再试。',
    'SPEECH_REPEATED':'这次语音请求已提交。请稍后手动重试。',
    'SPEECH_INVALID_RESPONSE':'BreezeBlue 返回的音频或时间轴无效，请重试。',
    'SPEECH_PROVIDER':'BreezeBlue 语音生成失败，请检查服务状态后重试。',
    'UNAUTHORIZED':'请先登录 FlickDock 账号，再使用 BreezeBlue 语音。',
}


def decode_audio(value,cancel,*,preview=False):
    if (not isinstance(value,dict) or value.get('provider')!='BreezeBlue'
            or value.get('content_type') not in (('audio/mpeg','audio/wav') if preview else ('audio/mpeg',)) or not isinstance(value.get('audio_base64'),str)
            or len(value['audio_base64'])>1500*1024):raise ValueError('Invalid audio envelope')
    audio=base64.b64decode(value['audio_base64'],validate=True)
    if cancel.is_set():raise InterruptedError()
    if not 2<=len(audio)<=1200*1024 or not (audio.startswith(b'ID3') or audio[0]==255 and audio[1]&224==224 or preview and audio.startswith(b'RIFF') and audio[8:12]==b'WAVE'):
        raise ValueError('Invalid MPEG audio')
    rows=value.get('word_timestamps')
    if not isinstance(rows,list) or len(rows)>2000:raise ValueError('Invalid timing envelope')
    result=[]
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get('word'),str) or len(row['word'])>300:raise ValueError('Invalid word timing')
        start,end=row.get('start'),row.get('end')
        if (type(start) not in (int,float) or type(end) not in (int,float) or not math.isfinite(start) or not math.isfinite(end)
                or start<0 or end<start or end>600):raise ValueError('Invalid word interval')
        result.append(dict(word=row['word'],start=float(start),end=float(end)))
    result.sort(key=lambda row:row['start'])
    return dict(audio=audio,timing=result,starts=[row['start'] for row in result])


class BreezeSpeechApi(QObject):
    voices_ready=Signal(int,object,str)
    completed=Signal(int,object,str)
    reset=Signal()
    def __init__(self,account=None,parent=None,*,transport=None):
        super().__init__(parent);self.account=account;self.transport=transport;self._owns_transport=transport is None
        self.closed=False;self.request=None;self.voice_requests={};self.epoch=0
        self.pool=QThreadPool(self);self.pool.setMaxThreadCount(1);self.jobs=set();self.cache={};self.audio_cache=OrderedDict();self.cache_bytes=0
        if account is not None and hasattr(account,'account_changed'):account.account_changed.connect(self._account_changed)
    def _client(self):
        if self.transport is None:
            from cloud_api import CloudApi
            from cloud_config import DEFAULT_CLOUD_URL
            self.transport=CloudApi(self.account.api.origin if self.account is not None else DEFAULT_CLOUD_URL,self)
        if self.account is not None:self.transport.token=self.account.api.token
        return self.transport
    def fork(self,parent=None):return BreezeSpeechApi(self.account,parent)
    @staticmethod
    def message(error):
        code=str(getattr(error,'code','')).upper()
        return ERRORS.get(code,'语音服务连接失败，请检查网络或服务端配置后重试。')
    def list_voices(self,generation,language):
        if self.closed:return
        if language not in SPEECH_LANGUAGES:raise ValueError('Invalid voice language')
        if language in self.cache and time.monotonic()-self.cache[language][0]<300:
            self.voices_ready.emit(generation,self.cache[language][1],'');return
        client=self._client();self.cancel_voice(generation);self.voice_requests[generation]='starting'
        def done(value,error):
            if self.closed or generation not in self.voice_requests:return
            self.voice_requests.pop(generation,None)
            if error is not None:self.voices_ready.emit(generation,None,self.message(error));return
            if not isinstance(value,dict) or value.get('provider')!='BreezeBlue' or not isinstance(value.get('voices'),list):
                self.voices_ready.emit(generation,None,'音色列表无效，请重试。');return
            voices=[]
            for row in value['voices'][:50]:
                if (isinstance(row,dict) and isinstance(row.get('voice_id'),str) and re.fullmatch(r'[A-Za-z0-9_-]{1,120}',row['voice_id'])
                        and isinstance(row.get('name'),str) and 0<len(row['name'])<=120):voices.append(dict(row))
            payload=dict(voices=voices,pitch_supported=value.get('pitch_supported') is True,
                         default_available=value.get('default_available') is True or bool(voices))
            self.cache[language]=(time.monotonic(),payload);self.voices_ready.emit(generation,payload,'')
        request=client.request('GET','/v1/speech/voices?language='+language,done,priority='high',timeout_ms=20000)
        if generation in self.voice_requests:self.voice_requests[generation]=request
    def cancel_voice(self,generation):
        request=self.voice_requests.pop(generation,None)
        if request and request!='starting' and self.transport:self.transport.cancel(request)
    def synthesize(self,generation,text,language,voice,rate,pitch):
        if self.closed:return
        if not 0<len(text)<=300 or '\0' in text or language not in SPEECH_LANGUAGES:raise ValueError('Speech segment outside bound')
        self.cancel_audio();epoch=self.epoch;client=self._client();self.request='starting'
        cache_key=(text,language,voice,float(rate),float(pitch))
        cached=self.audio_cache.pop(cache_key,None)
        if cached is not None:
            self.audio_cache[cache_key]=cached;self.request=None
            def ready():
                if not self.closed and epoch==self.epoch:self.completed.emit(generation,cached,'')
            QTimer.singleShot(0,self,ready);return
        def done(value,error):
            if self.closed or epoch!=self.epoch:return
            self.request=None
            if error is not None:self.completed.emit(generation,None,self.message(error));return
            job=DiskWork(lambda cancel:decode_audio(value,cancel));self.jobs.add(job)
            def decoded(result,error):
                job.signals.done.disconnect(decoded)
                self.jobs.discard(job)
                if self.closed or epoch!=self.epoch:return
                if error is None:
                    self.audio_cache[cache_key]=result;self.cache_bytes+=len(result['audio'])
                    while len(self.audio_cache)>8 or self.cache_bytes>4*1024*1024:
                        _,previous=self.audio_cache.popitem(last=False);self.cache_bytes-=len(previous['audio'])
                self.completed.emit(generation,result,'' if error is None else '生成的音频或时间轴无效，请重试。')
            job.signals.done.connect(decoded,Qt.QueuedConnection);self.pool.start(job)
        request=client.request('POST','/v1/speech/synthesize',done,priority='high',timeout_ms=110000,
            data=dict(text=text,language=language,voice_id=voice,speed=rate,pitch=pitch,request_id=str(uuid4())))
        if self.request is not None:self.request=request
    def cancel_audio(self):
        self.epoch+=1;request,self.request=self.request,None
        if request and request!='starting' and self.transport:self.transport.cancel(request)
        for job in self.jobs:job.cancel.set()
    def preview(self,generation,voice,language):
        if self.closed:return
        if language not in SPEECH_LANGUAGES or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}',voice):raise ValueError('Invalid preview')
        self.cancel_audio();epoch=self.epoch;client=self._client();self.request='starting'
        def done(value,error):
            if self.closed or epoch!=self.epoch:return
            self.request=None
            if error is not None:self.completed.emit(generation,None,self.message(error));return
            job=DiskWork(lambda cancel:decode_audio(value,cancel,preview=True));self.jobs.add(job)
            def ready(result,error):
                job.signals.done.disconnect(ready);self.jobs.discard(job)
                if self.closed or epoch!=self.epoch:return
                self.completed.emit(generation,result,'' if error is None else '默认试听音频无效，请重试。')
            job.signals.done.connect(ready,Qt.QueuedConnection);self.pool.start(job)
        request=client.request('GET','/v1/speech/preview?language='+language+'&voice_id='+voice,done,priority='high',timeout_ms=25000)
        if self.request is not None:self.request=request
    def _account_changed(self,*args):
        self.cancel_audio()
        for generation in tuple(self.voice_requests):self.cancel_voice(generation)
        self.cache.clear();self.audio_cache.clear();self.cache_bytes=0;self.reset.emit()
    def close(self):
        if self.closed:return
        self._account_changed();self.closed=True;self.pool.clear();self.pool.waitForDone(1500)
        if self.transport is not None and self._owns_transport:self.transport.close()
