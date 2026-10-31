"""Clipboard provenance under delayed native formats, session changes and typing."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QMimeData,QTimer,QUrl,Qt,SIGNAL
from PySide6.QtGui import QImage,QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLineEdit,QWidget,QVBoxLayout
from clipboard_sync import ClipboardSync,SYNC_MARKER
from clipboard_audit import ClipboardAudit
import clipboard_audit
from test_clipboard_sync_v1819 import Clipboard,Account,Events,Hub,APP,flush,until


class SequenceClipboard(Clipboard):
    def __init__(self):super().__init__();self.sequence=0;self.writes=0
    def setMimeData(self,mime):
        self.sequence+=1;self.writes+=1;super().setMimeData(mime)
        if mime.hasFormat(SYNC_MARKER):QTimer.singleShot(10,self.delayed_format)
    def delayed_format(self):self.sequence+=1;self.dataChanged.emit()


class OriginTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.hub=Hub();self.clients=[];self.clips=[];self.starts=[0,0]
        for index in range(2):
            clip=SequenceClipboard();events=Events();self.hub.events.append(events)
            client=ClipboardSync(Account(self.hub.api(str(index)),str(index)),events,self.base/str(index),clipboard=clip)
            client._sequence=lambda clip=clip:clip.sequence;client._last_sequence=clip.sequence
            client.copy_started.connect(lambda index=index:self.started(index))
            self.clips.append(clip);self.clients.append(client)
        until(lambda:all(not client._fetching for client in self.clients))

    def started(self,index):self.starts[index]+=1
    def settle(self,seconds=.3):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:APP.processEvents();time.sleep(.002)
    def tearDown(self):
        for client in self.clients:client.close()
        until(lambda:all(not client._jobs for client in self.clients));flush();self.temp.cleanup()
    def puts(self):return sum(method=='PUT' for method,_ in self.hub.calls)

    def test_delayed_text_notifications_never_republish_or_invalidate_receive(self):
        self.clips[0].text('one user action')
        until(lambda:self.clips[1].mimeData().text()=='one user action')
        generation=self.clients[1]._generation;self.settle()
        self.assertEqual(self.puts(),1);self.assertEqual(self.starts[1],0)
        self.assertEqual(self.clients[1]._generation,generation)
        self.assertEqual([clip.writes for clip in self.clips],[1,1])

    def test_file_and_image_delayed_notifications_do_not_repeat_bytes(self):
        source=self.base/'private.bin';source.write_bytes(b'original file bytes')
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(source))]);self.clips[0].setMimeData(mime)
        until(lambda:self.clips[1].mimeData().hasUrls());self.settle()
        self.assertEqual(self.puts(),1)
        copied=Path(self.clips[1].mimeData().urls()[0].toLocalFile())
        self.assertEqual(copied.read_bytes(),source.read_bytes())
        image=QImage(23,17,QImage.Format.Format_RGB32);image.fill(QColor('red'))
        mime=QMimeData();mime.setImageData(image);self.clips[0].setMimeData(mime)
        until(lambda:self.clips[1].mimeData().hasImage());self.settle()
        self.assertEqual(self.puts(),2);self.assertEqual(self.starts[1],0)
        self.assertEqual([clip.writes for clip in self.clips],[2,2])

    def test_real_copy_of_identical_content_without_marker_is_not_swallowed(self):
        self.clips[0].text('same');until(lambda:self.clips[1].mimeData().text()=='same');self.settle()
        previous=self.hub.head['id'];self.clips[1].text('same')
        until(lambda:self.hub.head['id']!=previous);self.settle()
        self.assertEqual(self.puts(),2);self.assertFalse(self.clips[1].mimeData().hasFormat(SYNC_MARKER))

    def test_account_switch_cannot_export_previous_accounts_cloud_payload(self):
        self.clips[0].text('old account cloud content');until(lambda:self.clips[1].mimeData().hasFormat(SYNC_MARKER));self.settle()
        client=self.clients[1];client.account.account={'id':'different-account'}
        client.account.account_changed.emit({});self.clips[1].delayed_format();self.settle()
        self.assertEqual(self.puts(),1);self.assertEqual(self.starts[1],0)

    def test_restarted_service_recognizes_existing_cloud_marker(self):
        self.clips[0].text('existing');until(lambda:self.clips[1].mimeData().hasFormat(SYNC_MARKER));self.settle()
        previous=self.clients[1];previous.close()
        client=ClipboardSync(previous.account,self.hub.events[1],self.base/'restarted',clipboard=self.clips[1])
        client._sequence=lambda:self.clips[1].sequence;client._last_sequence=self.clips[1].sequence
        self.clients.append(client);self.clips[1].delayed_format();self.settle()
        self.assertEqual(self.puts(),1)

    def test_closed_service_disconnects_all_sources_and_never_reads_clipboard(self):
        client=self.clients[1];events=self.hub.events[1];client.close()
        self.assertEqual(client._subscriptions,[])
        self.assertEqual(events.receivers(SIGNAL('clipboard_changed()')),0)
        self.assertEqual(client.account.receivers(SIGNAL('account_changed(PyObject)')),0)
        with patch.object(client,'_snapshot',side_effect=AssertionError('closed service read')):
            events.clipboard_changed.emit();events.connected.emit();client.account.account_changed.emit({});self.settle(.03)

    def test_typing_in_search_settings_and_password_controls_never_writes(self):
        # These are real Qt key events, with the actual ClipboardSync subscribed
        # to a disposable clipboard. Native external applications are tested in CI.
        self.clips[0].text('baseline');until(lambda:self.clips[1].mimeData().text()=='baseline');self.settle()
        before=self.puts();writes=[clip.writes for clip in self.clips]
        parent=QWidget();layout=QVBoxLayout(parent)
        for name in ('search','setting','login','password'):
            edit=QLineEdit(parent);edit.setObjectName(name);layout.addWidget(edit)
            if name=='password':edit.setEchoMode(QLineEdit.EchoMode.Password)
        parent.show()
        try:
            for edit in parent.findChildren(QLineEdit):
                edit.setFocus();QTest.keyClicks(edit,'ordinary input 123');self.assertEqual(edit.text(),'ordinary input 123')
            self.settle();self.assertEqual(self.puts(),before);self.assertEqual([clip.writes for clip in self.clips],writes)
        finally:parent.close();parent.deleteLater()

    def test_production_search_settings_and_account_inputs_do_not_copy(self):
        from contextlib import ExitStack
        import app_settings
        from account_panel import AccountPanel
        from test_account_settings_v181 import Service
        from test_html_native_workspace import WorkspaceTests
        fixture=WorkspaceTests();fixture.setUp();dialog=None;panel=None
        self.clips[0].text('unchanged baseline');until(lambda:self.clips[1].mimeData().text()=='unchanged baseline');self.settle()
        before=self.puts();writes=[clip.writes for clip in self.clips]
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(APP.__class__,'clipboard',return_value=self.clips[0]))
                stack.enter_context(patch.object(app_settings,'_startup_runtime_allowed',return_value=False))
                popup=fixture.overview();QTest.keyClicks(popup.search_edit,'typing into shelf search');self.settle(.03)
                dialog=app_settings.SettingsDialog(app_settings.SettingsStore(self.base/'settings'))
                panel=AccountPanel(Service(),email_binding=False);dialog.set_account_panel(panel);dialog.show()
                QTest.keyClicks(dialog.folder_edit,'ordinary settings input')
                panel.show_auth('login')
                for edit in (panel.username_edit,panel.password_edit):QTest.keyClicks(edit,'normalinput123')
                self.settle();self.assertEqual(self.puts(),before);self.assertEqual([clip.writes for clip in self.clips],writes)
                self.assertEqual(self.clips[0].mimeData().text(),'unchanged baseline')
        finally:
            if panel is not None:
                for name in ('auth_dialog','password_dialog'):
                    widget=getattr(panel,name,None)
                    if widget is not None:widget.hide()
            if dialog is not None:dialog.hide();dialog.deleteLater()
            fixture.tearDown()

    def test_provider_read_failure_cannot_escape_notification_or_diagnostics(self):
        self.clips[0].text('remote');until(lambda:self.clips[1].mimeData().hasFormat(SYNC_MARKER));self.settle()
        client=self.clients[1]
        with patch.object(self.clips[1],'mimeData',side_effect=RuntimeError('provider busy')):
            self.assertFalse(client._own_notification(self.clips[1].sequence+1))

    def test_rejected_cloud_write_preserves_previous_native_origin(self):
        self.clips[0].text('prior');until(lambda:self.clips[1].mimeData().hasFormat(SYNC_MARKER));self.settle()
        client=self.clients[1];marker=client._owned_marker;before=self.puts()
        with patch.object(self.clips[1],'setMimeData',return_value=None):
            self.assertFalse(client._apply(('text',dict(text='rejected',html='')),None,identity='12345678-1234-4234-8234-123456789abc'))
        self.assertEqual(client._owned_marker,marker);self.clips[1].delayed_format();self.settle()
        self.assertEqual(self.puts(),before);self.assertEqual(self.starts[1],0)

    def test_partly_normalized_failed_write_cannot_upload_itself(self):
        client=self.clients[1];clip=self.clips[1];original=clip.setMimeData
        def normalized(mime):mime.setText('platform normalized');original(mime)
        with patch.object(clip,'setMimeData',side_effect=normalized):
            self.assertFalse(client._apply(('text',dict(text='expected',html='')),None,identity='12345678-1234-4234-8234-123456789abc'))
        self.settle();self.assertEqual(self.puts(),0);self.assertEqual(self.starts[1],0)


class AuditTests(unittest.TestCase):
    def test_diagnostics_cannot_fail_a_successful_copy_or_mask_original_error(self):
        with tempfile.TemporaryDirectory() as directory:
            audit=ClipboardAudit(directory);clip=Clipboard()
            context=dict(module='test',function='copy',trigger='button',origin='user_copy')
            with patch.object(clipboard_audit,'_audit',audit),patch.object(clip,'mimeData',side_effect=OSError('read busy')):
                clipboard_audit.set_text(type('TextClipboard',(),{'setText':lambda _,value:clip.text(value),'mimeData':clip.mimeData})(),'still copied',**context)
                self.assertEqual(clip.mime.text(),'still copied')
                with patch.object(clip,'setMimeData',side_effect=RuntimeError('original writer failure')):
                    with self.assertRaisesRegex(RuntimeError,'original writer failure'):
                        clipboard_audit.set_mime(clip,QMimeData(),**context)
            audit.close()

    def test_trace_contains_only_keyed_fingerprints_and_static_context(self):
        with tempfile.TemporaryDirectory() as directory:
            audit=ClipboardAudit(directory);clip=Clipboard();mime=QMimeData();mime.setText('SECRET password / private name')
            with patch.object(clipboard_audit,'_audit',audit):
                for _ in range(2):clipboard_audit.set_mime(clip,mime,module='test',function='copy',trigger='button',origin='user_copy')
            audit.close();rows=[json.loads(line) for line in audit.path.read_text().splitlines()]
            self.assertEqual(len(rows),2);self.assertTrue(rows[1]['duplicate']);self.assertEqual(rows[0]['fingerprint'],rows[1]['fingerprint'])
            self.assertNotIn('SECRET',audit.path.read_text());self.assertNotIn('private name',audit.path.read_text())
            second=ClipboardAudit(Path(directory)/'other')
            self.assertNotEqual(audit.describe(mime)[1],second.describe(mime)[1]);second.close()

    def test_trace_rotation_is_bounded_and_worker_terminates(self):
        with tempfile.TemporaryDirectory() as directory:
            audit=ClipboardAudit(directory,max_bytes=1600,capacity=8);mime=QMimeData();mime.setText('private')
            for _ in range(100):audit.record(mime,module='test',function='copy',trigger='button',origin='user_copy')
            audit.close(3)
            self.assertFalse(audit._thread.is_alive());self.assertLessEqual(len(audit._queue),8)
            self.assertLessEqual(len(list(audit.path.parent.glob('*.jsonl'))),2)
            self.assertTrue(all(path.stat().st_size<=1600 for path in audit.path.parent.glob('*.jsonl')))


if __name__=='__main__':unittest.main()
