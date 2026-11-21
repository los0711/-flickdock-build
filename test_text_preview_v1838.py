"""Full-size files remain references; only bounded text is read off-thread."""
import threading,unittest
from pathlib import Path
from unittest.mock import patch
from text_preview import TEXT_PREVIEW_BYTES,read_text_preview
import test_html_native_workspace as w

class TextPreviewTests(unittest.TestCase):
    def test_long_text_preview_scrolls_without_edge_overlays(self):
        from settings_navigation import install_smooth_scrolling
        install_smooth_scrolling(w.APP)
        fixture=w.WorkspaceTests();fixture.setUp()
        try:
            path=fixture.base/'scrollable.txt'
            path.write_text('预览内容。\n'*180,encoding='utf-8')
            fixture.shelf.add_paths('permanent',[str(path)])
            preview=fixture.overview().preview
            preview.show_path(str(path))
            scrollbar=preview.text.verticalScrollBar()
            w.until(lambda:preview.text.isVisible() and scrollbar.maximum()>0)
            scrollbar.setValue(scrollbar.maximum()//2)
            w.flush()
            self.assertGreater(scrollbar.value(),0)
            self.assertIs(preview.text.property('scrollEdgeBlur'),False)
            self.assertIsNone(getattr(preview.text,'_flickdock_edge_fade',None))
        finally:fixture.tearDown()

    def test_bounded_preview_is_async_and_copy_keeps_original_file(self):
        fixture=w.WorkspaceTests();fixture.setUp()
        try:
            path=fixture.base/'large.txt';path.write_bytes(b'A'*(TEXT_PREVIEW_BYTES+1000))
            fixture.shelf.add_paths('permanent',[str(path)]);popup=fixture.overview();preview=popup.preview
            reads=[];original=Path.open;main=threading.get_ident()
            class Reader:
                def __init__(self,stream):self.stream=stream
                def __enter__(self):return self
                def __exit__(self,*args):self.stream.close()
                def read(self,size=-1):
                    reads.append((threading.get_ident(),size));return self.stream.read(size)
            def opened(source,*args,**kwargs):
                stream=original(source,*args,**kwargs)
                return Reader(stream) if str(source)==str(path) else stream
            with patch.object(Path,'open',opened),patch.object(Path,'read_bytes',side_effect=AssertionError('whole-file read')):
                preview.show_path(str(path));w.until(lambda:preview.copy_button.isEnabled())
            self.assertTrue(reads);self.assertTrue(all(thread!=main and size==TEXT_PREVIEW_BYTES+1 for thread,size in reads))
            self.assertEqual(len(preview.text.toPlainText()),TEXT_PREVIEW_BYTES)
            self.assertTrue(preview._text_truncated);self.assertIn('64 KB',preview.info.text())
            with patch.object(fixture.shelf,'copy_items') as copy:
                preview.copy_current();copy.assert_called_once_with([str(path)])
            self.assertEqual(path.stat().st_size,TEXT_PREVIEW_BYTES+1000)
        finally:fixture.tearDown()

    def test_cancelled_result_cannot_replace_new_selection_or_empty_state(self):
        fixture=w.WorkspaceTests();fixture.setUp()
        try:
            popup=fixture.overview();preview=popup.preview;old=fixture.paths[0]
            preview.show_path(old);preview.clear_preview()
            preview._text_loader._completed(old,preview._text_loader._epoch-1,dict(text='stale',truncated=False))
            self.assertIsNone(preview.current_path);self.assertEqual(preview.text.toPlainText(),'')
        finally:fixture.tearDown()

if __name__=='__main__':unittest.main(verbosity=2)
