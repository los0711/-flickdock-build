"""Real speech state machine, fake provider/audio device: no audio or credits."""
import base64
from pathlib import Path
import tempfile
import threading
import unittest

from PySide6.QtCore import QObject, Signal
from PySide6.QtMultimedia import QMediaPlayer

from breeze_speech_api import BreezeSpeechApi, decode_audio
from translator_speech import TranslatorSpeech, split_speech_text
from test_translator_v1843 import APP, settle


DATA=dict(audio=b'ID3-fixture-no-real-audio',timing=[dict(word='Hello',start=0.,end=1.)],starts=[0.])


class FakeSpeechApi(QObject):
    voices_ready=Signal(int,object,str)
    completed=Signal(int,object,str)
    reset=Signal()
    def __init__(self):super().__init__();self.voices=[];self.calls=[];self.cancelled=[];self.closed=False
    def list_voices(self,epoch,language):self.voices.append((epoch,language))
    def cancel_voice(self,epoch):self.cancelled.append(epoch)
    def synthesize(self,epoch,text,language,voice,rate,pitch):self.calls.append(dict(epoch=epoch,text=text,language=language,voice=voice,rate=rate,pitch=pitch))
    def cancel_audio(self):pass
    def close(self):self.closed=True
    def finish_voice(self,index=-1):
        epoch,language=self.voices[index]
        self.voices_ready.emit(epoch,dict(voices=[dict(voice_id='voc_'+language,name='Fixture '+language)],pitch_supported=language in ('en','zh'),default_available=True),'')
    def finish_audio(self,error=''):
        self.completed.emit(self.calls[-1]['epoch'],dict(DATA) if not error else None,error)


class FakePlayer(QObject):
    positionChanged=Signal(int)
    durationChanged=Signal(int)
    mediaStatusChanged=Signal(object)
    errorOccurred=Signal(object,str)
    def __init__(self,parent):super().__init__(parent);self.at=0;self.playing=False;self.device=None
    def setSourceDevice(self,device,url):self.device=device;self.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.LoadedMedia)
    def setSource(self,url):self.device=None;self.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.NoMedia)
    def play(self):self.playing=True
    def pause(self):self.playing=False
    def stop(self):self.playing=False
    def duration(self):return 1000
    def position(self):return self.at
    def move(self,position):self.at=position;self.positionChanged.emit(position)
    def finish(self):self.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.EndOfMedia)


class SpeechTests(unittest.TestCase):
    def setUp(self):
        self.api=FakeSpeechApi();self.speech=TranslatorSpeech(api=self.api,player_factory=lambda owner:(FakePlayer(owner),None))
    def tearDown(self):self.speech.close();self.speech.deleteLater();settle(timeout=.02)
    def start(self,text='Hello world.'):
        self.speech.speak(text,'en');self.api.finish_voice();self.api.finish_audio();settle(timeout=.02)
    def test_split_preserves_text_and_bounds_including_emoji_and_whitespace(self):
        for text in ('示例。'*500,'hello world\n'*100,'a'*1300,'😀'*705,' '+('x'*400)+' '*600+' ending'):
            parts=split_speech_text(text);self.assertEqual(''.join(parts),text);self.assertTrue(all(0<len(part)<=300 for part in parts))
    def test_no_provider_or_audio_engine_on_construction(self):
        self.assertFalse(self.api.calls);self.assertFalse(self.api.voices);self.assertIsNone(self.speech.player)
    def test_empty_voice_catalog_never_submits_paid_synthesis_even_on_retry(self):
        errors=[];self.speech.error.connect(errors.append);self.speech.speak('hello','en')
        epoch,_=self.api.voices[-1]
        self.api.voices_ready.emit(epoch,dict(voices=[],default_available=False),'')
        self.assertFalse(self.speech.active);self.assertFalse(self.api.calls)
        self.speech.speak('hello','en');self.assertFalse(self.speech.active)
        self.assertFalse(self.api.calls);self.assertEqual(len(errors),2)
    def test_progress_uses_audio_position_and_pause_resume(self):
        self.start();self.speech.player.move(500);self.assertAlmostEqual(self.speech.progress,.5)
        self.speech.toggle_pause();self.assertTrue(self.speech.paused);self.assertFalse(self.speech.player.playing)
        self.speech.toggle_pause();self.assertTrue(self.speech.player.playing)
    def test_only_one_generation_and_one_prefetched_piece(self):
        self.start('a'*901);self.assertEqual(len(self.api.calls),2);self.assertEqual(len(self.api.calls[1]['text']),300)
        self.api.finish_audio();self.assertEqual(len(self.api.calls),2);self.assertEqual(len(self.speech.ready),1)
        self.speech.player.finish();settle(timeout=.02);self.assertEqual(len(self.api.calls),3);self.assertEqual(self.speech.index,1)
    def test_late_audio_after_cancel_never_opens_player(self):
        self.speech.speak('Hello','en');self.api.finish_voice();epoch=self.api.calls[-1]['epoch'];self.speech.stop()
        self.api.completed.emit(epoch,dict(DATA),'');settle(timeout=.02);self.assertIsNone(self.speech.player);self.assertFalse(self.speech.active)
    def test_browsing_other_language_cannot_change_playback_requests(self):
        self.start('a'*601);self.speech.ensure_engine('zh');self.api.finish_voice();self.api.finish_audio()
        self.speech.player.finish();settle(timeout=.02);self.assertEqual(self.api.calls[-1]['language'],'en')
    def test_account_change_stops_playback_and_discards_catalog(self):
        self.start();self.api.reset.emit();self.assertFalse(self.speech.active);self.assertFalse(self.speech.catalog);self.assertFalse(self.speech.player.playing)
    def test_synthesis_failure_is_visible_and_releases_queue(self):
        errors=[];self.speech.error.connect(errors.append);self.speech.speak('hello','en');self.api.finish_voice();self.api.finish_audio('fixture failure')
        self.assertEqual(errors,['fixture failure']);self.assertFalse(self.speech.active);self.assertFalse(self.speech.ready)


class Transport:
    def __init__(self):self.calls=[];self.cancelled=[]
    def request(self,method,path,callback,**values):self.calls.append(dict(method=method,path=path,callback=callback,**values));return len(self.calls)
    def cancel(self,request):self.cancelled.append(request)


class SpeechApiTests(unittest.TestCase):
    def test_decode_validates_audio_and_timing_off_thread_payload(self):
        value=dict(provider='BreezeBlue',content_type='audio/mpeg',audio_base64=base64.b64encode(DATA['audio']).decode(),word_timestamps=DATA['timing'])
        self.assertEqual(decode_audio(value,threading.Event())['audio'],DATA['audio'])
        for change in (dict(audio_base64='not base64!?'),dict(content_type='text/html'),dict(word_timestamps=[dict(word='bad',start=-1,end=2)])):
            with self.assertRaises(ValueError):decode_audio({**value,**change},threading.Event())
    def test_speech_requests_never_contain_provider_keys_and_cached_audio_reuses_result(self):
        transport=Transport();api=BreezeSpeechApi(transport=transport);out=[];api.completed.connect(lambda *args:out.append(args))
        try:
            api.synthesize(1,'Hello','en','voc_fixture',1.,1.)
            request=transport.calls[-1];self.assertTrue(request.get('authenticated',True));self.assertEqual(set(request['data']),{'text','language','voice_id','speed','pitch','request_id'})
            request['callback'](dict(provider='BreezeBlue',content_type='audio/mpeg',audio_base64=base64.b64encode(DATA['audio']).decode(),word_timestamps=DATA['timing']),None)
            self.assertTrue(settle(lambda:bool(out),2));self.assertEqual(out[-1][0],1)
            calls=len(transport.calls);api.synthesize(2,'Hello','en','voc_fixture',1.,1.);settle(timeout=.02)
            self.assertEqual(len(transport.calls),calls);self.assertEqual(out[-1][0],2)
        finally:api.close()


if __name__=='__main__':unittest.main(verbosity=2)
