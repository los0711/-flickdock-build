"""Real shelf entry points with isolated clipboards; never reads the OS clipboard."""
import unittest
from unittest.mock import patch
from PySide6.QtCore import QMimeData,QPoint,QUrl,Qt
from PySide6.QtGui import QImage,QColor
from PySide6.QtTest import QTest
import test_html_native_workspace as workspace
from test_clipboard_sync_v1819 import Account,Clipboard,Events,Hub
from clipboard_sync import ClipboardSync
from clipboard_preview import ClipboardPresenter,ClipboardPreview
from flickdock import ShelfManager


class PreviewTests(unittest.TestCase):
    def setUp(self):
        workspace.WorkspaceTests.setUp(self)
        self.patches.enter_context(patch.object(workspace.Screen,'geometry',workspace.Screen.availableGeometry,create=True))
        self.patches.enter_context(patch.object(workspace.Screen,'devicePixelRatio',return_value=1,create=True))
        self.hub=Hub();self.clip=Clipboard();self.event=Events();self.hub.events.append(self.event)
        account=Account(self.hub.api('local'),'local')
        self.client=ClipboardSync(account,self.event,self.base/'clipboard',clipboard=self.clip)
        self.model=ClipboardPresenter(self.client)
        self.manager.clipboard_presenter=self.model;self.manager.clipboard_preview_dialog=None
        self.manager.show_clipboard_preview=lambda:ShelfManager.show_clipboard_preview(self.manager)
        for zone in self.shelf.zones.values():zone.clipboard_button.refresh()
        workspace.flush()

    def tearDown(self):
        dialog=self.manager.clipboard_preview_dialog
        if dialog is not None:dialog.close();dialog.deleteLater()
        self.model.close();self.client.close()
        workspace.until(lambda:not self.model.jobs and not self.client._jobs)
        self.model.deleteLater();workspace.flush()
        workspace.WorkspaceTests.tearDown(self)

    overview=workspace.WorkspaceTests.overview

    def test_both_buttons_preview_latest_text_and_never_copy_or_upload_again(self):
        self.clip.text('original 中文\nhttps://example.invalid')
        workspace.until(lambda:self.model.state.get('phase')=='synced')
        popup=self.overview();small=self.shelf.zones['permanent'].clipboard_button
        self.assertIs(popup.clipboard_button.presenter,small.presenter)
        # A publish acknowledgement can still schedule its normal head GET.
        # Previewing must not write/upload again, independent of that read.
        writes=lambda:[call for call in self.hub.calls if call[0]!='GET']
        before=writes();small.click();workspace.flush()
        dialog=self.manager.clipboard_preview_dialog
        self.assertTrue(dialog.isVisible());self.assertIs(dialog.content.currentWidget(),dialog.text)
        self.assertEqual(dialog.text.toPlainText(),self.clip.mimeData().text())
        self.assertEqual(dialog.badge.status,'synced')
        popup.clipboard_button.click();workspace.flush()
        self.assertIs(self.manager.clipboard_preview_dialog,dialog)
        self.assertEqual(writes(),before)
        self.assertEqual(self.clip.mimeData().text(),'original 中文\nhttps://example.invalid')
        self.clip.text('new value');workspace.until(lambda:dialog.text.toPlainText()=='new value')

    def test_active_transfer_moves_clipboard_above_strip_and_entire_button_clicks(self):
        popup=self.overview();button=popup.clipboard_button
        before=button.mapTo(popup.middle,QPoint()).y()
        ref='flickdock:transfer:upload-one'
        self.shelf.content.pending[ref]={'name':'large.bin','kind':'file'}
        self.shelf.content.transfers[ref]={'id':'upload-one','phase':'uploading','total':500000000,'received':500}
        self.shelf.transfers_updated();workspace.flush()
        self.assertTrue(popup.transfer_strip.isVisible())
        top=button.mapTo(popup.middle,QPoint()).y()
        self.assertLess(top,before)
        self.assertLess(top+button.height(),popup.transfer_strip.mapTo(popup.middle,QPoint()).y())
        QTest.mouseClick(button,Qt.LeftButton,pos=QPoint(2,2))
        self.assertTrue(self.manager.clipboard_preview_dialog.isVisible())

    def test_image_enlarges_without_mutating_original_and_status_does_not_redecode(self):
        image=QImage(640,480,QImage.Format_RGB32);image.fill(QColor('#4267a1'))
        path=self.base/'original.png';image.save(str(path),'PNG');original=path.read_bytes()
        mime=QMimeData();mime.setImageData(image);mime.setData('image/png',original);self.clip.setMimeData(mime)
        workspace.until(lambda:not self.model.thumbnail.isNull() and self.model.state.get('phase')=='synced')
        self.manager.show_clipboard_preview();workspace.until(lambda:not self.model.large.isNull())
        dialog=self.manager.clipboard_preview_dialog
        self.assertIs(dialog.content.currentWidget(),dialog.image)
        self.assertEqual(dialog.image.image.pixelColor(30,30),image.pixelColor(30,30))
        with patch.object(self.model,'_decode',wraps=self.model._decode) as decode:
            self.client._set_view(self.client.view['value'],'error',self.client.view['id']);workspace.flush()
            self.assertEqual(dialog.badge.status,'error');self.assertEqual(decode.call_count,0)
        self.assertEqual(bytes(self.clip.mimeData().data('image/png')),original)
        self.assertEqual(path.read_bytes(),original)

    def test_file_empty_and_repeated_enlarge_are_bounded(self):
        path=self.base/'copied.txt';path.write_text('kept original',encoding='utf8')
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(path))]);self.clip.setMimeData(mime)
        workspace.until(lambda:self.model.state.get('phase')=='synced' and not self.model.jobs)
        with patch.object(self.model.pool,'start',wraps=self.model.pool.start) as start:
            for _ in range(20):self.model.request_large()
            self.assertEqual(start.call_count,1)
        self.manager.show_clipboard_preview();workspace.until(lambda:not self.model.jobs)
        dialog=self.manager.clipboard_preview_dialog
        self.assertIn('copied.txt',dialog.info.text());self.assertEqual(path.read_text(encoding='utf8'),'kept original')
        self.clip.clear();workspace.until(lambda:self.model.state.get('value') is None)
        self.assertIs(dialog.content.currentWidget(),dialog.info)
        self.assertIn('暂无',dialog.info.text())

    def test_each_failed_retry_has_error_state_but_notice_remains_deduplicated(self):
        results=[];self.client.copy_result.connect(lambda ok,context:results.append(ok))
        value=('text',{'text':'not uploaded','html':''});pending={'id':'one','feedback':{}}
        self.client._set_view(value,'uploading','one')
        self.client._copy_result(pending,False)
        self.client._set_view(value,'uploading','one')
        self.client._copy_result(pending,False)
        self.assertEqual(self.model.state['phase'],'error')
        self.assertEqual(results,[False])


if __name__=='__main__':unittest.main(verbosity=2)
