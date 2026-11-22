"""Offscreen checks of the clipboard hand-off and translator controls."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from app_settings import SettingsStore
from test_translation import APP, Account, Api, Clipboard
import test_translator_v1844 as translator
import test_html_native_workspace as workspace
from test_translator_v1843 import FakeApi, settle
from translation_controller import TranslationController
from translator_session import TranslatorSession
from translator_store import TranslatorStore
from translator_widgets import ACTION_SIZE
from reference_widgets import IconTextButton
import theme_accent
import ui_theme


class InvitationTests(unittest.TestCase):
    def test_invitation_uses_saved_target_without_a_detection_job(self):
        with tempfile.TemporaryDirectory() as directory, patch('intake_notice.motion_allowed',return_value=False):
            settings=SettingsStore(directory)
            settings.update(translation_enabled=True, translation_target='zh')
            opens=[]
            manager=SimpleNamespace(app=APP, settings=settings, clipboard_sync=Clipboard(), account=Account(),
                                    show_translator_dialog=lambda **values:opens.append(values))
            api=Api()
            controller=TranslationController(manager,api=api)
            try:
                with patch('translation_languages._detector',side_effect=AssertionError('Unexpected clipboard language detection')):
                    controller.observe(('text',{'text':'这是一段测试文字。'}))
                    self.assertEqual(controller.notice._text,'是否翻译成简体中文')
                    self.assertEqual(controller.target,'zh')
                    self.assertEqual(api.calls,[])
                    controller.notice.show_languages()
                    from translation_notice import LanguageList
                    chooser=controller.notice.language_menu.findChild(LanguageList)
                    self.assertEqual(chooser.currentItem().data(Qt.UserRole),'zh')
                    french=next(chooser.item(i) for i in range(chooser.count()) if chooser.item(i).data(Qt.UserRole)=='fr')
                    chooser.itemClicked.emit(french)
                    self.assertEqual(controller.notice._text,'是否翻译成法语')
                    controller.observe(('text',{'text':'Another synthetic sentence.'}))
                    controller.notice.middle_button.click()
                    self.assertEqual(opens,[dict(text='Another synthetic sentence.',target='fr')])
                    self.assertEqual(settings.translation_target,'fr')
            finally:
                controller.close()
                if controller.notice:controller.notice.deleteLater()
                controller.deleteLater()
                settle(timeout=.02)

    def test_cold_launch_handoff_wins_over_stored_translator_target(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'translator.json'
            path.write_text('{"schema_version":1,"target":"ja","live":false}',encoding='utf8')
            store=TranslatorStore(directory)
            session=TranslatorSession(FakeApi(),store,detector=lambda text:'英语')
            try:
                self.assertFalse(store.ready)
                session.open_input('Synthetic clipboard text.','fr')
                self.assertTrue(settle(lambda:store.ready,2))
                self.assertEqual(session.source_text,'Synthetic clipboard text.')
                self.assertEqual(session.target,'fr')
                self.assertEqual(store.data['target'],'fr')
            finally:
                session.close()
                session.deleteLater()
                settle(timeout=.02)


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.fixture=translator.NativeFixTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_indicator_follows_operation_even_if_composer_scope_is_different(self):
        view=self.fixture.view
        self.assertEqual(view.session.polish_scope,'target')
        self.assertTrue(view.session.polish('简洁一些',scope='source'))
        self.assertTrue(view.source_task.isVisible())
        self.assertFalse(view.output_task.isVisible())
        self.fixture.api.finish('Hello.')
        self.assertFalse(view.source_task.isVisible())
        view.session.edit_source('Hello again.')
        view.session.set_language('target','ja')
        self.assertTrue(view.session.translate())
        self.assertTrue(view.output_task.isVisible())
        self.assertEqual(view.output_task.accessibleName(),'正在翻译…')
        self.assertEqual(len(view.output_task._phrases),2)
        view.session.cancel()
        self.assertFalse(view.output_task.isVisible())
        self.assertFalse(view.output_task._frames.isActive())

    def test_loading_text_follows_its_clock_and_stops_when_hidden(self):
        view=self.fixture.view
        view.session.set_language('target','ja')
        with patch('translator_widgets.reduced_motion',return_value=False):
            self.assertTrue(view.session.translate())
            task=view.output_task
            self.assertTrue(task._frames.isActive())
            task._since=.01
            start=task.grab().toImage()
            task._since=.8
            self.assertNotEqual(task.grab().toImage(),start)
            task._since=task.PHRASE_INTERVAL+.06
            self.assertNotEqual(task.grab().toImage(),start)
            self.assertEqual(task.accessibleName(),'正在翻译…')
            view.hide()
            self.assertFalse(task._frames.isActive())

    def test_loading_text_remains_readable_with_reduced_motion(self):
        view=self.fixture.view
        view.session.set_language('target','ja')
        with patch('translator_widgets.reduced_motion',return_value=True):
            self.assertTrue(view.session.translate())
            self.assertTrue(view.output_task.isVisible())
            self.assertFalse(view.output_task._frames.isActive())
            self.assertEqual(view.output_task.text(),'正在翻译…')
            self.fixture.api.finish('こんにちは。')
        self.assertFalse(view.output_task.isVisible())

    def test_translation_completion_displays_the_whole_document_without_reveal(self):
        view=self.fixture.view
        view.session.set_language('target','en')
        view.session.set_language('source','zh')
        view.source_editor.setPlainText('原文测试。')
        text='\n'.join(f'Translated line {number}.' for number in range(160))
        with patch('translator_widgets.reduced_motion',return_value=False):
            self.assertTrue(view.session.translate())
            self.fixture.api.finish(text)
            editor=view.output_editor
            self.assertEqual(editor.toPlainText(),text)
            self.assertFalse(editor._reveal.isActive())
            self.assertFalse(view.output_task.isVisible())

    def test_live_translation_keeps_the_result_editor_still_during_repeated_edits(self):
        view = self.fixture.view
        view.session.set_language('source','en')
        view.session.set_language('target','zh')
        view.session.set_delay(200)
        view.session.set_live(True)
        settle(timeout=.02)
        stable_geometry = view.output_editor.geometry()
        for source, translation in (('Hello again.','再次问好。'),('Hello again, team.','大家好。')):
            previous = view.output_editor.toPlainText()
            view.source_editor.setPlainText(source)
            self.assertTrue(settle(lambda:view.session.phase=='translating',1.5))
            self.assertFalse(view.output_task.isVisible())
            self.assertFalse(view.output_task._frames.isActive())
            self.assertEqual(view.output_editor.geometry(),stable_geometry)
            self.assertEqual(view.output_editor.toPlainText(),previous)
            self.fixture.api.finish(translation)
            settle(timeout=.02)
            self.assertEqual(view.output_editor.geometry(),stable_geometry)
            self.assertEqual(view.output_editor.toPlainText(),translation)

    def test_translator_joined_speech_caps_and_pill_size(self):
        view=self.fixture.view
        self.assertEqual(view.polish_button.width(),76)
        self.assertEqual(view.polish_button._surface_radius(),view.polish_button.height()/2)
        for play,tune in view.speech_buttons.values():
            joined=play.parentWidget()
            self.assertIs(joined,tune.parentWidget())
            self.assertEqual(joined.radius,joined.height()/2)
        for side,(group,menu,row) in view._language_groups.items():
            for i in range(row.count()):
                chip=row.itemAt(i).widget()
                chip.ensurePolished()
                self.assertGreaterEqual(chip.sizeHint().width()-chip.fontMetrics().horizontalAdvance(chip.text()),22)

    def test_copy_actions_and_speech_controls_fit_in_both_footers(self):
        view=self.fixture.view
        view.resize(800,570)
        settle(timeout=.05)
        for copy in (view.source_copy_button,view.copy_button):
            self.assertEqual(copy.height(),ACTION_SIZE)
            self.assertEqual(copy.parentWidget().width()-copy.geometry().right()-1,14)
            self.assertEqual(copy.property('referenceVariant'),'secondary')
        self.assertEqual(view.source_copy_button.text(),'复制原文')
        for play,settings in view.speech_buttons.values():
            self.assertEqual(play.text(),'转语音')
            self.assertEqual(play.height(),settings.height())
            self.assertEqual(settings.width(),settings.height())
            self.assertTrue(settings.property('controlFrame'))

    def test_language_and_voice_pick_with_theme_surface(self):
        view=self.fixture.view
        before=theme_accent.current()
        self.addCleanup(theme_accent.set_color,before)
        theme_accent.set_color('#0066D6')
        view.refresh()
        view.show_language('target')
        popup=view.language_popups['target']
        selected=popup.list.currentItem()
        option=popup.list.itemWidget(selected).findChild(IconTextButton)
        self.assertEqual(option.text(),'英语')
        self.assertFalse(option.icon().isNull())
        self.assertTrue(option.property('leftAligned'))
        self.assertEqual(option.property('referenceVariant'),'primary')
        popup.hide()
        view.show_tts('target')
        self.fixture.voice.finish_voice()
        view._choose_voice('voc_en')
        view._show_voices()
        _,row=view.voice_popup.rows[0]
        settle(timeout=.04)
        self.assertTrue(row.selected)
        pix=row.grab().toImage()
        self.assertEqual(pix.pixelColor(6,round(pix.height()/2)),QColor('#0066D6'))


class ShelfTests(unittest.TestCase):
    def test_unpinned_star_remains_visible_after_pointer_leaves(self):
        fixture=workspace.WorkspaceTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        view=fixture.overview()
        row=view._item_widgets[fixture.paths[0]]
        self.assertFalse(view.is_pinned(row.path))
        from PySide6.QtCore import QEvent
        APP.sendEvent(row,QEvent(QEvent.Leave))
        self.assertTrue(row.pin_button.isVisible())
        row.pin_button.click()
        self.assertTrue(view.is_pinned(row.path))
