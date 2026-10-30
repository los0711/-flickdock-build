"""Pointer-directed clipboard intake, isolated Qt and fake native hooks only."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import ctypes,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from PySide6.QtCore import QPoint,QMimeData,QUrl,Qt,QEvent,QBuffer,QIODevice,QByteArray
from PySide6.QtGui import QKeyEvent,QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLineEdit,QWidget,QApplication
from test_html_native_workspace import APP,flush,until
import test_html_native_workspace as workspace
import flickdock
from pointer_paste import PointerPasteController,WinPointerPaste,MacPointerPaste,_KeyboardInput,pointer_over_shelf


class PasteIntakeTests(unittest.TestCase):
    def setUp(self):
        self.fixture=workspace.WorkspaceTests();self.fixture.setUp();self.shelf=self.fixture.shelf
        self.shelf.pinned=False;self.clipboard=APP.clipboard()
    def tearDown(self):self.fixture.tearDown()

    def test_paste_in_overview_does_not_fold_or_hide(self):
        self.fixture.overview();before=self.shelf.visual_geometry()
        self.clipboard.setText('Clipboard text with 中文\nsecond line')
        self.shelf.paste_clipboard('permanent');flush()
        self.assertTrue(self.shelf.expanded)
        until(lambda:not self.shelf.incoming_drop)
        self.assertTrue(self.shelf.isVisible());self.assertTrue(self.shelf.expanded)
        self.assertEqual(self.shelf.visual_geometry(),before)

    def test_pasted_text_has_owned_txt_but_drag_is_text_only(self):
        raw='  Preserve whitespace\r\n中文与 emoji 🌈\t  '
        self.clipboard.setText(raw);self.shelf.paste_clipboard('permanent');flush()
        ref=self.shelf.files['permanent'][-1];self.assertTrue(self.shelf.content.is_text(ref))
        path=Path(self.shelf.content.metadata[ref]['text_file'])
        self.assertEqual(path.suffix,'.txt');self.assertEqual(path.read_bytes().decode('utf8'),raw)
        mime=self.shelf.content.mime_for([ref]);self.assertEqual(mime.text(),raw)
        self.assertFalse(mime.hasUrls());self.assertFalse(mime.hasFormat('text/uri-list'))
        self.shelf.remove_path('permanent',ref);until(lambda:not self.shelf._delete_jobs)
        self.assertFalse(path.exists());self.assertTrue(Path(self.fixture.paths[0]).exists())

    def test_paste_in_preview_does_not_hide_and_local_file_stays_file(self):
        self.clipboard.setMimeData(self._file_mime(self.fixture.paths[0]))
        self.shelf.paste_clipboard('permanent');flush();until(lambda:not self.shelf.incoming_drop)
        self.assertTrue(self.shelf.isVisible());self.assertFalse(self.shelf.expanded)
        self.assertEqual(self.shelf.files['permanent'][-1],str(Path(self.fixture.paths[0]).resolve()))

    def _file_mime(self,path):
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(path)]);return mime

    def test_plain_url_paste_is_txt_and_stays_native_text_when_dragged(self):
        raw='https://example.invalid/original.png';self.clipboard.setText(raw)
        self.shelf.paste_clipboard('permanent');flush()
        ref=self.shelf.files['permanent'][-1]
        self.assertEqual(Path(self.shelf.content.metadata[ref]['text_file']).read_text(encoding='utf8'),raw)
        self.assertEqual(self.shelf.content.mime_for([ref]).text(),raw)
        self.assertFalse(self.shelf.content.mime_for([ref]).hasUrls())

    def test_network_indicator_click_is_informational_and_does_not_start_drag(self):
        QTest.mouseClick(self.shelf.network_status,Qt.LeftButton);flush()
        self.assertIsNone(self.shelf.drag_origin)

    def test_encoded_photo_preserves_every_original_byte(self):
        image=QImage(24,24,QImage.Format_RGB32);image.fill(Qt.red)
        buffer=QBuffer();buffer.open(QIODevice.WriteOnly);self.assertTrue(image.save(buffer,'JPEG',83))
        original=bytes(buffer.data());buffer.close()
        mime=QMimeData();mime.setData('image/jpeg',QByteArray(original))
        refs=self.shelf.content.capture_mime(mime,Path(self.fixture.temp.name)/'photo-captures')
        self.assertEqual(len(refs),1);self.assertEqual(Path(refs[0]).suffix,'.jpg')
        self.assertEqual(Path(refs[0]).read_bytes(),original)

    def test_paste_routes_by_pointer_while_another_editor_has_focus(self):
        manager=self.fixture.manager;manager.shelves=[self.shelf];manager.settings_open=lambda:False
        manager.paste_target_at_pointer=lambda:flickdock.ShelfManager.paste_target_at_pointer(manager)
        controller=PointerPasteController(APP,manager.paste_target_at_pointer,
            lambda target:target.paste_clipboard('permanent'),native=False)
        editor=QLineEdit();editor.setText('original');editor.show();editor.setFocus();flush()
        try:
            self.clipboard.setText('to shelf')
            with patch.object(flickdock.QCursor,'pos',return_value=self.shelf.visual_geometry().center()):
                QTest.keyClick(editor,Qt.Key_V,Qt.ControlModifier);flush()
            self.assertEqual(editor.text(),'original');self.assertIn('to shelf',self.shelf.content.texts.values())
            with patch.object(flickdock.QCursor,'pos',return_value=QPoint(-1000,-1000)):
                QTest.keyClick(editor,Qt.Key_V,Qt.ControlModifier);flush()
            self.assertEqual(editor.text(),'originalto shelf')
        finally:controller.close();editor.close();editor.deleteLater();flush()


class NativePasteTests(unittest.TestCase):
    def test_windows_hook_forwards_unrelated_input_and_swallows_one_accepted_chord(self):
        modifiers={0x11};accept=Mock(return_value=True)
        api=SimpleNamespace(SetWindowsHookExW=Mock(return_value=42),CallNextHookEx=Mock(return_value=88),
            UnhookWindowsHookEx=Mock(),GetAsyncKeyState=lambda key:0x8000 if key in modifiers else 0)
        hook=WinPointerPaste(accept,user32=api,kernel32=SimpleNamespace(GetModuleHandleW=lambda _:1))
        def event(key,message=0x100):
            record=_KeyboardInput(key,0,0,0,0);return hook._event(0,message,ctypes.addressof(record))
        self.assertEqual(event(65),88);self.assertEqual(event(0x56),1)
        self.assertEqual(event(0x56),1);self.assertEqual(event(0x56,0x101),1);accept.assert_called_once()
        accept.return_value=False;self.assertEqual(event(0x56),88)
        modifiers.add(0x12);self.assertEqual(event(0x56),88)
        hook.close();hook.close();api.UnhookWindowsHookEx.assert_called_once_with(42)

    def test_mac_tap_permission_and_only_command_v_is_consumed(self):
        q=SimpleNamespace(kCGEventKeyDown=10,kCGEventKeyUp=11,kCGSessionEventTap=1,kCGHeadInsertEventTap=0,
            kCGEventTapOptionDefault=0,kCGEventTapDisabledByTimeout=100,kCGEventTapDisabledByUserInput=101,
            kCGKeyboardEventKeycode=9,kCGEventFlagMaskCommand=1,kCGEventFlagMaskControl=2,
            kCGEventFlagMaskAlternate=4,kCGEventFlagMaskShift=8,kCFRunLoopCommonModes=7,
            CGEventTapCreate=Mock(return_value=None),CFMachPortCreateRunLoopSource=Mock(return_value=3),
            CFRunLoopAddSource=Mock(),CGEventTapEnable=Mock(),CFRunLoopRemoveSource=Mock(),CFMachPortInvalidate=Mock(),
            CFRunLoopGetMain=lambda:2,CGEventGetIntegerValueField=lambda e,f:e['key'],CGEventGetFlags=lambda e:e['flags'])
        denied=MacPointerPaste(Mock(),quartz=q);self.assertTrue(denied.permission_required);self.assertFalse(denied.available);denied.close()
        q.CGEventTapCreate.return_value=4;accept=Mock(return_value=True);hook=MacPointerPaste(accept,quartz=q)
        event={'key':9,'flags':1};self.assertIsNone(hook._event(None,10,event,None))
        self.assertIsNone(hook._event(None,10,event,None));self.assertIsNone(hook._event(None,11,event,None));accept.assert_called_once()
        accept.return_value=False;self.assertIs(hook._event(None,10,event,None),event)
        accept.return_value=True;event['flags']=9;self.assertIs(hook._event(None,10,event,None),event)
        hook._event(None,100,event,None);hook.close();q.CFMachPortInvalidate.assert_called_once_with(4)

    def test_disabled_native_tap_is_detected_and_recreated(self):
        backends=[]
        def factory(accept):
            backend=SimpleNamespace(available=True,permission_required=False,check_health=Mock(return_value=True),close=Mock())
            backends.append(backend);return backend
        controller=PointerPasteController(APP,lambda:None,lambda _:None,native=False,backend_factory=factory)
        try:
            controller.retry_native();self.assertEqual(len(backends),1)
            backends[0].check_health.return_value=False
            controller.retry_native();self.assertEqual(len(backends),2);backends[0].close.assert_called_once()
        finally:controller.close()

    def test_native_hit_test_rejects_a_window_covering_the_shelf(self):
        widget=SimpleNamespace(winId=lambda:17)
        api=SimpleNamespace(GetCursorPos=lambda _:True,WindowFromPoint=lambda _:42,GetAncestor=lambda hwnd,flags:hwnd)
        self.assertFalse(pointer_over_shelf(widget,QPoint(),platform='win32',user32=api))
        api.WindowFromPoint=lambda _:17
        self.assertTrue(pointer_over_shelf(widget,QPoint(),platform='win32',user32=api))
        q=SimpleNamespace(kCGWindowListOptionOnScreenOnly=1,kCGWindowListExcludeDesktopElements=2,kCGNullWindowID=0,
            kCGWindowBounds='bounds',kCGWindowAlpha='alpha',kCGWindowNumber='id',
            CGWindowListCopyWindowInfo=lambda *_:[{'id':42,'bounds':dict(X=0,Y=0,Width=300,Height=300)},
                {'id':17,'bounds':dict(X=0,Y=0,Width=300,Height=300)}])
        self.assertFalse(pointer_over_shelf(widget,QPoint(50,50),platform='darwin',quartz=q,window_number=17))


if __name__=='__main__':unittest.main()
