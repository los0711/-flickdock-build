"""Copying translated output never recursively invites another translation."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QWidget
import test_translation as fixture
from translator_panel import TranslatorDialog
from translator_store import TranslatorStore
from test_translator_v1843 import FakeApi


class CopyTests(unittest.TestCase):
    setUp=fixture.TranslationTests.setUp
    tearDown=fixture.TranslationTests.tearDown
    def test_suppression_consumes_one_output_and_keeps_next_normal_copy(self):
        self.controller.observe(('text',{'text':'Original source.'}));self.assertTrue(self.controller.notice.visible)
        self.controller.silence_output_copy('Translated output.');self.assertFalse(self.controller.notice.visible)
        self.controller.observe(('text',{'text':'Translated output.'}));self.assertFalse(self.controller.notice.visible)
        self.controller.observe(('text',{'text':'Translated output.'}));self.assertTrue(self.controller.notice.visible)
    def test_delayed_observation_is_not_limited_by_copy_ack_timeout(self):
        self.controller.silence_output_copy('Translated output.');self.controller._copy_failed()
        self.controller.observe(('text',{'text':'Translated output.'}));self.assertIsNone(self.controller.notice)
    def test_a_different_copy_is_never_dropped(self):
        self.controller.silence_output_copy('Translated output.')
        self.controller.observe(('text',{'text':'A different source.'}));self.assertTrue(self.controller.notice.visible)
        self.assertEqual(self.controller.text,'A different source.')
    def test_real_copy_button_arms_before_synchronous_clipboard_notification(self):
        self.manager.translation=self.controller;self.manager.shelf=QWidget();store=TranslatorStore(self.temp.name,autoload=False)
        view=TranslatorDialog(self.manager,api=FakeApi(),store=store,detector=lambda text:'英语');observed=[]
        self.manager.clipboard_sync.copy_observed.connect(observed.append)
        def write(text):self.manager.clipboard_sync.copy_observed.emit(('text',{'text':text}))
        try:
            view.session.result='Translated output.'
            with patch('PySide6.QtWidgets.QApplication.clipboard',return_value=SimpleNamespace(setText=write)),patch('platform_native.native_input_available',return_value=False):view._copy(True)
            self.assertEqual(observed,[('text',{'text':'Translated output.'})]);self.assertIsNone(self.controller.notice)
            self.assertEqual(view.copy_button.text(),'已复制')
        finally:view.shutdown();view.deleteLater();self.manager.shelf.deleteLater();fixture.APP.processEvents()


if __name__=='__main__':unittest.main(verbosity=2)
