"""Saved-file history regressions with isolated images and no native desktop access."""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from unittest.mock import patch

from PySide6.QtGui import QColor,QImage
from clipboard_history_store import ClipboardHistoryStore,encode_png
from screenshot_history import read_screenshot,save_folders,load_folders,screenshot_hint


class SavedScreenshotTests(unittest.TestCase):
    def image(self,path,color='#147abd',format='PNG'):
        image=QImage(53,37,QImage.Format_ARGB32)
        image.fill(QColor(color))
        self.assertTrue(image.save(str(path),format))
        return image

    def test_file_only_capture_preserves_original_and_reopens_from_history(self):
        with TemporaryDirectory() as root:
            path=Path(root)/'Screenshot 2026-11-21 at 19.30.04.png'
            self.image(path)
            before=path.read_bytes()
            captured=read_screenshot(path,time.time(),False,None,Event())
            self.assertEqual(captured[0],'image')
            store=ClipboardHistoryStore(Path(root)/'history')
            key=store.execute('remember',captured)
            image=QImage.fromData(ClipboardHistoryStore(store.root).execute('payload',key)[1])
            self.assertEqual(image.pixelColor(2,2),QColor('#147abd'))
            store.execute('clear')
            self.assertEqual(path.read_bytes(),before)

    def test_third_party_folders_accept_arbitrary_names_without_importing_other_photos(self):
        with TemporaryDirectory() as root:
            folder=Path(root)/'Capture exports'
            folder.mkdir()
            path=folder/'project-418.png'
            self.image(path)
            with patch('screenshot_history.screenshot_hint',side_effect=lambda path,capture:bool(capture)):
                self.assertIsNone(read_screenshot(path,time.time(),False,None,Event()))
                value=read_screenshot(path,time.time(),False,None,Event(),(folder.resolve(),))
                self.assertEqual(value[0],'image')

    def test_screenshot_metadata_detects_renamed_captures(self):
        import plistlib
        path=Path('/tmp/project-418.png')
        with patch('screenshot_history.os.getxattr',create=True,return_value=plistlib.dumps(True)):
            self.assertTrue(screenshot_hint(path))
        with patch('screenshot_history.os.getxattr',create=True,side_effect=OSError):
            self.assertFalse(screenshot_hint(path))

    def test_partial_file_retries_and_identical_event_is_ignored(self):
        with TemporaryDirectory() as root:
            path=Path(root)/'截图.png'
            path.write_bytes(b'')
            self.assertEqual(read_screenshot(path,time.time(),False,None,Event()),'retry')
            self.image(path)
            captured=read_screenshot(path,time.time(),False,None,Event())
            self.assertEqual(captured[0],'image')
            stamp=captured[1]['_screenshot_stamp']
            self.assertIsNone(read_screenshot(path,time.time(),False,stamp,Event()))
            self.assertIsNone(read_screenshot(path,time.time()+10,False,None,Event()))

    def test_cancellation_never_decodes_a_file(self):
        with TemporaryDirectory() as root:
            path=Path(root)/'Screenshot.png'
            self.image(path)
            cancel=Event();cancel.set()
            with patch('screenshot_history.QImageReader',side_effect=AssertionError('decoded after cancellation')):
                self.assertEqual(read_screenshot(path,time.time(),False,None,cancel),'retry')

    def test_screen_image_deduplicates_file_and_clipboard_encodings(self):
        with TemporaryDirectory() as root:
            image=QImage(29,23,QImage.Format_ARGB32);image.fill(QColor('#71b428'))
            store=ClipboardHistoryStore(root)
            first=store.execute('remember',('image',{'image':image}))
            # BMP and PNG contain identical pixels but different file bytes.
            from PySide6.QtCore import QBuffer,QByteArray,QIODevice
            data=QByteArray();buffer=QBuffer(data);buffer.open(QIODevice.WriteOnly)
            self.assertTrue(image.save(buffer,'BMP'))
            second=store.execute('remember',('image',{'raw':bytes(data)}))
            self.assertEqual(first,second)
            self.assertEqual(len(store.execute('list',12,0)),1)
            store.execute('remove',first)
            third=store.execute('remember',('image',{'raw':encode_png(image)}))
            self.assertEqual(len(store.execute('list',12,0)),1)
            self.assertIsNotNone(store.execute('payload',third))

    def test_selected_screenshot_folders_persist_without_source_file_changes(self):
        with TemporaryDirectory() as root:
            folders=[str(Path(root)/'Screenshots'),str(Path(root)/'Exports')]
            self.assertTrue(save_folders(root,folders,Event()))
            self.assertEqual([str(path) for path in load_folders(root)],folders)
            cancel=Event();cancel.set()
            self.assertFalse(save_folders(root,[],cancel))
            self.assertEqual([str(path) for path in load_folders(root)],folders)


class MacPasteReceiptTests(unittest.TestCase):
    def test_waits_for_the_consumer_instead_of_using_a_dispatch_delay(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import Mock
        from clipboard_history_native import _wait_mac_paste_read
        received=Event()
        provider=SimpleNamespace(received=received,error=None)
        loop=SimpleNamespace(runUntilDate_=Mock(side_effect=lambda _:received.set()))
        foundation=SimpleNamespace(NSRunLoop=SimpleNamespace(currentRunLoop=lambda:loop),
                                   NSDate=SimpleNamespace(dateWithTimeIntervalSinceNow_=lambda value:value))
        with patch.dict(sys.modules,Foundation=foundation):
            self.assertTrue(_wait_mac_paste_read(provider,SimpleNamespace(changeCount=lambda:8),8))
        loop.runUntilDate_.assert_called_once()

    def test_a_new_user_copy_cancels_waiting_for_old_paste_data(self):
        import sys
        from types import SimpleNamespace
        from clipboard_history_native import _wait_mac_paste_read
        foundation=SimpleNamespace(NSRunLoop=SimpleNamespace(currentRunLoop=lambda:None),NSDate=object())
        provider=SimpleNamespace(received=Event(),error=None)
        with patch.dict(sys.modules,Foundation=foundation):
            self.assertFalse(_wait_mac_paste_read(provider,SimpleNamespace(changeCount=lambda:9),8))

    def test_data_provider_errors_keep_the_original_error(self):
        import sys
        from types import SimpleNamespace
        from clipboard_history_native import _wait_mac_paste_read
        received=Event();received.set()
        provider=SimpleNamespace(received=received,error=OSError('native paste payload failed'))
        foundation=SimpleNamespace(NSRunLoop=SimpleNamespace(currentRunLoop=lambda:None),NSDate=object())
        with patch.dict(sys.modules,Foundation=foundation):
            with self.assertRaisesRegex(OSError,'native paste payload failed'):
                _wait_mac_paste_read(provider,SimpleNamespace(changeCount=lambda:8),8)


if __name__=='__main__':
    unittest.main(verbosity=2)

