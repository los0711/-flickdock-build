"""Frozen translator verification; explicit offscreen, synthetic providers only."""
import json,os,sys,tempfile,time,traceback
from pathlib import Path
from types import SimpleNamespace


def run(argv,load_font):
    if os.environ.get('QT_QPA_PLATFORM')!='offscreen':raise RuntimeError('Translator verification requires QT_QPA_PLATFORM=offscreen')
    from PySide6.QtCore import QObject,Signal
    from PySide6.QtGui import QShortcut
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtWidgets import QApplication,QWidget
    from app_version import APP_VERSION
    from app_settings import SettingsStore
    from translator_panel import TranslatorDialog
    from translator_store import TranslatorStore
    from translator_speech import TranslatorSpeech
    import ui_theme
    class TranslationFixture(QObject):
        completed=Signal(int,str,str)
        def translate(self,epoch,text,target,**options):self.epoch=epoch
        def polish(self,epoch,text,target,instruction,**options):self.epoch=epoch
        def cancel(self):pass
        def finish(self,text):self.completed.emit(self.epoch,text,'')
    class SpeechFixture(QObject):
        voices_ready=Signal(int,object,str);completed=Signal(int,object,str);reset=Signal()
        def list_voices(self,epoch,language):self.voices_ready.emit(epoch,dict(voices=[dict(voice_id='fixture',name='QA voice')],pitch_supported=True,default_available=True),'')
        def synthesize(self,epoch,*args):self.completed.emit(epoch,dict(audio=b'ID3-fixture-no-real-audio',timing=[dict(word='Hello',start=0.,end=1.)],starts=[0.]),'')
        def cancel_audio(self):pass
        def cancel_voice(self,epoch):pass
        def close(self):pass
    class PlayerFixture(QObject):
        positionChanged=Signal(int);durationChanged=Signal(int);mediaStatusChanged=Signal(object);errorOccurred=Signal(object,str)
        def __init__(self,owner):super().__init__(owner);self.at=0;self.playing=False
        def duration(self):return 1000
        def position(self):return self.at
        def setSourceDevice(self,*args):self.mediaStatusChanged.emit(QMediaPlayer.LoadedMedia)
        def setSource(self,*args):self.mediaStatusChanged.emit(QMediaPlayer.NoMedia)
        def play(self):self.playing=True
        def pause(self):self.playing=False
        def stop(self):self.playing=False
    app=QApplication.instance() or QApplication([argv[0]]);app.setQuitOnLastWindowClosed(False);load_font(app)
    def settle(seconds=.05):
        until=time.monotonic()+seconds
        while time.monotonic()<until:app.processEvents();time.sleep(.002)
    output=Path(argv[argv.index('--verify-output')+1]);capture=Path(argv[argv.index('--verify-capture')+1]) if '--verify-capture' in argv else None
    report=dict(ok=False,version=APP_VERSION,frozen=bool(getattr(sys,'frozen',False)),qt_platform=app.platformName(),real_provider=False,native_audio_access=False,native_clipboard_access=False)
    view=None;shelf=None
    try:
        with tempfile.TemporaryDirectory(prefix='flickdock-translator-verify-') as directory:
            settings=SettingsStore(directory);settings.update(theme='dark');ui_theme.set_theme('dark');shelf=QWidget()
            store=TranslatorStore(directory,autoload=False);store.data['live']=False;api=TranslationFixture()
            speech=TranslatorSpeech(api=SpeechFixture(),player_factory=lambda owner:(PlayerFixture(owner),None))
            view=TranslatorDialog(SimpleNamespace(settings=settings,shelf=shelf),api=api,store=store,detector=lambda text:'中文',speech=speech)
            view.show();settle();view.source_editor.setPlainText('你好，请确认交付时间。');view.session.translate();api.finish('Hello, please confirm the delivery schedule.');settle()
            assert view.output_editor.toPlainText()=='Hello, please confirm the delivery schedule.'
            view.session.polish('更亲切');api.finish('Hello, could you please confirm the delivery schedule?');settle()
            assert view.session.original=='Hello, please confirm the delivery schedule.';view.session.undo();assert not view.session.revisions
            assert not view.findChildren(QShortcut) and not hasattr(view,'settings_button')
            view.delay_slider.setValue(17);assert view.session.delay_seconds==1.7
            view._speak(True);settle();assert speech.active and speech.player.playing
            speech.player.at=420;speech.player.positionChanged.emit(420);assert abs(speech.progress-.42)<.001
            speech.toggle_pause();assert speech.paused and not speech.player.playing;speech.toggle_pause();assert speech.player.playing
            if capture:capture.parent.mkdir(parents=True,exist_ok=True);assert view.grab().save(str(capture))
            speech.stop();assert not speech.active
            view.session.remember();view.toggle_history();settle(.3);assert view.history.opened
            view.toggle_history();settle(.3);assert not view.history.opened
            for theme in ('light','dark'):
                settings.update(theme=theme);ui_theme.set_theme(theme);view.refresh();settle();assert view._theme==theme
            view.hide();assert not view.session.debounce.isActive() and not speech.active
            # Exercise the real bundled fastText binary/model, not only the
            # deterministic detector used by the UI fixture above.
            from translation_languages import detect_source
            from sync_files import DiskWork
            from PySide6.QtCore import QThreadPool,Qt
            languages=[]
            job=DiskWork(lambda cancel:[detect_source(text) for text in ('这是语言识别测试。','Please confirm the delivery date.','Bonjour, merci pour votre réponse.')])
            job.signals.done.connect(lambda value,error:languages.append((value,error)),Qt.QueuedConnection)
            QThreadPool.globalInstance().start(job);deadline=time.monotonic()+10
            while not languages and time.monotonic()<deadline:app.processEvents();time.sleep(.003)
            assert languages and languages[0][1] is None and languages[0][0]==['中文','英语','法语'],languages
            report['real_local_language_detection']=languages[0][0]
            if '--verify-speech-audio' in argv:
                from PySide6.QtCore import QUrl
                from PySide6.QtMultimedia import QAudioDecoder
                audio=Path(argv[argv.index('--verify-speech-audio')+1]).resolve();decoder=QAudioDecoder();decoded=[];finished=[False]
                def ready():
                    buffer=decoder.read();decoded.append((buffer.byteCount(),buffer.duration()))
                decoder.bufferReady.connect(ready);decoder.finished.connect(lambda:finished.__setitem__(0,True))
                decoder.setSource(QUrl.fromLocalFile(str(audio)));decoder.start();deadline=time.monotonic()+15
                while not finished[0] and time.monotonic()<deadline:
                    app.processEvents();time.sleep(.003)
                    if decoder.error()!=QAudioDecoder.NoError:raise RuntimeError('Frozen speech decode failed: '+decoder.errorString())
                assert finished[0] and sum(row[0] for row in decoded)>0
                report['real_mp3_decode']=dict(pcm_bytes=sum(row[0] for row in decoded),seconds=sum(row[1] for row in decoded)/1000000,speaker_output=False)
                decoder.stop();decoder.deleteLater();app.processEvents()
            report.update(ok=True,two_panes=True,revision_chain=True,history=True,seconds_slider=True,no_extra_shortcuts=True,audio_state_from_player=True,hide_cancels=True)
            view.shutdown();view.deleteLater();view=None;shelf.deleteLater();shelf=None;settle()
    except Exception as error:report.update(error=str(error),traceback=traceback.format_exc())
    finally:
        if view is not None:view.shutdown();view.deleteLater()
        if shelf is not None:shelf.deleteLater()
        app.processEvents();output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return 0 if report['ok'] else 1
