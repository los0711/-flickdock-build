"""Frozen translator verification; explicit offscreen, synthetic providers only."""
import json,os,sys,tempfile,time,traceback
from pathlib import Path
from types import SimpleNamespace


def _verify_cocoa_pin(view, settle):
    import ctypes
    import AppKit
    import objc
    from PySide6.QtCore import Qt

    window = view.windowHandle()
    native = objc.objc_object(c_void_p=ctypes.c_void_p(int(view.winId()))).window()
    number, geometry = native.windowNumber(), view.geometry()
    assert native.level() == AppKit.NSNormalWindowLevel
    original_behavior = native.collectionBehavior()
    original_hides = native.hidesOnDeactivate()
    required_behavior = (AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                         | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)

    view.pin_button.click()
    settle()
    assert native.level() > AppKit.NSNormalWindowLevel
    assert native.collectionBehavior() & required_behavior == required_behavior
    assert not native.hidesOnDeactivate()

    def wait_for(predicate, message):
        deadline = time.monotonic() + 8
        while not predicate() and time.monotonic() < deadline:
            settle(.025)
        assert predicate(), message

    # Exercise the OS foreground switch using an existing runner application.
    application = AppKit.NSApplication.sharedApplication()
    finder = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_('com.apple.finder')[0]
    workspace = AppKit.NSWorkspace.sharedWorkspace()
    application.activateIgnoringOtherApps_(True)
    wait_for(application.isActive, 'Translator did not activate')
    finder.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
    finder_pid = finder.processIdentifier()
    wait_for(lambda: workspace.frontmostApplication().processIdentifier() == finder_pid and not application.isActive(),
             'Could not switch to Finder')
    settle(.25)
    assert not application.isActive(), 'Pinned translator stole focus'
    assert native.isVisible() and native.level() >= AppKit.NSModalPanelWindowLevel
    assert native.collectionBehavior() & required_behavior == required_behavior
    application.activateIgnoringOtherApps_(True)
    wait_for(application.isActive, 'Translator did not reactivate')

    # Cocoa recalculates its native level from the QWindow flags during
    # native style changes and full-screen transitions.
    shadowless = bool(window.flags() & Qt.NoDropShadowWindowHint)
    window.setFlag(Qt.NoDropShadowWindowHint, not shadowless)
    window.setFlag(Qt.NoDropShadowWindowHint, shadowless)
    settle()
    assert native.level() >= AppKit.NSModalPanelWindowLevel, 'Cocoa lost the pin when Qt reapplied its flags'
    assert native.collectionBehavior() & required_behavior == required_behavior
    assert view.windowHandle() is window and native.windowNumber() == number
    assert view.geometry() == geometry and view.isVisible()
    view.hide()
    view.show()
    settle()
    assert native.level() >= AppKit.NSModalPanelWindowLevel
    view.pin_button.click()
    settle()
    assert native.level() == AppKit.NSNormalWindowLevel
    assert native.collectionBehavior() == original_behavior
    assert native.hidesOnDeactivate() == original_hides


def run(argv,load_font):
    from offscreen_verify import verification_platform_allowed
    if not verification_platform_allowed():raise RuntimeError('Translator verification requires isolated Qt verification mode')
    from PySide6.QtCore import QObject,Signal
    from PySide6.QtGui import QShortcut
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtWidgets import QApplication,QWidget
    from app_version import APP_VERSION
    from app_settings import SettingsStore
    from translation_controller import TranslationController
    from translator_panel import TranslatorDialog
    from translator_store import TranslatorStore
    from translator_speech import TranslatorSpeech
    import ui_theme
    class TranslationFixture(QObject):
        completed=Signal(int,str,str)
        busy=False
        def translate(self,epoch,text,target,**options):
            self.epoch=epoch;self.target=target;self.busy=True
        def polish(self,epoch,text,target,instruction,**options):self.epoch=epoch;self.busy=True
        def cancel(self):self.busy=False
        def finish(self,text):self.busy=False;self.completed.emit(self.epoch,text,'')
    class ClipboardFixture(QObject):
        copy_observed=Signal(object)
        def set_local_observation(self,enabled):self.observing=enabled
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
    view=None;shelf=None;clipboard_controller=None
    try:
        with tempfile.TemporaryDirectory(prefix='flickdock-translator-verify-') as directory:
            settings=SettingsStore(directory);settings.update(theme='dark');ui_theme.set_theme('dark');shelf=QWidget()
            store=TranslatorStore(directory,autoload=False);store.data['live']=False;api=TranslationFixture()
            speech=TranslatorSpeech(api=SpeechFixture(),player_factory=lambda owner:(PlayerFixture(owner),None))
            clipboard=ClipboardFixture();clipboard_api=TranslationFixture()
            manager=SimpleNamespace(app=app,settings=settings,shelf=shelf,clipboard_sync=clipboard)
            clipboard_controller=manager.translation=TranslationController(manager,api=clipboard_api,
                detector=lambda text:'英语',sequence=lambda:1)
            view=TranslatorDialog(manager,api=api,store=store,detector=lambda text:'中文',speech=speech)
            view.show();settle();view.source_editor.setPlainText('你好，请确认交付时间。');view.session.translate();api.finish('Hello, please confirm the delivery schedule.');settle()
            if sys.platform=='darwin':
                group=view.caption_controls
                assert view.title_bar.layout().indexOf(group)==0 and group.x()<view.back_button.x(),'macOS controls must be at the leading edge'
                assert view.close_button is group.buttons['close'],'Only the leading group owns the macOS close button'
                assert all(button.isVisible() for button in group.buttons.values()),'A macOS caption control is hidden'
                assert tuple(group.buttons)==('close','min','max')
                report['mac_leading_controls']=True
                if app.platformName()=='cocoa':
                    _verify_cocoa_pin(view,settle)
                    report['mac_translator_topmost']=True
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
            settings.update(translation_enabled=False)
            view.show()
            view.session.set_language('target','ja')
            from live_translation import LiveEditorTranslation
            from live_translation_editor import EditorState
            from PySide6.QtCore import QRect
            from unittest.mock import patch

            class LiveTransport:
                requests=[]
                def request(self, method, path, done, *, data, **kwargs):
                    self.requests.append((data,done));return object()
                def cancel(self,ticket):
                    return None

            fixture=[EditorState(('verification-editor',),(65,160,410,90),'')]
            live_transport=LiveTransport()
            live=view.live_editor_translation=LiveEditorTranslation(
                live_transport,store,view,probe=lambda cancel:fixture[0])
            view.auto_clipboard_button.setEnabled(True)
            assert not view.auto_clipboard_button.isChecked()
            with patch('live_translation._editor_geometry',return_value=(QRect(*fixture[0].bounds),app.primaryScreen())):
                view.auto_clipboard_button.click()
                assert live.running and view.isVisible()
                clipboard.copy_observed.emit(('text',{'text':'Synthetic clipboard text.'}))
                deadline=time.monotonic()+2
                while live.editor is None and time.monotonic()<deadline:settle(.01)
                assert live.editor is not None and not live.card.isVisible()
                assert not clipboard_api.busy and not live_transport.requests
                fixture[0]=EditorState(fixture[0].identity,fixture[0].bounds,'输入测试。')
                deadline=time.monotonic()+3
                while not live_transport.requests and time.monotonic()<deadline:settle(.01)
                assert live_transport.requests and live.card.isVisible()
                data,finish=live_transport.requests[-1]
                assert data['target']=='ja' and data['units'][0]['text']=='输入测试。'
                view.close()
                assert live.running and view.auto_clipboard_button.isChecked()
                assert not view._live_activation_notice.isVisible()
                finish(dict(operation='translate_units',units=[dict(id='0',text='テスト。')]),None);settle()
                assert live.draft.result=='テスト。' and not clipboard.observing
                view.show()
                assert view.auto_clipboard_button.isChecked() and not view._live_activation_notice.isVisible()
                view.auto_clipboard_button.click()
                assert not live.running and not live.card.isVisible()
            report['live_editor_translation']=True
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
        if clipboard_controller is not None:clipboard_controller.close();clipboard_controller.pool.waitForDone(2000)
        app.processEvents();output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return 0 if report['ok'] else 1
