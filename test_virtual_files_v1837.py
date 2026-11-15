"""Large shelves create lightweight cards only near the visible viewport."""
import unittest
import threading
from pathlib import Path
from unittest.mock import patch
import test_html_native_workspace as w
from content_grid import GRID_COLUMN_WIDTH_PX


class VirtualFilesTests(unittest.TestCase):
    def setUp(self):
        self.fixture=w.WorkspaceTests();self.fixture.setUp();self.shelf=self.fixture.shelf
        for index in range(125):
            path=self.fixture.base/f'shortcut-{index:03}.dat';path.write_bytes(b'x')
            self.shelf.files['permanent'].append(str(path))
        self.shelf.refresh();self.popup=self.fixture.overview()

    def tearDown(self):
        self.shelf.image_cache.close();self.shelf.image_cache._pool.waitForDone(5000);self.fixture.tearDown()

    def visible_loaded(self):
        start,end,_=self.popup._visible_file_indices()
        return all(self.popup._build_refs[i] in self.popup._item_widgets for i in range(start,end))

    def test_list_and_cards_load_visible_files_and_keep_exact_scroll_extent(self):
        popup=self.popup
        for mode in ('list','icons'):
            popup.change_view_mode(mode);popup.resume_content()
            w.until(self.visible_loaded)
            self.assertTrue(popup._virtual_rows);self.assertLess(len(popup._item_widgets),len(popup.visible_refs())//2)
            bar=popup.scroll.verticalScrollBar();self.assertGreater(bar.maximum(),0)
            maximum=bar.maximum();bar.setValue(maximum);w.until(self.visible_loaded)
            self.assertEqual(bar.value(),maximum)
            final=popup._build_refs[-1];self.assertIn(final,popup._item_widgets)
            popup.select_item(final);self.assertIn(final,popup.chosen_refs())
            if mode=='icons':self.assertEqual(popup._item_widgets[final].width(),popup._grid_card_width())
            bar.setValue(0);w.until(self.visible_loaded)
            self.assertIn(final,popup.chosen_refs(),'selection must survive recycling')
            self.assertEqual(bar.maximum(),maximum)

    def test_grid_construction_does_not_request_unused_native_icons(self):
        popup=self.popup;popup.set_category('audio');w.flush()
        with patch.object(self.shelf,'item_pixmap',wraps=self.shelf.item_pixmap) as icons:
            popup.change_view_mode('icons');popup.set_category('all')
            w.until(self.visible_loaded)
            # Only actual thumbnail paints may request this; creating 128
            # cards must never synchronously resolve 128 shell icons.
            self.assertLess(icons.call_count,25)

    def test_ambiguous_ts_classification_reads_signature_off_the_ui_thread(self):
        path=self.fixture.base/'transport.ts';path.write_bytes(b'G'+bytes(187)+b'G'+bytes(187))
        ref=str(path);self.shelf.files['permanent'].append(ref)
        main=threading.get_ident();readers=[];original=Path.open
        def tracked(source,*args,**kwargs):
            if source==path:readers.append(threading.get_ident())
            return original(source,*args,**kwargs)
        with patch.object(Path,'open',tracked):
            self.assertEqual(self.shelf.content.category(ref),'files')
            w.until(lambda:self.shelf.content.category(ref)=='video')
        self.assertTrue(readers);self.assertNotIn(main,readers)


if __name__=='__main__':unittest.main(verbosity=2)
