"""Resource retention and cancelled queued decode regression boundaries."""
import tempfile,threading,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from test_html_native_workspace import APP,until,flush
import async_images
from flickdock import PreviewPane


class ResourceTests(unittest.TestCase):
    def test_removed_paths_and_unusual_extensions_do_not_leave_unbounded_caches(self):
        cache=async_images.ImageCache()
        try:
            for index in range(2000):cache.forget(f'C:/fixture/removed-{index}.png')
            self.assertEqual(len(cache._generations),0)
            for index in range(160):cache.pixmap(f'C:/fixture/file.extension{index}',40,dpr=1)
            self.assertLessEqual(len(cache._placeholders),128)
        finally:cache.close();cache.deleteLater();flush()

    def test_video_posters_have_native_byte_budget_without_mutating_visible_frame(self):
        cache={};refresh=[]
        owner=SimpleNamespace(shelf=SimpleNamespace(preview_cache=cache,
            zones={'permanent':SimpleNamespace(refresh=lambda:refresh.append(True))}),
            popup=SimpleNamespace(isVisible=lambda:False))
        frame=QImage(3840,2160,QImage.Format_RGBA8888);frame.fill(Qt.blue)
        for i in range(48):PreviewPane._poster_ready(owner,str(i),frame)
        self.assertLessEqual(sum(image.sizeInBytes() for image in cache.values()),16*1024*1024)
        self.assertTrue(all(max(image.width(),image.height())<=512 for image in cache.values()))
        self.assertEqual((frame.width(),frame.height()),(3840,2160))
        self.assertIn('47',cache);self.assertNotIn('0',cache);self.assertEqual(len(refresh),48)

    def test_forget_cancels_queued_decode_and_retires_its_runnable(self):
        with tempfile.TemporaryDirectory() as directory:
            first=Path(directory)/'first.png';second=Path(directory)/'removed.png'
            frame=QImage(64,64,QImage.Format_RGB32);frame.fill(Qt.blue)
            frame.save(str(first));frame.save(str(second))
            cache=async_images.ImageCache();cache._pool.setMaxThreadCount(1)
            entered=threading.Event();release=threading.Event();calls=[];original=async_images._decode
            def blocked(path,side,cancel):
                calls.append(path)
                if path==str(first):entered.set();release.wait(3)
                return original(path,side,cancel)
            try:
                with patch('async_images._decode',side_effect=blocked):
                    cache.request(first,128);until(entered.is_set)
                    cache.request(second,128);cache.forget(second);release.set()
                    until(lambda:not cache._pending)
                self.assertEqual(calls,[str(first)]);self.assertIsNone(cache.image(second,128))
                cache.request(second,128);until(lambda:cache.image(second,128) is not None)
                self.assertFalse(cache.image(second,128).isNull())
            finally:release.set();cache.close();cache._pool.waitForDone(3000);cache.deleteLater();flush()


if __name__=='__main__':unittest.main()
