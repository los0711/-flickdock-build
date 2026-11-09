"""History format conversions are observations, not new Copy intents."""
from contextlib import ExitStack
import tempfile
from pathlib import Path
import unittest
from uuid import uuid4
from PySide6.QtCore import QBuffer,QIODevice,QMimeData
from PySide6.QtGui import QImage,QColor
from PySide6.QtTest import QTest
from unittest.mock import patch
from clipboard_sync import SYNC_MARKER,signature
import test_clipboard_fast_v1821 as fast_fixture
from test_clipboard_sync_v1819 import APP,until,flush


class HistoryTests(unittest.TestCase):
    def test_file_url_filesystem_alias_is_the_same_clipboard_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'original.bin';source.write_bytes(b'original')
            alias=root/'alias.bin'
            try:alias.symlink_to(source)
            except OSError as error:self.skipTest('Filesystem links unavailable: '+str(error.errno))
            self.assertEqual(signature(('files',[str(source)])),signature(('files',[str(alias)])))
            other=root/'other.bin';other.write_bytes(source.read_bytes())
            self.assertNotEqual(signature(('files',[str(source)])),signature(('files',[str(other)])))
            before=signature(('files',[str(source)]));source.write_bytes(b'changed file')
            self.assertNotEqual(before,signature(('files',[str(alias)])))

    def setUp(self):
        fast_fixture.ClipboardFastTests.setUp(self)
        self.patches=ExitStack();self.sequences=[0,0];self.native_writes=[0,0]
        self.feedback=[];self.clients[0].copy_result.connect(lambda ok,context:self.feedback.append(ok))
        for index,(clip,client) in enumerate(zip(self.clips,self.clients)):
            original=clip.setMimeData
            def write(mime,index=index,original=original):
                self.sequences[index]+=1
                if mime.hasFormat(SYNC_MARKER):self.native_writes[index]+=1
                original(mime)
            self.patches.enter_context(patch.object(clip,'setMimeData',side_effect=write))
            client._sequence=lambda index=index:self.sequences[index]
            client._last_sequence=0
        until(lambda:all(not value._fetching for value in self.clients))

    def tearDown(self):
        self.patches.close();fast_fixture.ClipboardFastTests.tearDown(self)

    def settle(self):
        QTest.qWait(90);flush()

    def test_history_text_clone_without_marker_never_republishes_or_rewrites(self):
        self.clips[0].text('same actual copy')
        until(lambda:self.clips[1].mimeData().text()=='same actual copy')
        for _ in range(4):
            self.clips[1].text('same actual copy');self.settle()
            self.clips[0].text('same actual copy');self.settle()
        self.assertEqual(self.hub.version,1)
        self.assertEqual(self.feedback,[True])
        self.assertEqual(self.native_writes,[0,1])
        self.clips[1].text('a genuinely different copy')
        until(lambda:self.clips[0].mimeData().text()=='a genuinely different copy')
        self.assertEqual(self.hub.version,2)

    def test_png_and_native_bitmap_describe_the_same_content(self):
        image=QImage(160,90,QImage.Format_ARGB32);image.fill(QColor(20,40,70,160))
        buffer=QBuffer();buffer.open(QIODevice.WriteOnly);image.save(buffer,'PNG')
        raw=bytes(buffer.data())
        self.assertEqual(signature(('image',dict(image=image))),signature(('image',dict(raw=raw,mime='image/png'))))
        mime=QMimeData();mime.setImageData(image);self.clips[0].setMimeData(mime)
        until(lambda:self.clips[1].mimeData().hasImage())
        baseline=sum(method=='PUT' for method,_ in self.hub.calls)
        for _ in range(3):
            mime=QMimeData();mime.setImageData(image);mime.setData('image/png',raw)
            self.clips[1].setMimeData(mime);self.settle()
        self.assertEqual(sum(method=='PUT' for method,_ in self.hub.calls),baseline)
        self.assertEqual(self.native_writes,[0,1])

    def test_native_line_ending_conversion_is_not_a_second_copy(self):
        self.clips[0].text('first\nsecond')
        until(lambda:self.clips[1].mimeData().text()=='first\nsecond')
        self.clips[1].text('first\r\nsecond');self.settle()
        self.assertEqual(self.hub.version,1)
        self.assertEqual(self.clips[0].mimeData().text(),'first\nsecond')

    def test_equivalent_remote_payload_does_not_replace_system_clipboard(self):
        self.clips[0].text('existing content')
        until(lambda:self.clips[1].mimeData().text()=='existing content')
        before=list(self.native_writes)
        self.assertTrue(self.clients[1]._apply(('text',dict(text='existing content',html='')),self.clients[1].cache,identity=str(uuid4())))
        self.assertEqual(self.native_writes,before)


if __name__=='__main__':unittest.main()
