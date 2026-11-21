"""BreezeBlue TTS + audio-only Qt playback, bounded to one prefetched segment.

Playback progress comes from the audio device, not an estimated completion
timer. A stopped generation cannot attach a late audio result to a new one.
"""
from bisect import bisect_right
from pathlib import Path
import re

from translation_languages import voice_language_matches
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, Qt, QTimer, QUrl, Signal


def split_speech_text(text,maximum=300):
    parts=[];position=0
    while position<len(text):
        end=min(len(text),position+maximum)
        if end<len(text):
            candidate=max(text.rfind(char,position,end) for char in '\n。！？!?；;.')
            if candidate>=position+maximum//3:end=candidate+1
            else:
                space=text.rfind(' ',position,end)
                if space>=position+maximum//2:end=space+1
        parts.append(text[position:end]);position=end
    return parts


class TranslatorSpeech(QObject):
    END_HOLD_MS=1000
    error=Signal(str)
    changed=Signal()
    voices_changed=Signal()
    def __init__(self,parent=None,*,api=None,player_factory=None):
        super().__init__(parent)
        if api is None:
            from breeze_speech_api import BreezeSpeechApi
            api=BreezeSpeechApi(parent=self)
        self.api=api;self.player_factory=player_factory;self.player=None;self.output=None
        self.side=None;self.text='';self.active=False;self.paused=False;self.progress=None;self.segment_progress=0.0;self.word='';self.phase='idle'
        self.rate=1.;self.pitch=1.;self.voice_key='';self.language='en';self.catalog=[];self.pitch_supported=False
        self.epoch=0;self.voice_epoch=0;self.closed=False;self.parts=[];self.index=0;self.ready={};self.inflight=None;self.current=None
        self._device=None;self._retired=[];self._switching=False;self._options=None
        self._catalogs={};self._pitch_support={};self._voice_jobs={};self._browse_language=None
        self._export=None;self._export_epoch=0
        self.audition_key='';self._durations={};self._seek_fraction=None
        self.finished=False;self.completed_side=None;self.completed_text='';self.completed_duration=0.
        self._pump=QTimer(self);self._pump.setSingleShot(True);self._pump.timeout.connect(self._play_ready)
        self._end_hold=QTimer(self);self._end_hold.setSingleShot(True)
        self._end_hold.setTimerType(Qt.PreciseTimer)
        self._end_hold.timeout.connect(lambda:self.stop(completed=True))
        self._tail_remaining=0
        self._caption_spans=()
        self._caption_starts=()
        self._caption_word=-1
        api.voices_ready.connect(self._voices);api.completed.connect(self._generated);api.reset.connect(self._account_reset)
    @staticmethod
    def _qt():
        from PySide6.QtMultimedia import QMediaPlayer
        return QMediaPlayer
    def ensure_engine(self,language=None):
        # Kept as the small UI-facing preparation API: this prepares the
        # Breeze catalog, never constructs a local speech/model backend.
        if self.closed:return False
        language=language or self.language
        self._browse_language=language
        self.catalog=self._catalogs.get(language,[]);self.pitch_supported=self._pitch_support.get(language,False);self.voices_changed.emit()
        self._prepare_catalog(language,'browse');return True

    def browse_language(self, language):
        """Clear a stale catalog while source-language detection is pending."""
        if language is None:
            self._browse_language=None
            self.catalog=[];self.pitch_supported=False
            for token,(purpose,_) in tuple(self._voice_jobs.items()):
                if purpose=='browse':
                    self.api.cancel_voice(token)
                    self._voice_jobs.pop(token,None)
            self.voices_changed.emit()
        else:
            self.ensure_engine(language)

    def catalog_for(self, language):
        return self._catalogs.get(language)
    def _prepare_catalog(self,language,purpose):
        if purpose=='play' and language in self._catalogs:
            if self._catalogs[language]:self._request_next()
            else:self._fail('当前语言没有可用的 BreezeBlue 音色，请更换语言或音色。')
            return
        for token,(kind,code) in tuple(self._voice_jobs.items()):
            if kind!=purpose:continue
            if code==language:return
            self.api.cancel_voice(token);self._voice_jobs.pop(token,None)
        self.voice_epoch+=1;token=self.voice_epoch;self._voice_jobs[token]=(purpose,language)
        self.api.list_voices(token,language)
    def _voices(self,epoch,value,error):
        job=self._voice_jobs.pop(epoch,None)
        if self.closed or job is None:return
        purpose,language=job
        if error:
            if purpose=='play':self._fail(error)
            else:self.error.emit(error)
            return
        self._catalogs[language]=[dict(key=row['voice_id'],name=row['name'],meta=dict(row)) for row in value['voices']
            if voice_language_matches(language,row.get('language_code'))]
        self._pitch_support[language]=value.get('pitch_supported') is True
        if self._browse_language==language:
            self.catalog=self._catalogs[language];self.pitch_supported=self._pitch_support[language];self.voices_changed.emit()
        if self.active and self.language==language:
            if not self._catalogs[language]:self._fail('当前语言没有可用的 BreezeBlue 音色，请更换语言或音色。')
            else:self._request_next()
    def configure(self,*,rate=1.,pitch=1.,voice=''):
        self.rate=max(.5,min(2.,float(rate)));self.pitch=max(.8,min(1.2,float(pitch)));self.voice_key=str(voice)
        # A coherent utterance uses one immutable option snapshot. Controls
        # affect the next reading, without restarting paid synthesis mid-clip.
    def speak(self,text,language,side='target',*,force=False,voice=None):
        if self.closed or not text.strip():return
        if not force and self.active and self.side==side:self.stop();return
        self.stop();self.text=text;self.side=side;self.language=language;self.parts=[part for part in split_speech_text(text) if part.strip()];self._durations={}
        self.active=True;self.paused=False;self.phase='generating';self.index=0;self.ready={};self.current=None
        selected=self.voice_key if voice is None else str(voice)
        self._options=dict(rate=self.rate,pitch=self.pitch,voice=selected)
        self.changed.emit()
        self._prepare_catalog(language,'play')
    def export_audio(self,path,text,language,callback=None,*,voice=None):
        if self.closed or not text.strip():return False
        if self._export is None:
            from speech_export import SpeechExporter
            self._export=SpeechExporter(self.api.fork(self),self);self._export.error.connect(self.error)
        selected=self.voice_key if voice is None else voice
        catalog=self.catalog_for(language)
        if catalog==[]:
            self.error.emit('当前语言没有可用的 BreezeBlue 音色。')
            return False
        choices=[row['key'] for row in catalog or ()]
        voice=selected if catalog is None or selected in choices else choices[0]
        return self._export.start(path,[part for part in split_speech_text(text) if part.strip()],language,voice,self.rate,
            self.pitch if self._pitch_support.get(language,False) else 1.,callback)

    def _request_next(self):
        if not self.active or self.closed or self.inflight is not None or self.language not in self._catalogs:return
        # No more than current + one prefetched segment can exist in memory.
        wanted=next((index for index in range(self.index,min(len(self.parts),self.index+2))
                     if index not in self.ready and not (index==self.index and self.current is not None)),None)
        if wanted is None:return
        self.inflight=wanted;choices=[row['key'] for row in self._catalogs[self.language]]
        selected=self._options['voice']
        voice=selected if selected in choices else choices[0]
        pitch=self._options['pitch'] if self._pitch_support.get(self.language,False) else 1.
        self.api.synthesize(self.epoch,self.parts[wanted],self.language,voice,self._options['rate'],pitch)
    def _generated(self,epoch,value,error):
        if self.closed or not self.active or epoch!=self.epoch or self.inflight is None:return
        index,self.inflight=self.inflight,None
        if error:self._fail(error);return
        timing=value.get('timing',[])
        if timing:self._durations[index]=timing[-1]['end']
        self.ready[index]=value
        if self.current is None and not self.paused:self._pump.start(0)
        else:self._request_next()
    def _ensure_player(self):
        if self.player is not None:return
        if self.player_factory:self.player,self.output=self.player_factory(self)
        else:
            from PySide6.QtMultimedia import QAudioOutput,QMediaPlayer
            self.output=QAudioOutput(self);self.player=QMediaPlayer(self);self.player.setAudioOutput(self.output)
        self.player.positionChanged.connect(self._position);self.player.durationChanged.connect(lambda value:self._position(self.player.position()))
        self.player.mediaStatusChanged.connect(self._media_status);self.player.errorOccurred.connect(self._player_error)
    def _play_ready(self):
        if not self.active or self.paused or self.current is not None or self.index not in self.ready:return
        self._ensure_player();self.current=self.ready.pop(self.index);self._switching=True
        self._prepare_caption()
        try:
            self._detach_device();self._device=QBuffer(self);self._device.setData(QByteArray(self.current['audio']));self._device.open(QIODevice.ReadOnly)
            self.player.setSourceDevice(self._device,QUrl('breeze-speech.mp3'));self.phase='playing'
            if self._seek_fraction is not None and self.player.duration()>0:
                self.player.setPosition(round(self.player.duration()*self._seek_fraction));self._seek_fraction=None
            self.player.play()
        finally:self._switching=False
        self.changed.emit();self._request_next()
    def _detach_device(self):
        if self._device is not None:self._retired.append(self._device);self._device=None
        if self.player is not None:self.player.stop();self.player.setSource(QUrl())
    def _drain_retired(self):
        retired,self._retired=self._retired,[]
        for device in retired:device.close();device.deleteLater()
    def _media_status(self,status):
        media=self._qt()
        if status in (media.MediaStatus.NoMedia,media.MediaStatus.LoadedMedia):self._drain_retired()
        if self._switching or not self.active:return
        if status==media.MediaStatus.EndOfMedia and self.phase=='playing':
            if self.index+1==len(self.parts):
                if self.side=='audition':self.stop(completed=True);return
                self.phase='ending';self.progress=self.segment_progress=1.
                self._caption_word=len(self._caption_spans)-1
                self._tail_remaining=self.END_HOLD_MS
                if not self.paused:self._end_hold.start(self._tail_remaining)
                self.changed.emit()
                return
            self.current=None;self.index+=1
            self.phase='generating';self.word='';self.changed.emit();self._pump.start(0);self._request_next()
    def _position(self,position):
        if not self.active or self.current is None or self.player is None or self.phase=='ending':return
        duration=self.player.duration()
        if duration>0:
            self.segment_progress=min(1.,max(0.,position/duration))
            prefix=sum(len(part) for part in self.parts[:self.index]);total=sum(len(part) for part in self.parts)
            self.progress=min(1.,(prefix+len(self.parts[self.index])*self.segment_progress)/max(1,total))
        timing=self.current['timing'];index=bisect_right(self.current['starts'],position/1000)-1
        self.word=timing[index]['word'] if 0<=index<len(timing) and timing[index]['end']>=position/1000 else ''
        self._caption_word=bisect_right(self._caption_starts,position/1000)-1
        self.changed.emit()
    def audition(self,voice):
        """Use the voice's native preview; fallback never reads editor content."""
        row=next((r for r in self.catalog if r['key']==voice),None) or next((r for r in self.catalog),None)
        if row is None:return
        language=self._browse_language or 'en'
        self.audition_key=row['key']
        if row.get('meta',{}).get('has_preview') and hasattr(self.api,'preview'):
            self.stop();self.audition_key=row['key'];self.side='audition';self.text='';self.language=language;self.parts=[''];self.index=0
            self.active=True;self.phase='generating';self.inflight=0;self._durations={};self.changed.emit()
            self.api.preview(self.epoch,row['key'],language);return
        sample={'zh':'你好，这是这个音色的试听。','zh-Hant':'你好，這是這個音色的試聽。','en':'Hello, this is a voice preview.',
                'ja':'こんにちは、音声サンプルです。','ko':'안녕하세요. 음성 미리 듣기입니다.',
                'fr':'Bonjour, voici un aperçu de cette voix.','de':'Hallo, das ist eine Hörprobe.',
                'es':'Hola, esta es una muestra de voz.'}.get(row.get('meta',{}).get('language_code') or language,'Hello, this is a voice preview.')
        rate,pitch=self.rate,self.pitch;self.rate=self.pitch=1.
        try:self.speak(sample,language,'audition',force=True,voice=row['key'])
        finally:self.rate,self.pitch=rate,pitch
        self.audition_key=row['key']

    def seek(self,fraction):
        """Jump to a fraction of the spoken text, within or across segments."""
        if not self.active or not self.parts:return False
        fraction=max(0.,min(1.,float(fraction)))
        total=sum(len(part) for part in self.parts) or 1
        target=fraction*total;consumed=0;index=len(self.parts)-1;offset=0.
        for position,part in enumerate(self.parts):
            if target<=consumed+len(part):
                index=position;offset=(target-consumed)/max(1,len(part));break
            consumed+=len(part)
        if index==self.index and self.current is not None and self.player is not None:
            duration=self.player.duration()
            if duration>0:
                was_ending=self.phase=='ending'
                self._end_hold.stop();self._tail_remaining=0;self.phase='playing'
                self.player.setPosition(round(duration*offset))
                if was_ending and not self.paused:self.player.play()
                self.progress=fraction;self.changed.emit();return True
        self._end_hold.stop();self._tail_remaining=0
        self.index=index;self.ready={key:value for key,value in self.ready.items() if key>=index}
        self.current=None;self.phase='generating';self.progress=fraction;self.paused=False;self._seek_fraction=offset
        self._switching=True
        try:self._detach_device()
        finally:self._switching=False
        self.changed.emit();self._pump.start(0);self._request_next();return True

    @property
    def total_duration(self):
        """Seconds of every segment already synthesized (a running total)."""
        return sum(self._durations.values())

    def _prepare_caption(self):
        """Map provider timings to the exact supplied text once per segment."""
        spans=[]
        part=self.parts[self.index]
        search=0
        for row in self.current.get('timing',[]):
            word=row['word'].strip()
            if not word:continue
            match=re.compile(re.escape(word),re.IGNORECASE).search(part,search)
            if match is None:
                word=word.strip('.,!?。！？；;:："“”')
                match=re.compile(re.escape(word),re.IGNORECASE).search(part,search) if word else None
            if match is None:continue
            spans.append((match.start(),match.end(),row['start']))
            search=match.end()
        self._caption_spans=tuple(spans)
        self._caption_starts=tuple(span[2] for span in spans)
        self._caption_word=bisect_right(self._caption_starts,0.)-1

    def caption(self):
        """Return current segment text and the active word's original offsets."""
        if not self.active or self.index>=len(self.parts):return '',0,0
        part=self.parts[self.index]
        if self.phase not in ('playing','ending') or self._caption_word<0:return part,0,0
        start,end,_=self._caption_spans[self._caption_word]
        return part,start,end

    def subtitle(self):
        """Return current segment text and the count of characters reached."""
        part,_,end=self.caption()
        return part,len(part) if self.phase=='ending' else end

    def seek_word(self,offset):
        """Seek using the provider's timestamp for the clicked caption word."""
        if not self.active or self.phase not in ('playing','ending') or self.player is None:return False
        for start,end,seconds in self._caption_spans:
            if start<=offset<end:
                was_ending=self.phase=='ending'
                self._end_hold.stop();self._tail_remaining=0;self.phase='playing'
                self.player.setPosition(round(seconds*1000))
                if was_ending and not self.paused:self.player.play()
                return True
        return False

    def toggle_pause(self):
        if not self.active:return
        self.paused=not self.paused
        if self.phase=='ending':
            if self.paused:
                self._tail_remaining=max(1,self._end_hold.remainingTime())
                self._end_hold.stop()
            else:self._end_hold.start(self._tail_remaining)
        elif self.player is not None and self.current is not None:
            if self.paused:self.player.pause()
            else:self.player.play()
        elif not self.paused:self._pump.start(0)
        self.changed.emit()
    def _player_error(self,reason,message):
        if self.active:self._fail('语音音频无法播放，请重试或检查音频设备。')
    def _fail(self,message):self.stop();self.error.emit(message)
    def _account_reset(self):
        self.catalog=[];self._catalogs.clear();self._pitch_support.clear();self._voice_jobs.clear();self.voice_epoch+=1;self.stop();self.voices_changed.emit()
    def stop(self,*,completed=False):
        self.finished=completed
        if completed:self.completed_side=self.side;self.completed_text=self.text;self.completed_duration=self.total_duration
        self._end_hold.stop();self._tail_remaining=0
        self.epoch+=1;self.api.cancel_audio();self._pump.stop();self.active=False;self.paused=False;self.progress=None;self.word='';self.side=None;self.phase='idle';self._seek_fraction=None
        for token,(purpose,language) in tuple(self._voice_jobs.items()):
            if purpose=='play':self.api.cancel_voice(token);self._voice_jobs.pop(token,None)
        self.segment_progress=0.0;self.ready={};self.inflight=None;self.current=None;self._switching=True
        try:self._detach_device()
        finally:self._switching=False
        self.changed.emit()
    def close(self):
        if self.closed:return
        self.stop();self.closed=True
        if self._export is not None:self._export.close()
        self.api.close();self._drain_retired()
