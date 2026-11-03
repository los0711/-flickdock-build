"""Actual Qt transfer entry with owned fake metadata/reader, no native clipboard."""
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QEvent,Qt,QMimeData,QDateTime
from PySide6.QtGui import QKeyEvent,QKeySequence
from PySide6.QtTest import QTest
import test_transfer_interaction_v1823 as fixtures
import test_html_native_workspace as shelf_fixtures
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


class ShelfScopeTests(unittest.TestCase):
    def setUp(self):
        self.workspace=shelf_fixtures.WorkspaceTests('runTest');self.workspace.setUp();self.shelf=self.workspace.shelf
        self.route=self.shelf._file_paste_intent();self.route.enabled=True;self.route._hint=lambda:41;self.route._reader_factory=Reader
        self.serial=0
    def tearDown(self):self.workspace.tearDown()
    def paths(self,*,directory=False):
        self.serial+=1;root=self.workspace.base
        paths=[root/f'paste-{self.serial}-{index}.dat' for index in range(2)]
        for index,path in enumerate(paths):path.write_bytes(b'owned original '+bytes([index]))
        if directory:
            folder=root/f'paste-directory-{self.serial}';folder.mkdir();(folder/'inside.txt').write_text('owned');paths.append(folder)
        return [str(path) for path in paths]
    def paste(self):
        with patch.object(shelf_fixtures.subject.QApplication,'clipboard',side_effect=AssertionError('S1 GUI touched foreign provider')):
            self.assertTrue(self.shelf.paste_clipboard('permanent'))
        return len(self.route._reader.requests)-1
    def settle(self,paths):
        shelf_fixtures.until(lambda:all(path in self.shelf.files['permanent'] for path in paths) and not self.shelf.incoming_drop)

    def test_files_and_directory_use_one_isolated_request_without_gui_payload_read(self):
        paths=self.paths(directory=True);before=list(self.shelf.files['permanent']);index=self.paste()
        self.assertTrue(self.shelf.paste_hold_active())
        self.assertEqual(self.shelf.files['permanent'],before);self.assertEqual(len(self.route._reader.requests),1)
        self.assertTrue(self.route._reader.requests[0][1].canonicalize)
        self.route._reader.complete(index,paths);self.settle(paths)
        self.assertFalse(self.shelf.paste_hold_active())
        for file in paths[:2]:self.assertTrue(Path(file).read_bytes().startswith(b'owned original'))

    def test_preview_and_overview_paste_preserve_the_current_view(self):
        for expanded in (False,True):
            if expanded:self.workspace.overview()
            paths=self.paths();index=self.paste();self.route._reader.complete(index,paths);self.settle(paths)
            self.assertTrue(self.shelf.isVisible());self.assertEqual(self.shelf.expanded,expanded)

    def test_owned_external_editor_paste_chord_routes_once_without_activation_or_gui_read(self):
        from pointer_paste import PointerPasteController
        from PySide6.QtWidgets import QLineEdit
        editor=QLineEdit();editor.setText('keep editor contents');editor.show();editor.setFocus();shelf_fixtures.flush()
        controller=PointerPasteController(shelf_fixtures.APP,lambda:self.shelf,
            lambda owner:owner.paste_clipboard('permanent'),native=False)
        try:
            with patch.object(shelf_fixtures.subject.QApplication,'clipboard',side_effect=AssertionError('Paste chord read external provider on GUI')):
                QTest.keySequence(editor,QKeySequence(QKeySequence.StandardKey.Paste));shelf_fixtures.flush()
            self.assertEqual(editor.text(),'keep editor contents');self.assertEqual(len(self.route._reader.requests),1)
            paths=self.paths();self.route._reader.complete(0,paths);self.settle(paths)
        finally:controller.close();editor.close();editor.deleteLater();shelf_fixtures.flush()

    def test_clear_and_native_hide_reopen_retire_old_intents(self):
        paths=self.paths();index=self.paste();self.shelf.remove_all('permanent');self.route._reader.complete(index,paths)
        self.assertFalse(self.shelf.paste_hold_active())
        self.assertEqual(self.shelf.files['permanent'],[])
        index=self.paste();self.shelf.hide();self.shelf.show();shelf_fixtures.flush();self.route._reader.complete(index,paths)
        self.assertEqual(self.shelf.files['permanent'],[]);self.assertEqual(self.route._reader.cancelled,['1','2'])
        index=self.paste();self.route._reader.complete(index,paths);self.settle(paths)

    def test_late_paths_cannot_enter_settings_quitting_or_hiding_owner(self):
        for boundary in ('settings','quitting','hiding'):
            paths=self.paths();index=self.paste()
            if boundary=='settings':self.workspace.manager.settings_open=lambda:True
            elif boundary=='quitting':self.workspace.manager._quitting=True
            else:self.shelf.visible_state='hiding'
            self.route._reader.complete(index,paths)
            self.assertFalse(any(path in self.shelf.files['permanent'] for path in paths))
            self.workspace.manager.settings_open=lambda:False;self.workspace.manager._quitting=False;self.shelf.visible_state='floating'

    def test_absent_file_hint_keeps_original_text_and_native_drag_semantics(self):
        from types import SimpleNamespace
        self.route._hint=lambda:None;mime=QMimeData();mime.setText('owned original text for shelf')
        with patch.object(shelf_fixtures.subject.QApplication,'clipboard',return_value=SimpleNamespace(mimeData=lambda:mime)):
            self.assertTrue(self.shelf.paste_clipboard('permanent'))
        shelf_fixtures.until(lambda:not self.shelf.incoming_drop);ref=self.shelf.files['permanent'][-1]
        self.assertEqual(self.shelf.content.text(ref),'owned original text for shelf')
        exported=self.shelf.content.mime_for([ref]);self.assertTrue(exported.hasText());self.assertFalse(exported.hasUrls())
        self.assertIsNone(self.route._reader)

    def test_copy_mode_preserves_source_and_uses_the_existing_copy_pipeline(self):
        self.workspace.settings.update(capture_mode='copy');paths=self.paths();before=len(self.shelf.files['permanent'])
        index=self.paste();self.route._reader.complete(index,paths)
        shelf_fixtures.until(lambda:len(self.shelf.files['permanent'])==before+2 and not self.shelf.copy_jobs and not self.shelf.incoming_drop)
        for original,copied in zip(paths,self.shelf.files['permanent'][-2:]):
            self.assertNotEqual(original,copied);self.assertEqual(Path(original).read_bytes(),Path(copied).read_bytes())
            self.assertEqual(self.shelf.content.metadata[copied]['capture_mode'],'copy')

    def test_intake_time_is_bound_to_the_paste_request_not_the_delayed_completion(self):
        expected='2026-11-03T09:12:34Z';moment=QDateTime.fromString(expected,Qt.ISODate)
        class Clock(QDateTime):
            @staticmethod
            def currentDateTimeUtc():return moment
        paths=self.paths()
        with patch.object(shelf_fixtures.subject,'QDateTime',Clock):index=self.paste()
        self.route._reader.complete(index,paths);self.settle(paths)
        for path in paths:self.assertEqual(self.shelf.content.metadata[path]['shelf_added_at'],expected)

    def test_reference_admission_never_stats_the_source_on_the_gui_thread(self):
        self._assert_no_gui_admission_stat('reference')

    def test_copy_admission_never_stats_source_or_captured_files_on_the_gui_thread(self):
        self._assert_no_gui_admission_stat('copy')

    def _assert_no_gui_admission_stat(self,mode):
        from PySide6.QtCore import QThread
        self.workspace.settings.update(capture_mode=mode);paths=self.paths();index=self.paste()
        original=Path.stat;violations=[];base=str(self.workspace.base)
        def stat(path,*args,**kwargs):
            if str(path).startswith(base) and QThread.currentThread()==shelf_fixtures.APP.thread():
                violations.append(str(path));raise AssertionError('Admission disk access on GUI')
            return original(path,*args,**kwargs)
        with patch.object(Path,'stat',stat):
            self.route._reader.complete(index,paths)
            shelf_fixtures.until(lambda:len(self.shelf.files['permanent'])==len(self.workspace.paths)+2 and not self.shelf.copy_jobs and not self.shelf.incoming_drop)
        self.assertEqual(violations,[])
        for path in paths:self.assertTrue(Path(path).read_bytes().startswith(b'owned original'))

    def test_clear_while_preflight_is_running_cannot_restore_the_old_files(self):
        import threading
        import shelf_admission
        entered=threading.Event();release=threading.Event();prepare=shelf_admission.prepare_intake
        def held(paths,cancel):
            entered.set()
            if not release.wait(3):raise TimeoutError('Owned preflight fixture not released')
            return prepare(paths,cancel)
        paths=self.paths()
        try:
            with patch.object(shelf_admission,'prepare_intake',held):
                index=self.paste();self.route._reader.complete(index,paths)
                shelf_fixtures.until(entered.is_set)
                self.shelf.remove_all('permanent');release.set()
                shelf_fixtures.until(lambda:not self.shelf._file_admission_jobs)
            self.assertEqual(self.shelf.files['permanent'],[])
            for path in paths:self.assertTrue(Path(path).exists())
        finally:release.set()

    def test_reverse_disk_completion_preserves_the_order_of_accepted_pastes(self):
        import threading
        import shelf_admission
        release=threading.Event();entered=threading.Event();prepare=shelf_admission.prepare_intake
        first=self.paths();second=self.paths()
        def held(paths,cancel):
            if paths[0]==first[0]:
                entered.set()
                if not release.wait(3):raise TimeoutError('Owned first admission not released')
            return prepare(paths,cancel)
        try:
            with patch.object(shelf_admission,'prepare_intake',held):
                index=self.paste();self.route._reader.complete(index,first);shelf_fixtures.until(entered.is_set)
                index=self.paste();self.route._reader.complete(index,second)
                shelf_fixtures.until(lambda:len(self.shelf._file_admission_jobs)==2 and list(self.shelf._file_admission_jobs.values())[-1] is not None)
                self.assertFalse(any(path in self.shelf.files['permanent'] for path in first+second))
                release.set();shelf_fixtures.until(lambda:not self.shelf._file_admission_jobs and not self.shelf.incoming_drop)
            self.assertEqual(self.shelf.files['permanent'][-4:],first+second)
        finally:release.set()

    def test_clear_during_copy_result_preflight_cleans_only_the_owned_copy(self):
        import threading
        import shelf_intake_limit
        self.workspace.settings.update(capture_mode='copy');paths=self.paths()
        entered=threading.Event();release=threading.Event();prepare=shelf_intake_limit.prepare_additions
        destination=Path(self.workspace.settings.capture_dir)/'imports'
        def held(paths,cancel):
            if all(Path(path).is_relative_to(destination) for path in paths):
                entered.set()
                if not release.wait(3):raise TimeoutError('Owned copy result fixture not released')
            return prepare(paths,cancel)
        try:
            with patch.object(shelf_intake_limit,'prepare_additions',held):
                index=self.paste();self.route._reader.complete(index,paths);shelf_fixtures.until(entered.is_set)
                self.shelf.remove_all('permanent');release.set()
                shelf_fixtures.until(lambda:not self.shelf.copy_jobs and not self.shelf._file_admission_jobs)
            self.assertEqual(self.shelf.files['permanent'],[])
            self.assertEqual(list(destination.iterdir()),[])
            for path in paths:self.assertTrue(Path(path).read_bytes().startswith(b'owned original'))
        finally:release.set()


if __name__=='__main__':unittest.main()
