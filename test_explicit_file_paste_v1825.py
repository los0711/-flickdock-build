"""Actual Qt transfer entry with owned fake metadata/reader, no native clipboard."""
import unittest
from unittest.mock import patch
from PySide6.QtCore import QEvent,Qt
from PySide6.QtGui import QKeyEvent,QKeySequence
from PySide6.QtTest import QTest
import test_transfer_interaction_v1823 as fixtures
from explicit_file_paste import windows_file_hint
from clipboard_references import receipt


class Reader:
    def __init__(self,parent):self.requests=[];self.cancelled=[];self.closed=False;self.ready=[]
    def read(self,callback,*,replace,purpose):
        self.requests.append((callback,purpose));return str(len(self.requests))
    def cancel(self,token):self.cancelled.append(token)
    def close(self):self.closed=True
    def dispatch_ready(self):
        ready,self.ready=self.ready,[]
        for index,paths in ready:self.complete(index,paths)
    def complete(self,index,paths):
        callback,purpose=self.requests[index]
        callback(receipt(purpose.request,purpose.sequence,'paths',paths=paths),None)


class TransferScopeTests(unittest.TestCase):
    setUp=fixtures.TransferInteractionTests.setUp
    tearDown=fixtures.TransferInteractionTests.tearDown
    copy_files=fixtures.TransferInteractionTests.copy_files
    def isolated_route(self):
        route=self.dialog.drop_router.native_paste
        route.enabled=True;route._hint=lambda:41;route._reader_factory=Reader
        return route
    def press(self,widget=None):
        widget=self.dialog.panel.drop_zone if widget is None else widget
        with patch('PySide6.QtWidgets.QApplication.clipboard',side_effect=AssertionError('GUI touched external payload')):
            QTest.keySequence(widget,QKeySequence(QKeySequence.StandardKey.Paste));fixtures.flush()

    def test_exact_override_keypress_reads_payload_zero_times_and_commits_once(self):
        paths=self.copy_files();route=self.isolated_route();self.press()
        self.assertEqual(len(route._reader.requests),1)
        self.assertEqual(self.dialog.panel._queued_paths,[])
        route._reader.complete(0,paths);self.assertEqual(self.dialog.panel._queued_paths,paths)
        with patch.object(self.dialog.panel,'queue_paths',side_effect=AssertionError('Duplicate completion')):
            route._reader.complete(0,paths)

    def test_mode_handoff_keeps_intent_and_targets_current_panel(self):
        paths=self.copy_files();route=self.isolated_route();self.press()
        old=self.dialog.panel;self.dialog.set_service(self.wan);fixtures.flush()
        self.assertIsNot(self.dialog.panel,old);self.assertEqual(route._reader.cancelled,[])
        route._reader.complete(0,paths)
        self.assertEqual(old._queued_paths,[]);self.assertEqual(self.dialog.panel._queued_paths,paths)

    def test_explicit_clear_remove_send_success_invalidate_pending_intent(self):
        paths=self.copy_files();route=self.isolated_route()
        for action in ('clear','remove','send-success'):
            self.dialog.panel.queue_paths(paths,replace=True);self.press()
            index=len(route._reader.requests)-1
            # All three existing actions replace the draft through queue_paths;
            # even clearing an already-empty draft must fence a pending Paste.
            replacement=paths[1:] if action=='remove' else []
            self.dialog.panel.queue_paths(replacement,replace=True)
            route._reader.complete(index,paths)
            self.assertEqual(self.dialog.panel._queued_paths,replacement)
        self.assertEqual(len(route._reader.cancelled),3)

    def test_hidden_closed_and_reopened_view_does_not_restore_old_draft(self):
        paths=self.copy_files();route=self.isolated_route();self.press()
        self.dialog.hide();self.dialog.show();fixtures.flush()
        route._reader.complete(0,paths);self.assertEqual(self.dialog.panel._queued_paths,[])
        self.assertEqual(route._reader.cancelled,['1'])
        self.press();self.dialog.closing_intent.emit();route._reader.complete(1,paths)
        self.assertEqual(self.dialog.panel._queued_paths,[])

    def test_repeat_never_captures_and_a_new_intent_retires_old_callback(self):
        paths=self.copy_files();route=self.isolated_route();self.press();self.press()
        self.assertEqual(route._reader.cancelled,['1']);self.assertEqual(len(route._reader.requests),2)
        route._reader.complete(0,paths);self.assertEqual(self.dialog.panel._queued_paths,[])
        route._reader.complete(1,paths);self.assertEqual(self.dialog.panel._queued_paths,paths)
        event=QKeyEvent(QEvent.Type.KeyPress,Qt.Key.Key_V,Qt.KeyboardModifier.ControlModifier,'',True,1)
        self.assertTrue(route.maybe_handle(event));self.assertEqual(len(route._reader.requests),2)

    def test_verified_previous_paste_is_delivered_before_new_intent(self):
        paths=self.copy_files();route=self.isolated_route();self.press()
        route._reader.ready=[(0,paths[:1])]
        self.press()
        self.assertEqual(self.dialog.panel._queued_paths,paths[:1]);self.assertEqual(route._reader.cancelled,[])
        route._reader.complete(1,paths[1:])
        self.assertEqual(self.dialog.panel._queued_paths,paths)

    def test_file_paste_in_code_field_does_not_write_or_move_native_input(self):
        paths=self.copy_files();route=self.isolated_route();self.dialog.set_service(self.wan)
        edit=self.dialog.panel.peer_code_input;edit.setText('123456');edit.setSelection(2,2)
        before=(edit.text(),edit.cursorPosition(),edit.selectedText())
        self.press(edit);route._reader.complete(0,paths)
        self.assertEqual((edit.text(),edit.cursorPosition(),edit.selectedText()),before)
        self.assertEqual(self.dialog.panel._queued_paths,paths)

    def test_metadata_hint_does_not_read_payload_or_turn_absence_into_non_file_proof(self):
        class Metadata:
            value=7;advertised=True
            def sequence(self):return self.value
            def available(self,format):self.test_format=format;return self.advertised
            def read(self,*_):raise AssertionError('Metadata must not GetClipboardData')
        provider=Metadata();self.assertEqual(windows_file_hint(provider),7)
        provider.advertised=False;self.assertIsNone(windows_file_hint(provider))
        route=self.isolated_route();route._hint=lambda:None
        event=QKeyEvent(QEvent.Type.KeyPress,Qt.Key.Key_V,Qt.KeyboardModifier.ControlModifier)
        self.assertIsNone(route.maybe_handle(event));self.assertIsNone(route._reader)


if __name__=='__main__':unittest.main()
