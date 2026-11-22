"""Synthetic editor regression coverage for the floating, incremental translator."""
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject, QPoint, QRect, QSize, Signal
from PySide6.QtWidgets import QApplication, QWidget

from live_translation import LiveEditorTranslation, fit_card
from live_translation_draft import LiveDraft
from live_translation_editor import EditorSelection, EditorState
from live_translation_motion import LiveTranslationMark, LiveTranslationNotice
from translator_store import TranslatorStore


class UnitsApi(QObject):
    units_ready = Signal(int, object, str)

    def __init__(self):
        super().__init__()
        self.calls = []

    def cancel(self):
        return None

    def translate_units(self, generation, rows, target):
        self.calls.append((generation, rows, target))

    def resolve(self, *translations):
        generation, rows, _ = self.calls[-1]
        assert len(rows) == len(translations)
        translated = [dict(id=row['id'], text=text) for row, text in zip(rows, translations)]
        self.units_ready.emit(generation, {'units': translated}, '')


class Transport:
    def __init__(self):
        self.requests = []

    def request(self, method, path, finished, *, data, **kwargs):
        ticket = object()
        self.requests.append((ticket, data, finished))
        return ticket

    def cancel(self, ticket):
        return None


def wait_for(app, predicate, limit=1.5):
    deadline = time.monotonic() + limit
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.004)
    return predicate()


class LiveTranslationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_append_sends_only_the_new_sentence(self):
        api = UnitsApi()
        draft = LiveDraft(api)
        self.addCleanup(draft.reset)
        draft.edit('你好。预算已确认。', immediate=True)
        api.resolve('Hello.', 'The budget is approved.')
        draft.edit('你好。预算已确认。周五出货。', immediate=True)
        self.assertEqual(api.calls[-1][1], [{'id': '2', 'text': '周五出货。', 'before': '', 'after': ''}])
        api.resolve('We ship on Friday.')
        self.assertEqual(draft.result, 'Hello. The budget is approved. We ship on Friday.')

    def test_editing_the_middle_reuses_both_neighbors(self):
        api = UnitsApi()
        draft = LiveDraft(api)
        self.addCleanup(draft.reset)
        draft.edit('你好。预算已确认。周五出货。', immediate=True)
        api.resolve('Hello.', 'The budget is approved.', 'We ship on Friday.')
        draft.edit('你好。预算还在审批。周五出货。', immediate=True)
        self.assertEqual(api.calls[-1][1], [
            {'id': '1', 'text': '预算还在审批。', 'before': '你好。', 'after': '周五出货。'}])
        api.resolve('The budget is pending.')
        self.assertEqual(draft.result, 'Hello. The budget is pending. We ship on Friday.')

    def test_burst_is_coalesced_and_superseded_result_is_never_shown(self):
        api = UnitsApi()
        draft = LiveDraft(api)
        self.addCleanup(draft.reset)
        for text in ('你', '你好', '你好。'):
            draft.edit(text)
        self.assertEqual(api.calls, [])
        self.assertTrue(wait_for(self.app, lambda: len(api.calls) == 1))
        self.assertEqual(api.calls[-1][1][0]['text'], '你好。')
        draft.edit('你好！', immediate=True)
        self.assertEqual(len(api.calls), 1)
        api.resolve('Stale text.')
        self.assertEqual(draft.result, '')
        self.assertEqual(len(api.calls), 2)
        api.resolve('Hello!')
        self.assertTrue(draft.ready)
        self.assertEqual(draft.result, 'Hello!')

    def test_clearing_immediately_invalidates_in_flight_preview(self):
        api = UnitsApi()
        draft = LiveDraft(api)
        self.addCleanup(draft.reset)
        draft.edit('你好。', immediate=True)
        draft.edit('')
        api.resolve('Late greeting.')
        self.assertEqual(draft.result, '')
        self.assertFalse(draft.ready or draft.loading)

    def test_drag_never_intersects_the_editor(self):
        area = QRect(0, 0, 1160, 740)
        editor = QRect(260, 260, 660, 130)
        for x in range(0, 1160, 16):
            for y in range(0, 740, 16):
                rect = fit_card(area, editor, QSize(400, 158), QPoint(x, y))
                self.assertTrue(area.contains(rect))
                self.assertFalse(rect.intersects(editor.adjusted(-9, -9, 9, 9)))
        self.assertTrue(fit_card(area, area, QSize(400, 158)).isEmpty())

    def test_language_tool_samples_one_finished_selection(self):
        bounds = (60, 150, 570, 110)
        editor = EditorState(('scenario',), bounds, '消息。', (0, 1))
        samples = []
        selected = EditorSelection(('other-control',), 'Bonjour à tous')

        def probe(cancel, *, sample=False):
            if sample:
                samples.append(sample)
                return selected
            return editor

        with tempfile.TemporaryDirectory() as directory:
            store = TranslatorStore(directory, autoload=False)
            live = LiveEditorTranslation(Transport(), store, probe=probe)
            try:
                with patch('live_translation.content_language', return_value='fr'), \
                        patch('live_translation.mouse_buttons_mask', return_value=0):
                    live.start('en')
                    self.assertTrue(wait_for(self.app, lambda: live.editor is not None))
                    live.card.sample.setChecked(True)
                    live._toggle_sample()
                    self.assertTrue(wait_for(self.app, lambda: live.draft.target == 'fr'))
                    self.assertFalse(live.card.sample.isChecked())
                    self.assertEqual(samples, [True])
            finally:
                live.stop()
                live.pool.waitForDone(1000)
                live.card.close()
                store.close()

    def test_lottie_notice_is_one_shot_and_mark_loops_while_visible(self):
        parent = QWidget()
        parent.resize(600, 540)
        parent.show()
        notice = LiveTranslationNotice(parent)
        marker = LiveTranslationMark(parent)
        with patch('live_translation_motion.reduced_motion', return_value=False):
            notice.present()
            self.assertTrue(notice.isVisible())
            self.assertTrue(wait_for(self.app, lambda: not notice.isVisible(), limit=2.2))
            self.assertFalse(notice.pulse.isActive())
            marker.set_running(True)
            self.assertEqual(marker.repeat.interval(), 3000)
            self.assertTrue(marker.repeat.isActive())
            first = marker.started
            self.assertTrue(wait_for(self.app, lambda: marker.started is not None and marker.started != first, limit=3.4))
            marker.set_running(False)
            self.assertFalse(marker.repeat.isActive() or marker.pulse.isActive())
        parent.close()

    def test_popup_after_typing_clears_and_inserts_without_a_clipboard_call(self):
        bounds = (60, 150, 570, 110)
        fixture = [EditorState(('test-editor',), bounds, 'Existing text')]
        inserted = []

        def probe(cancel, *, expected=None, insertion=None):
            if cancel.is_set():
                return None
            if insertion is not None:
                if expected != fixture[0]:
                    return False
                inserted.append(insertion)
                fixture[0] = EditorState(('test-editor',), bounds, insertion)
                return True
            return fixture[0]

        with tempfile.TemporaryDirectory() as directory:
            store = TranslatorStore(directory, autoload=False)
            transport = Transport()
            live = LiveEditorTranslation(transport, store, probe=probe)
            try:
                with patch('live_translation._editor_geometry', return_value=(QRect(*bounds), self.app.primaryScreen())):
                    live.start('en')
                    self.assertTrue(wait_for(self.app, lambda: live.editor is not None))
                    self.assertFalse(live.card.isVisible())
                    self.assertEqual(transport.requests, [])
                    fixture[0] = EditorState(('test-editor',), bounds, '这是新消息。')
                    self.assertTrue(wait_for(self.app, live.card.isVisible))
                    self.assertTrue(wait_for(self.app, lambda: bool(transport.requests)))
                    _, data, finish = transport.requests[-1]
                    self.assertEqual(data['units'][0]['text'], '这是新消息。')
                    self.assertTrue(live.card.geometry().intersected(QRect(*bounds)).isEmpty())
                    finish(dict(operation='translate_units', units=[dict(id='0', text='This is a new message.')]), None)
                    self.assertTrue(live.card.insert.isEnabled())
                    live.card.insert.click()
                    self.assertTrue(wait_for(self.app, lambda: bool(inserted)))
                    self.assertEqual(inserted, ['This is a new message.'])
                    self.assertTrue(wait_for(self.app, lambda: not live.card.isVisible() and live.editor == fixture[0]))
                    fixture[0] = EditorState(('test-editor',), bounds, '再试一次。')
                    self.assertTrue(wait_for(self.app, live.card.isVisible))
                    fixture[0] = EditorState(('test-editor',), bounds, '')
                    self.assertTrue(wait_for(self.app, lambda: not live.card.isVisible()))
            finally:
                live.stop()
                live.pool.waitForDone(1000)
                live.card.close()
                store.close()


if __name__ == '__main__':
    unittest.main()
